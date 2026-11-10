"""'Employers to contact' for healthcare and care jobs: GP practices, care homes, home-care agencies, dental
practices, pharmacies and hospitals near the postcode. These are ORGANISATIONS that employ the kind of person you
are looking for - NOT candidates.

Sources (both free, no key):
* NHS Organisation Data Service ORD API (https://digital.nhs.uk/developer/api-catalogue/organisation-data-service-ord)
  - open data for England and Wales under the Open Government Licence. Searched by postcode district for the
  role (GP practice RO76, care home RO269, domiciliary care RO270, dental practice RO110, pharmacy RO182) for the
  nearest districts only, then the nearest records are read for their address and published phone. The ORD API
  is due to be retired (target Sept 2027); its replacement needs an API key, so this part will stop then.
* OpenStreetMap via the public Overpass API (amenity=doctors, social_facility=nursing_home, amenity=dentist …)
  with the phone / website / email published for the place (© OpenStreetMap contributors, ODbL). One query.
Distances use postcodes.io (free). Results are cached for 7 days (ODS) / 1 day (OSM).
"""
from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field, fields
from typing import Callable, Iterable, Optional

import requests

from ..http import HttpClient
from ..utils import find_emails
from .cache import DiskCache
from .directories import OverpassUnavailable, overpass_elements, overpass_http
from .geo import API as POSTCODES_API, Place, haversine_miles

log = logging.getLogger(__name__)

ODS = "https://directory.spineservices.nhs.uk/ORD/2-0-0/organisations"
ODS_ROLES = {"gp": "RO76", "care_home": "RO269", "home_care": "RO270", "dental": "RO110", "pharmacy": "RO182"}
TYPE_LABELS = {"gp": "GP practice", "care_home": "Care home / nursing home", "home_care": "Home care agency",
               "dental": "Dental practice", "pharmacy": "Pharmacy", "hospital": "Hospital", "clinic": "Clinic"}
OSM_FILTERS = {
    "gp": ['["amenity"="doctors"]', '["healthcare"="doctor"]'],
    "care_home": ['["social_facility"~"^(nursing_home|assisted_living)$"]', '["amenity"="nursing_home"]',
                  '["healthcare"="nursing_home"]'],
    "home_care": ['["healthcare"="home_care"]', '["social_facility"="outreach"]["social_facility:for"~"senior|disabled"]'],
    "dental": ['["amenity"="dentist"]'],
    "pharmacy": ['["amenity"="pharmacy"]'],
    "hospital": ['["amenity"="hospital"]'],
    "clinic": ['["amenity"="clinic"]'],
}
# job title -> kinds of employer (first match wins; checked in order)
ROLE_EMPLOYERS: list[tuple[re.Pattern, list[str]]] = [
    (re.compile(r"\bdental|dentist|hygienist\b", re.I), ["dental"]),
    (re.compile(r"\bpharmac", re.I), ["pharmacy"]),
    (re.compile(r"\b(practice|gp|general practice|treatment room|surgery) nurse|practice manager|gp receptionist|"
                r"medical receptionist|phlebotom", re.I), ["gp"]),
    (re.compile(r"\b(care|support) (worker|assistant)|carer|domiciliary|home care|senior carer|care giver|caregiver|"
                r"activities coordinator|care home", re.I), ["care_home", "home_care"]),
    (re.compile(r"\b(hca|health ?care assistant|nursing assistant|nursing associate|clinical support)", re.I),
     ["gp", "care_home", "hospital"]),
    (re.compile(r"\bnurs(e|es|ing)\b|\brgn\b|\brmn\b|midwi", re.I), ["gp", "care_home", "hospital", "clinic"]),
]


def employer_kinds(title: str) -> list[str]:
    for rx, kinds in ROLE_EMPLOYERS:
        if rx.search(title or ""):
            return kinds
    return []


@dataclass
class EmployerLead:
    """An organisation that employs this kind of worker (business contact details only). Not a candidate."""
    name: str = ""
    employer_type: str = ""
    address: str = ""
    postcode: str = ""
    distance_miles: Optional[float] = None
    phone: str = ""
    email: str = ""
    website: str = ""
    source: str = ""          # "NHS ODS", "OpenStreetMap", "NHS ODS + OpenStreetMap"
    source_url: str = ""      # the record the details came from
    ods_code: str = ""
    lat: Optional[float] = None
    lon: Optional[float] = None
    note: str = "Employer - not a candidate"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def field_names(cls) -> list[str]:
        return [f.name for f in fields(cls)]


def _pc(s: str) -> str:
    return re.sub(r"\s+", "", (s or "").upper())


def _outcode(pc: str) -> str:
    p = _pc(pc)
    return p[:-3] if len(p) > 4 else p


def _title(s: str) -> str:
    return " ".join(w if (w.isupper() and len(w) <= 3 and w not in {"THE", "AND"}) or any(c.isdigit() for c in w)
                    else w.capitalize() for w in (s or "").split())


def _name_key(s: str) -> str:
    s = re.sub(r"\b(the|surgery|medical|practice|centre|center|health|group|dr|drs|ltd|limited|care|home|homes)\b",
               " ", (s or "").lower())
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", s).split())


def _same_place(a: EmployerLead, b: EmployerLead) -> bool:
    if a.ods_code and b.ods_code:
        return a.ods_code.upper() == b.ods_code.upper()   # OSM often tags the NHS code: the strongest join
    try:
        from rapidfuzz import fuzz
        ratio = fuzz.token_set_ratio
    except ImportError:  # pragma: no cover
        import difflib

        def ratio(x, y):
            return 100 * difflib.SequenceMatcher(None, x, y).ratio()
    na, nb = _name_key(a.name), _name_key(b.name)
    sim = ratio(na, nb) if na and nb else ratio(a.name.lower(), b.name.lower())
    if a.postcode and b.postcode and _pc(a.postcode) == _pc(b.postcode):
        return sim >= 70
    if None not in (a.lat, a.lon, b.lat, b.lon):
        return haversine_miles(a.lat, a.lon, b.lat, b.lon) <= 0.2 and sim >= 85
    return False


def merge_leads(ods: list[EmployerLead], osm: list[EmployerLead]) -> list[EmployerLead]:
    out = list(ods)
    for o in osm:
        hit = next((x for x in out if _same_place(x, o)), None)
        if hit is None:
            out.append(o)
            continue
        for f in ("phone", "email", "website", "address", "postcode"):
            if not getattr(hit, f) and getattr(o, f):
                setattr(hit, f, getattr(o, f))
        if "OpenStreetMap" not in hit.source:
            hit.source += " + OpenStreetMap"
            hit.source_url = f"{hit.source_url} {o.source_url}".strip()
    return out


def suppress_leads(leads: list[EmployerLead], entries: Iterable[str]) -> tuple[list[EmployerLead], int]:
    """Do-not-contact list: an email, @domain, website or record URL removes the employer too."""
    ents = [e.strip().lower().rstrip("/") for e in entries if e and e.strip()]
    kept = []
    for l in leads:
        email = l.email.lower()
        site = l.website.lower().split("://", 1)[-1].removeprefix("www.").rstrip("/")
        urls = [u.lower().rstrip("/") for u in l.source_url.split()]
        bad = False
        for e in ents:
            if email and (e == email or (e.startswith("@") and email.endswith(e))):
                bad = True
            elif e.startswith("@") and site and (site == e[1:] or site.startswith(e[1:] + "/") or site.endswith("." + e[1:])):
                bad = True
            elif site and e.split("://", 1)[-1].removeprefix("www.") == site:
                bad = True
            elif e in urls or any(u.split("://", 1)[-1] == e for u in urls):
                bad = True
        if not bad:
            kept.append(l)
    return kept, len(leads) - len(kept)


class EmployerLeadsFinder:
    name = "employers"

    def __init__(self, http: Optional[HttpClient] = None, osm_http: Optional[HttpClient] = None,
                 max_districts: int = 15, max_details: int = 40, max_leads: int = 150, max_geocode: int = 600,
                 osm_radius_miles: float = 6):
        self.http = http or HttpClient(min_interval=0.3, timeout=30, retries=2, backoff=2)
        self.osm_http = osm_http or overpass_http()  # short timeouts + overall budget: don't stall the search
        self.max_districts, self.max_details, self.max_leads = max_districts, max_details, max_leads
        self.max_geocode, self.osm_radius = max_geocode, osm_radius_miles
        self.cache = DiskCache("employers", 7 * 86400)
        self.osm_cache = DiskCache("osm", 86400)
        self.notes: list[str] = []
        self.stats = {"ods_records": 0, "osm_places": 0, "with_phone": 0, "with_website": 0, "with_email": 0}

    # --- NHS ODS -------------------------------------------------------------------------------------------
    def _cached_json(self, url: str, params: Optional[dict] = None):
        key = url + "?" + "&".join(f"{k}={v}" for k, v in sorted((params or {}).items()))
        hit = self.cache.get(key)
        if hit is not None:
            return hit
        data = self.http.get_json(url, params=params)
        self.cache.set(key, data)
        return data

    def districts(self, place: Place, radius_miles: float) -> list[str]:
        """Postcode districts (outcodes) within the radius, nearest first."""
        radius_m = int(min(max(radius_miles, 1) * 1609.344, 25000))
        rows = (self._cached_json(f"{POSTCODES_API}/outcodes", {"lon": round(place.lon, 4), "lat": round(place.lat, 4),
                                                                "radius": radius_m, "limit": 100}) or {}).get("result") or []
        rows = [r for r in rows if r.get("latitude") is not None]
        rows.sort(key=lambda r: haversine_miles(place.lat, place.lon, r["latitude"], r["longitude"]))
        out = [r["outcode"].upper() for r in rows]
        home = _outcode(place.postcode)
        if home and home not in out:
            out.insert(0, home)
        return out

    def prefixes(self, districts: list[str]) -> list[str]:
        """ODS matches postcodes by prefix, so 'M1' also returns M11-M19: one request covers several districts."""
        out: list[str] = []
        for d in districts:
            m = re.match(r"[A-Z]+\d", d)
            p = m.group(0) if m else d
            if p not in out:
                out.append(p)
        return out[: self.max_districts]

    def geocode_postcodes(self, pcs: list[str]) -> dict[str, tuple[float, float]]:
        out: dict[str, tuple[float, float]] = {}
        todo = []
        for p in dict.fromkeys(_pc(x) for x in pcs if x):
            hit = self.cache.get("pc:" + p)
            if hit:
                out[p] = tuple(hit)
            else:
                todo.append(p)
        for i in range(0, len(todo), 100):
            chunk = todo[i:i + 100]
            try:
                self.http._throttle("api.postcodes.io")
                r = self.http.session.post(f"{POSTCODES_API}/postcodes", json={"postcodes": chunk}, timeout=30)
                r.raise_for_status()
                for row in r.json().get("result") or []:
                    res = row.get("result")
                    if res and res.get("latitude") is not None:
                        co = (res["latitude"], res["longitude"])
                        out[_pc(row.get("query"))] = co
                        self.cache.set("pc:" + _pc(row.get("query")), list(co))
            except (requests.RequestException, ValueError) as e:
                log.info("postcode bulk lookup failed: %s", e)
        return out

    def ods_leads(self, kinds: list[str], place: Place, radius_miles: float,
                  progress: Callable[[str], None] = lambda m: None) -> list[EmployerLead]:
        roles = [(k, ODS_ROLES[k]) for k in kinds if k in ODS_ROLES]
        if not roles:
            return []
        dists = self.districts(place, radius_miles)
        wanted = set(dists)
        prefs = self.prefixes(dists)
        progress(f"NHS ODS: {', '.join(TYPE_LABELS[k] for k, _ in roles)} in {len(dists)} nearby postcode districts…")
        recs: dict[str, tuple[str, dict]] = {}
        for pre in prefs:
            for kind, role in roles:
                data = self._cached_json(ODS, {"PostCode": pre, "Roles": role, "Status": "Active", "Limit": 1000})
                for o in (data or {}).get("Organisations") or []:
                    if _outcode(o.get("PostCode", "")) in wanted and o.get("OrgId") not in recs:
                        recs[o["OrgId"]] = (kind, o)
        recs = dict(list(recs.items())[: self.max_geocode])
        coords = self.geocode_postcodes([o.get("PostCode", "") for _, o in recs.values()])
        leads = []
        for oid, (kind, o) in recs.items():
            co = coords.get(_pc(o.get("PostCode", "")))
            dist = round(haversine_miles(place.lat, place.lon, *co), 1) if co else None
            if dist is not None and dist > radius_miles:
                continue
            leads.append(EmployerLead(
                name=_title(o.get("Name", "")), employer_type=TYPE_LABELS[kind], postcode=o.get("PostCode", ""),
                distance_miles=dist, source="NHS ODS", ods_code=oid,
                source_url=f"{ODS}/{oid}", lat=co[0] if co else None, lon=co[1] if co else None))
        leads.sort(key=lambda l: (l.distance_miles is None, l.distance_miles or 0))
        leads = leads[: self.max_leads]
        for l in leads[: self.max_details]:
            try:
                org = (self._cached_json(l.source_url) or {}).get("Organisation") or {}
            except (requests.RequestException, ValueError) as e:
                log.info("ods detail %s failed: %s", l.ods_code, e)
                continue
            loc = (org.get("GeoLoc") or {}).get("Location") or {}
            l.address = ", ".join(_title(loc[k]) for k in ("AddrLn1", "AddrLn2", "AddrLn3", "Town") if loc.get(k))
            for c in ((org.get("Contacts") or {}).get("Contact") or []):
                if c.get("type") == "tel" and not l.phone:
                    l.phone = c.get("value", "")
                elif c.get("type") in ("http", "https", "url") and not l.website:
                    v = c.get("value", "")
                    l.website = v if v.startswith("http") else f"https://{v}"
                elif c.get("type") == "email" and not l.email:
                    l.email = (find_emails(c.get("value", "")) or [""])[0]
        self.stats["ods_records"] = len(leads)
        return leads

    # --- OpenStreetMap ---------------------------------------------------------------------------------------
    def osm_query(self, kinds: list[str], place: Place, radius_miles: float) -> str:
        r = int(min(max(radius_miles, 1), 25) * 1609.344)
        parts = "".join(f"nwr{f}(around:{r},{place.lat:.5f},{place.lon:.5f});" for k in kinds for f in OSM_FILTERS.get(k, []))
        return f"[out:json][timeout:25];({parts});out center tags 400;"

    def osm_leads(self, kinds: list[str], place: Place, radius_miles: float,
                  progress: Callable[[str], None] = lambda m: None) -> list[EmployerLead]:
        q = self.osm_query(kinds, place, radius_miles)
        els = self.osm_cache.get(q)
        if els is None:
            progress("OpenStreetMap: employers near the postcode…")
            try:
                els = overpass_elements(self.osm_http, q)
                self.osm_cache.set(q, els)
            except OverpassUnavailable as e:
                self.notes.append(f"OpenStreetMap's Overpass servers didn't answer ({e}); showing NHS ODS records "
                                  "only - search again later for websites/phones from OpenStreetMap")
                return []
        out = []
        for e in els:
            t = e.get("tags") or {}
            name = t.get("name") or t.get("operator") or ""
            if not name:
                continue
            lat = e.get("lat") or (e.get("center") or {}).get("lat")
            lon = e.get("lon") or (e.get("center") or {}).get("lon")
            dist = round(haversine_miles(place.lat, place.lon, lat, lon), 1) if lat and lon else None
            if dist is not None and dist > radius_miles:
                continue
            kind = self._osm_kind(t, kinds)
            addr = ", ".join(filter(None, [" ".join(filter(None, [t.get("addr:housenumber"), t.get("addr:street")])),
                                           t.get("addr:city") or t.get("addr:town")]))
            out.append(EmployerLead(
                name=name, employer_type=TYPE_LABELS.get(kind, kind), address=addr, postcode=t.get("addr:postcode", ""),
                distance_miles=dist,
                phone=(t.get("phone") or t.get("contact:phone") or "").split(";")[0].strip(),
                email=(find_emails(t.get("email") or t.get("contact:email") or "") or [""])[0],
                website=t.get("website") or t.get("contact:website") or t.get("url") or "",
                source="OpenStreetMap", source_url=f"https://www.openstreetmap.org/{e.get('type', 'node')}/{e.get('id')}",
                ods_code=t.get("ref:ODS") or t.get("ref:GB:nhs") or "", lat=lat, lon=lon))
        self.stats["osm_places"] = len(out)
        return out

    @staticmethod
    def _osm_kind(t: dict, kinds: list[str]) -> str:
        if t.get("amenity") == "doctors" or t.get("healthcare") == "doctor":
            return "gp"
        if t.get("amenity") == "dentist":
            return "dental"
        if t.get("amenity") == "pharmacy":
            return "pharmacy"
        if t.get("amenity") == "hospital":
            return "hospital"
        if t.get("amenity") == "clinic":
            return "clinic"
        if t.get("healthcare") == "home_care" or t.get("social_facility") == "outreach":
            return "home_care"
        return "care_home" if "care_home" in kinds else kinds[0]

    # --- both ------------------------------------------------------------------------------------------------
    def find(self, title: str, place: Place, radius_miles: float, entries: Iterable[str] = (),
             progress: Callable[[str], None] = lambda m: None) -> tuple[list[EmployerLead], int]:
        kinds = employer_kinds(title)
        if not kinds:
            return [], 0
        ods: list[EmployerLead] = []
        try:
            ods = self.ods_leads(kinds, place, radius_miles, progress)
        except (requests.RequestException, ValueError) as e:
            log.info("ods failed: %s", e)
            self.notes.append(f"NHS ODS unavailable ({e})")
        # Overpass: nearest few miles only (a 15-mile query in London returns thousands and times out)
        osm = self.osm_leads(kinds, place, min(radius_miles, self.osm_radius), progress)
        leads = merge_leads(ods, osm)
        leads, n = suppress_leads(leads, entries)
        leads.sort(key=lambda l: (l.distance_miles is None, l.distance_miles or 0, l.name))
        leads = leads[: self.max_leads]
        self.stats.update(with_phone=sum(1 for l in leads if l.phone), with_website=sum(1 for l in leads if l.website),
                          with_email=sum(1 for l in leads if l.email))
        return leads, n
