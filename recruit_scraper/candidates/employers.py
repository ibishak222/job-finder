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
import threading
import time
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
               "dental": "Dental practice", "pharmacy": "Pharmacy", "hospital": "Hospital", "clinic": "Clinic",
               "urgent_care": "Urgent care / walk-in / out-of-hours", "nurse_agency": "Nursing agency",
               "community": "Community nursing service", "trades": "Electrician / plumber / builder / trade firm",
               "accountancy": "Accountancy / tax practice", "law": "Law firm", "food": "Restaurant / pub / cafe / hotel",
               "mental_health": "Mental health / learning disability service"}
OSM_FILTERS = {
    "gp": ['["amenity"="doctors"]', '["healthcare"="doctor"]'],
    "care_home": ['["social_facility"~"^(nursing_home|assisted_living)$"]', '["amenity"="nursing_home"]',
                  '["healthcare"="nursing_home"]'],
    "home_care": ['["healthcare"="home_care"]', '["social_facility"="outreach"]["social_facility:for"~"senior|disabled"]'],
    "dental": ['["amenity"="dentist"]'],
    "pharmacy": ['["amenity"="pharmacy"]'],
    "hospital": ['["amenity"="hospital"]', '["healthcare"="hospice"]', '["amenity"="hospice"]'],
    "mental_health": ['["healthcare"="psychotherapist"]', '["healthcare:speciality"="psychiatry"]'],
    "clinic": ['["amenity"="clinic"]'],
    # trades, accountants, law firms, kitchens: small businesses that publish a phone / website on the map
    "trades": ['["craft"~"^(electrician|plumber|carpenter|builder|hvac|roofer|plasterer|painter|joiner|tiler|'
               'glaziery|heating_engineer|gas_engineer|bricklaying)$"]'],
    "accountancy": ['["office"~"^(accountant|tax_advisor)$"]'],
    "law": ['["office"="lawyer"]'],
    "food": ['["amenity"~"^(restaurant|pub|cafe)$"]["website"]', '["tourism"="hotel"]["website"]'],
}
# job title -> kinds of employer (first match wins; checked in order)
ROLE_EMPLOYERS: list[tuple[re.Pattern, list[str]]] = [
    (re.compile(r"\bdental|dentist|hygienist\b", re.I), ["dental"]),
    (re.compile(r"\bpharmac", re.I), ["pharmacy"]),
    (re.compile(r"mental health|\brmn\b|\brnld\b|psychiatric|learning disabilit|\bcamhs\b|\bcpn\b", re.I),
     ["mental_health", "hospital", "care_home", "community"]),
    (re.compile(r"\b(nursing|residential|care|dementia) home\b.*\b(nurse|rgn)\b|\b(nurse|rgn)\b.*\b(nursing|residential|care|"
                r"dementia) home\b|\b(nursing|care) home (nurse|manager|staff nurse|deputy)", re.I),
     ["care_home", "home_care"]),
    (re.compile(r"\b(advanced nurse practitioner|nurse practitioner|anp|advanced clinical practitioner|"
                r"nurse prescriber)\b", re.I), ["gp", "urgent_care"]),
    (re.compile(r"\b(practice|gp|general practice|treatment room|surgery|primary care) nurse|\bgpn\b|practice manager|"
                r"gp receptionist|medical receptionist|phlebotom", re.I), ["gp"]),
    (re.compile(r"\b(district|community) (nurse|nursing|staff nurse)", re.I), ["community", "gp", "care_home"]),
    (re.compile(r"\b(care|support) (worker|assistant)|carer|domiciliary|home care|senior carer|care giver|caregiver|"
                r"activities coordinator|care home|support worker|care co-?ordinator|healthcare support|"
                r"senior care|reablement|shared lives", re.I), ["care_home", "home_care"]),
    (re.compile(r"\b(hca|health ?care assistant|nursing assistant|nursing associate|clinical support)", re.I),
     ["gp", "care_home", "hospital"]),
    (re.compile(r"\bnurs(e|es|ing)\b|\brgn\b|\brmn\b|midwi", re.I), ["gp", "care_home", "hospital", "clinic"]),
    # official registers only (see registers.py): kitchens, schools, barristers' chambers
    (re.compile(r"\b(chef|cook|kitchen|sous|commis|catering|pastry)\b", re.I), ["food"]),
    (re.compile(r"\b(teacher|teaching assistant|senco|sendco|headteacher|head teacher|nqt|early career teacher|"
                r"cover supervisor|learning support assistant)\b", re.I), ["school"]),
    (re.compile(r"\bbarrister|\bfamily (lawyer|law|solicitor)|\bkc\b|\bcounsel\b", re.I), ["barristers"]),
    (re.compile(r"\b(electrician|plumber|gas engineer|heating engineer|carpenter|joiner|bricklayer|plasterer|"
                r"roofer|painter and decorator|tiler|builder|handyman|hvac|multi-?skilled)\b", re.I), ["trades"]),
    (re.compile(r"\b(accountant|bookkeeper|tax (advisor|adviser|manager)|payroll|auditor|finance manager)\b", re.I),
     ["accountancy"]),
    (re.compile(r"\b(solicitor|paralegal|legal (secretary|assistant|executive)|conveyancer|lawyer)\b", re.I), ["law"]),
]


_NURSING = re.compile(r"\bnurs|\brgn\b|\brmn\b|\banp\b|\bgpn\b|\bhca\b|health ?care assistant|practitioner|"
                      r"\bcare (worker|assistant)|carer|support worker|care co-?ordinator", re.I)

# names that mark the employers a LOCUM / bank worker is placed by: agencies, urgent care and out-of-hours providers,
# PCNs and GP federations / alliances / extended-access hubs
LOCUM_EMPLOYER = re.compile(r"locum|nurse agency|nursing agency|staffing|recruit|relief|out[- ]of[- ]hours|\booh\b|"
                            r"urgent care|walk-?in|federation|\bpcn\b|primary care network|gp (alliance|collaborative)|"
                            r"extended access|\bhub\b|\bagency\b|bank staff|medical personnel", re.I)


def locum_priority(lead: "EmployerLead") -> int:
    """0 for the employers locum / bank staff are booked through, 1 for the rest."""
    return 0 if LOCUM_EMPLOYER.search(f"{lead.employer_type} {lead.name}") else 1


def employer_kinds(title: str) -> list[str]:
    from .matching import contract_type
    for rx, kinds in ROLE_EMPLOYERS:
        if rx.search(title or ""):
            kinds = list(kinds)
            if contract_type(title or "") == "temporary" and _NURSING.search(title or ""):
                # locum / bank nursing: also out-of-hours / urgent care and the nursing agencies that place locums
                kinds += [k for k in ("urgent_care", "nurse_agency") if k not in kinds]
            return kinds
    return []


# which employers come first for a role: the core employer (GP practices for a practice nurse), then the rest
def _tier(lead: "EmployerLead", kinds: list[str]) -> int:
    t = (lead.employer_type or "").lower()
    order = {"gp": r"gp practice|doctors", "urgent_care": r"urgent care|walk-in|out-of-hours",
             "nurse_agency": r"nurse agency|nursing agency|nurse / staffing|staffing agency",
             "mental_health": r"mental health|learning disab", "community": r"community (nursing|health)",
             "care_home": r"care home|nursing home", "home_care": r"home care|care at home|housing support|domiciliary",
             "hospital": r"hospital|hospice", "clinic": r"clinic|community"}
    for i, k in enumerate(kinds):
        if k in order and re.search(order[k], t):
            return i
    return len(kinds)


def contactability(lead: "EmployerLead") -> int:
    return 4 * bool(lead.email) + 2 * bool(lead.website) + 2 * bool(lead.phone) + bool(lead.contact_name)


@dataclass
class EmployerLead:
    """An organisation that employs this kind of worker (business contact details only). Not a candidate."""
    name: str = ""
    employer_type: str = ""
    contact_name: str = ""    # only from an official register (registered manager, nurse prescriber, barrister, head)
    contact_role: str = ""
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
    careers_url: str = ""     # the careers / vacancies page linked from the employer's own website
    vacancy: str = ""         # an open vacancy for this role found on that website ('Practice Nurse - closes ...')
    vacancy_url: str = ""
    vacancy_type: str = ""    # 'locum / bank / temporary' or 'permanent / other', from the advert
    bank_url: str = ""        # a 'join our bank' / locum / flexible-staff page on the employer's website

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
        if o.source not in hit.source:
            hit.source += f" + {o.source}"
            hit.source_url = f"{hit.source_url} {o.source_url}".strip()
    return out


def suppress_leads(leads: list[EmployerLead], entries: Iterable[str]) -> tuple[list[EmployerLead], int]:
    """Do-not-contact list: an email, @domain, website or record URL removes the employer too."""
    ents = [e.strip().lower().rstrip("/") for e in entries if e and e.strip()]
    kept = []
    for l in leads:
        if l.contact_name and l.contact_name.lower() in ents:
            continue          # a named register contact can be suppressed by name too
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
                 max_districts: int = 15, max_details: int = 40, max_leads: int = 250, max_geocode: int = 600,
                 osm_radius_miles: float = 6, registers=True, max_named: int = 150, practice_sites: int = 60,
                 practice_seconds: float = 60):
        self.http = http or HttpClient(min_interval=0.3, timeout=30, retries=2, backoff=2)
        self.osm_http = osm_http or overpass_http()  # short timeouts + overall budget: don't stall the search
        self.max_districts, self.max_details, self.max_leads = max_districts, max_details, max_leads
        self.max_geocode, self.osm_radius = max_geocode, osm_radius_miles
        self.registers, self.max_named = registers, max_named     # True / False / a RegistersFinder (tests)
        self.practice_sites, self.practice_seconds = practice_sites, practice_seconds
        self.budget_seconds = 100.0          # hard overall time budget for the whole employer search (all stages)
        self._deadline = time.monotonic() + self.budget_seconds
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
        stage_end = time.monotonic() + min(25.0, self._left() * 0.25)
        for pre in prefs:
            if time.monotonic() > stage_end:
                self.notes.append("NHS ODS lookup stopped at its time budget (partial list)")
                break
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
            if time.monotonic() > stage_end + 15:
                break
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
        if not any(OSM_FILTERS.get(k) for k in kinds):
            return []
        q = self.osm_query(kinds, place, radius_miles)
        els = self.osm_cache.get(q)
        if els is None:
            progress("OpenStreetMap: employers near the postcode…")
            try:
                els = overpass_elements(self.osm_http, q, budget=min(20.0, max(5.0, self._left() * 0.2)))
                self.osm_cache.set(q, els)
            except OverpassUnavailable as e:
                self.notes.append(f"OpenStreetMap's Overpass servers didn't answer ({e}); showing NHS ODS / official "
                                  "register records only - search again later for OpenStreetMap websites/phones")
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
        if t.get("craft"):
            return "trades"
        if t.get("office") in ("accountant", "tax_advisor"):
            return "accountancy"
        if t.get("office") == "lawyer":
            return "law"
        if t.get("amenity") in ("restaurant", "pub", "cafe") or t.get("tourism") == "hotel":
            return "food"
        return "care_home" if "care_home" in kinds else kinds[0]

    def _bounded(self, label: str, seconds: float, fn, default):
        """fn() within a hard time limit (a stalled download or server can't hold the search): on timeout the stage is
        skipped with a note and the search carries on; the worker is a daemon thread so it never delays exit."""
        box: dict = {}

        def run():
            try:
                box["v"] = fn()
            except BaseException as e:  # noqa: BLE001 - handed back to the caller below
                box["e"] = e
        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(max(1.0, seconds))
        if t.is_alive():
            self.notes.append(f"{label} stopped at its time limit (partial results)")
            return default
        if "e" in box:
            raise box["e"]
        return box["v"]

    def _left(self) -> float:
        return max(0.0, self._deadline - time.monotonic())

    # --- official registers (CQC, Care Inspectorate, ODS CSVs, FSA, GIAS, Charity Commission, BSB) ---------
    def register_leads(self, kinds: list[str], title: str, place: Place, radius_miles: float,
                       progress: Callable[[str], None]) -> tuple[list[EmployerLead], list[EmployerLead]]:
        from .registers import RegistersFinder
        try:
            dists = None if place.nationwide else self.districts(place, radius_miles)
        except (requests.RequestException, ValueError) as e:
            self.notes.append(f"postcode districts unavailable ({type(e).__name__}); official registers skipped")
            return [], []
        finder = self.registers if not isinstance(self.registers, bool) else RegistersFinder()
        if hasattr(finder, "total_budget"):   # register downloads: partial beats late
            finder.total_budget = min(finder.total_budget, max(15.0, self._left() * 0.45))
        rows = finder.find(kinds, title, place, radius_miles, dists, self.geocode_postcodes, progress)
        self.notes += finder.notes
        self.stats.update({f"register_{k}": v for k, v in finder.stats.items()})
        emp, named = [], []
        for r in rows:
            lead = EmployerLead(name=r.name, employer_type=r.employer_type, contact_name=r.contact_name,
                                contact_role=r.contact_role, address=r.address, postcode=r.postcode,
                                distance_miles=r.distance, phone=r.phone, email=r.email, website=r.website,
                                source=r.source, source_url=r.source_url, ods_code=r.ods_code, lat=r.lat, lon=r.lon,
                                note=("Named in an official register - a professional at this workplace, not a "
                                      "job-seeker" if r.named_person else "Employer - not a candidate"))
            (named if r.named_person else emp).append(lead)
        return emp, named

    # --- the practice's own website ----------------------------------------------------------------------------
    def _cap(self, leads: list[EmployerLead], kinds: list[str]) -> list[EmployerLead]:
        """First max_leads, but each other kind asked for (nurse agencies, urgent care for a locum search) keeps a
        share, so 250 GP practices can't push every agency out of the list."""
        if len(leads) <= self.max_leads or len(kinds) < 2:
            return leads[: self.max_leads]
        share = self.max_leads // (2 * len(kinds))
        keep: set[int] = set()
        for t in range(1, len(kinds)):
            keep.update(id(l) for l in [l for l in leads if _tier(l, kinds) == t][:share])
        room = self.max_leads - len(keep)
        out = []
        for l in leads:
            if id(l) in keep:
                out.append(l)
            elif room > 0:
                out.append(l)
                room -= 1
        return out

    def practice_sites_contacts(self, leads: list[EmployerLead], progress: Callable[[str], None],
                                title: str = "") -> None:
        """For the top employers with a website (GP practices, care homes, restaurants, electricians, accountants ...):
        what the employer publishes on its OWN site - contact email, phone, the manager it names, its careers page and any
        open vacancy for this role (robots.txt respected, cached, time-boxed, a few in parallel)."""
        import time
        from concurrent.futures import ThreadPoolExecutor, wait
        from .contacts import SiteContactFinder
        todo = [l for l in leads if l.website][: self.practice_sites]
        if not todo:
            return
        progress(f"Employer websites: reading {len(todo)} sites for the contact details, careers page and vacancies "
                 "they publish…")
        finder = SiteContactFinder(HttpClient(min_interval=0.5, timeout=10, retries=0))
        secs = min(self.practice_seconds, max(8.0, self._left() - 3))
        end = time.monotonic() + secs
        finder.deadline = end + 2
        n_e = n_m = n_p = n_c = n_v = n_l = n_b = 0
        pool = ThreadPoolExecutor(max_workers=12)
        futs = {pool.submit(finder.employer, l.website, title or None): l for l in todo}
        done, _ = wait(futs, timeout=max(1.0, end - time.monotonic()))
        pool.shutdown(wait=False, cancel_futures=True)
        for f in done:
            l = futs[f]
            try:
                got = f.result()
            except Exception as e:  # noqa: BLE001 - one bad site never stops the search
                log.info("employer site %s failed: %s", l.website, e)
                continue
            src = got.get("source_url") or l.website
            found = False
            if got.get("emails") and not l.email:
                l.email = got["emails"][0]
                n_e += 1
                found = True
            if got.get("phones") and not l.phone:
                l.phone = got["phones"][0]
                n_p += 1
                found = True
            if got.get("managers") and not l.contact_name:
                l.contact_name, role = got["managers"][0]
                l.contact_role = f"{role} (named on the practice's own website)"
                n_m += 1
                found = True
            if (got.get("careers_url") or got.get("bank_url")) and not l.careers_url:
                l.careers_url = got.get("careers_url") or got["bank_url"]
                n_c += 1
            if got.get("bank_url") and not l.bank_url:
                l.bank_url = got["bank_url"]
                n_b += 1
            if got.get("vacancies"):
                # a locum / bank advert is shown first: that's the one a locum search is after
                vs = sorted(got["vacancies"], key=lambda v: v.get("type") != "locum / bank / temporary")
                v = vs[0]
                l.vacancy_type = v.get("type", "")
                n_l += l.vacancy_type == "locum / bank / temporary"
                extra = " · ".join(x for x in (v.get("pay"), f"closes {v['closing_date']}" if v.get("closing_date") else "")
                                   if x)
                l.vacancy = v["title"] + (f" ({extra})" if extra else "")
                l.vacancy_url = v.get("url") or v.get("page") or l.careers_url
                n_v += 1
                found = True
            if found and "employer website" not in l.source:
                l.source += " + employer website"
                l.source_url = f"{l.source_url} {src}".strip()
        self.stats.update(practice_sites_checked=len(done), practice_site_emails=n_e, practice_site_managers=n_m,
                          site_phones=n_p, site_careers_pages=n_c, site_vacancies=n_v, site_locum_vacancies=n_l,
                          site_bank_pages=n_b)
        if len(done) < len(todo):
            self.notes.append(f"employer websites: {len(todo) - len(done)} not read (time limit)")

    # --- both ------------------------------------------------------------------------------------------------
    def find(self, title: str, place: Place, radius_miles: float, entries: Iterable[str] = (),
             progress: Callable[[str], None] = lambda m: None) -> tuple[list[EmployerLead], int]:
        kinds = employer_kinds(title)
        if not kinds:
            return [], 0
        self._deadline = time.monotonic() + self.budget_seconds
        ods: list[EmployerLead] = []
        osm: list[EmployerLead] = []
        if not place.nationwide:   # the ODS API and the map search work around a postcode; whole-UK uses registers
            try:
                ods = self._bounded("NHS ODS lookup", min(45.0, self._left() * 0.4),
                                    lambda: self.ods_leads(kinds, place, radius_miles, progress), [])
            except (requests.RequestException, ValueError) as e:
                log.info("ods failed: %s", e)
                self.notes.append(f"NHS ODS unavailable ({e})")
            # Overpass: nearest few miles only (a 15-mile query in London returns thousands and times out)
            osm = self._bounded("OpenStreetMap lookup", min(30.0, self._left() * 0.3),
                                lambda: self.osm_leads(kinds, place, min(radius_miles, self.osm_radius), progress), [])
        leads = merge_leads(ods, osm)
        named: list[EmployerLead] = []
        if self.registers:
            reg_emp, named = self._bounded("Official registers", min(60.0, self._left() * 0.55),
                                           lambda: self.register_leads(kinds, title, place, radius_miles, progress),
                                           ([], []))
            leads = merge_leads(leads, reg_emp)
        leads, n = suppress_leads(leads, entries)
        named, n2 = suppress_leads(named, entries)
        n += n2
        # the right kind of employer first, then nearest (in 3-mile bands) and most contactable
        locum = "nurse_agency" in kinds   # a locum / bank search: agencies, urgent care, OOH, PCNs / federations first
        if locum:
            kinds = ["nurse_agency", "urgent_care"] + [k for k in kinds if k not in ("nurse_agency", "urgent_care")]
        tiered = len(kinds) <= 3 or locum   # a broad title ('nurse') keeps every kind of employer mixed by distance
        leads.sort(key=lambda l: (locum_priority(l) if locum else 0, _tier(l, kinds) if tiered else 0,
                                  l.distance_miles is None, int((l.distance_miles or 0) // 3),
                                  -contactability(l), l.distance_miles or 0, l.name))
        named.sort(key=lambda l: (l.distance_miles is None, l.distance_miles or 0, l.name))
        leads = self._cap(leads, kinds if tiered else [])
        if self.practice_sites:
            self.practice_sites_contacts(leads, progress, title)
            # employers advertising this role right now first, a locum / bank advert before a permanent one (stable)
            leads.sort(key=lambda l: (not l.vacancy, l.vacancy_type != "locum / bank / temporary" if locum else 0))
            leads, n3 = suppress_leads(leads, entries)   # a newly found email may be on the do-not-contact list
            n += n3
        leads = leads + named[: self.max_named]
        self.stats.update(with_phone=sum(1 for l in leads if l.phone), with_website=sum(1 for l in leads if l.website),
                          with_email=sum(1 for l in leads if l.email))
        return leads, n
