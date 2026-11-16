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

from ..agencies import is_agency_employer
from ..http import HttpClient
from ..utils import find_emails
from .cache import DiskCache
from .directories import OverpassUnavailable, overpass_elements, overpass_http
from .geo import API as POSTCODES_API, Place, haversine_miles

log = logging.getLogger(__name__)

ODS = "https://directory.spineservices.nhs.uk/ORD/2-0-0/organisations"
ODS_ROLES = {"trust": "RO197", "gp": "RO76", "care_home": "RO269", "home_care": "RO270", "dental": "RO110", "pharmacy": "RO182"}
TYPE_LABELS = {"gp": "GP practice", "care_home": "Care home / nursing home", "home_care": "Home care agency",
               "dental": "Dental practice", "pharmacy": "Pharmacy", "hospital": "Hospital", "clinic": "Clinic",
               "urgent_care": "Urgent care / walk-in / out-of-hours", "independent": "Independent clinic / private practice",
               "community": "Community nursing service", "trades": "Electrician / plumber / builder / trade firm",
               "accountancy": "Accountancy / tax practice", "law": "Law firm", "food": "Restaurant / pub / cafe / hotel",
               "mental_health": "Mental health / learning disability service",
               "trust": "NHS trust (runs its own staff bank)"}
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
    (re.compile(r"\bpharmac", re.I), ["pharmacy", "gp"]),
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
    (re.compile(r"\b(gps?|general practitioner|salaried gp|locum gp|doctor|physician|medical officer|clinical fellow|"
                r"sessional (gp|doctor))\b", re.I), ["gp", "urgent_care", "hospital"]),
    (re.compile(r"\b(physiotherapist|physio|occupational therapist|paramedic|podiatrist|chiropodist|dietitian|dietician|"
                r"radiographer|sonographer|speech (and language )?therapist|orthoptist|audiologist|health visitor|"
                r"clinical pharmacist)\b", re.I), ["gp", "community", "hospital", "clinic"]),
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
                      r"\bcare (worker|assistant)|carer|support worker|care co-?ordinator|\bgps?\b|general practitioner|"
                      r"\bdoctor|physician|pharmacist|physio|occupational therapist|paramedic|podiatrist|dietitian|"
                      r"dietician|radiographer|sonographer|speech (and language )?therapist|health visitor|midwi", re.I)

# names that mark the DIRECT employers a LOCUM / bank worker is booked by: urgent care and out-of-hours providers, PCNs
# and GP federations / alliances / extended-access hubs, hospices, and NHS trusts' own staff banks. Recruitment, staffing
# and locum AGENCIES are never listed (see recruit_scraper/agencies.py).
LOCUM_EMPLOYER = re.compile(r"out[- ]of[- ]hours|\booh\b|urgent care|walk-?in|federation|\bpcn\b|primary care network|"
                            r"gp (alliance|collaborative|cooperative|co-operative)|extended access|\bhub\b|\bhospice\b|"
                            r"in-house bank|staff bank|nhs (foundation )?trust|foundation trust|health board", re.I)


def locum_priority(lead: "EmployerLead") -> int:
    """0 for the direct employers that book locum / bank staff in bulk (urgent care, OOH, PCNs / federations, hubs, hospices,
    trusts' own banks); 1 for the rest (GP practices, independent clinics, care homes - all direct employers too)."""
    return 0 if LOCUM_EMPLOYER.search(f"{lead.employer_type} {lead.name}") else 1


def locum_rank(lead: "EmployerLead") -> int:
    """0 an advert for locum / bank staff is open now · 1 a staff bank / locum page, or the kind of direct employer that
    books locums in bulk (urgent care, OOH, PCNs / federations, hospices, trusts' own banks) · 2 the rest."""
    if lead.hires_locums >= 3:
        return 0
    if lead.hires_locums == 2 or locum_priority(lead) == 0:
        return 1
    return 2


_PRIVATE_TITLE = re.compile(r"\b(private|independent|self-?pay)\b", re.I)
_CLINICAL_KINDS = {"gp", "hospital", "clinic", "urgent_care", "community", "mental_health"}


def is_locum_search(title: str) -> bool:
    from .matching import contract_type
    return contract_type(title or "") == "temporary" and bool(_NURSING.search(title or ""))


def employer_kinds(title: str) -> list[str]:
    from .matching import contract_type
    for rx, kinds in ROLE_EMPLOYERS:
        if rx.search(title or ""):
            kinds = list(kinds)
            if is_locum_search(title):
                # locum / bank nursing: also out-of-hours / urgent care providers (direct employers; agencies are never listed)
                kinds += [k for k in ("urgent_care", "trust") if k not in kinds]
            if _CLINICAL_KINDS & set(kinds) and "dental" not in kinds:
                # independent / private GP services and clinics employ these roles too ('private practice nurse' first)
                kinds = ["independent"] + kinds if _PRIVATE_TITLE.search(title or "") else kinds + ["independent"]
            return kinds
    return []


# which employers come first for a role: the core employer (GP practices for a practice nurse), then the rest
def _tier(lead: "EmployerLead", kinds: list[str]) -> int:
    t = (lead.employer_type or "").lower()
    order = {"gp": r"gp practice|doctors", "urgent_care": r"urgent care|walk-in|out-of-hours",
             "independent": r"independent|private|aesthetic|occupational|travel / vaccination|online / telephone",
             "trust": r"nhs trust|health board", "mental_health": r"mental health|learning disab", "community": r"community (nursing|health)",
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
    sector: str = ""          # 'NHS' | 'Independent' | '' (health sector split; blank for care homes, schools, firms)
    hires_locums: int = 0     # 0 no evidence · 1 its own site mentions locum / bank / sessional staff · 2 it has a bank / locum
                              # page · 3 it has a locum / bank / sessional advert open now
    locum_evidence: str = ""  # the words / advert that show it, as the employer published them
    locum_evidence_url: str = ""   # the page or advert that says so (source link)

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
        if o.sector and not hit.sector:
            hit.sector = o.sector
        if o.source not in hit.source:
            hit.source += f" + {o.source}"
            hit.source_url = f"{hit.source_url} {o.source_url}".strip()
    return out


_NHS_SOURCE = re.compile(r"NHS ODS|PHS|HSC BSO", re.I)
_NHS_TYPE = re.compile(r"gp practice|doctors|community (nursing|health)|urgent care|walk-in|out-of-hours|nhs|"
                       r"nurse prescriber", re.I)


def assign_sector(lead: EmployerLead) -> str:
    """'NHS' for NHS-contracted GP practices / NHS bodies (they come from the NHS ODS / PHS / BSO lists or have NHS in
    the name), 'Independent' for private / independent providers from the regulators' registers, '' when the
    NHS-or-independent split does not apply (care homes, schools, law firms…)."""
    if lead.sector == "Independent" and not (_NHS_SOURCE.search(lead.source) and _NHS_TYPE.search(lead.employer_type)):
        return "Independent"
    if re.search(r"\bNHS\b|foundation trust|health board", lead.name, re.I):
        return "NHS"
    if _NHS_SOURCE.search(lead.source) and _NHS_TYPE.search(lead.employer_type):
        return "NHS"       # an NHS-listed GP practice is NHS-contracted even when CQC also lists it
    return lead.sector


def drop_agencies(leads: list[EmployerLead]) -> tuple[list[EmployerLead], int]:
    """Recruitment / staffing / locum agencies are other recruiters, not employers: never listed."""
    kept = [l for l in leads if not is_agency_employer(l.name, l.employer_type)]
    return kept, len(leads) - len(kept)


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
                 practice_seconds: float = 60, locum_adverts: bool = True, advert_seconds: float = 30.0,
                 uk_trusts: int = 45):
        self.locum_adverts, self.advert_seconds, self.uk_trusts = locum_adverts, advert_seconds, uk_trusts
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
        self.site_jobs: list[tuple["EmployerLead", dict]] = []   # (employer, vacancy) read off employers' own sites
        self.stats = {"ods_records": 0, "osm_places": 0, "with_phone": 0, "with_website": 0, "with_email": 0}

    # --- NHS ODS -------------------------------------------------------------------------------------------
    def _cached_json(self, url: str, params: Optional[dict] = None, http: Optional[HttpClient] = None):
        key = url + "?" + "&".join(f"{k}={v}" for k, v in sorted((params or {}).items()))
        hit = self.cache.get(key)
        if hit is not None:
            return hit
        data = (http or self.http).get_json(url, params=params)
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
                    v = (c.get("value") or "").strip().lower()      # ODS publishes these in capitals
                    l.website = v if v.startswith("http") else f"https://{v}" if v else ""
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
                els = overpass_elements(self.osm_http, q, budget=min(30.0, max(5.0, self._left() * 0.3)))
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
                ods_code=t.get("ref:ODS") or t.get("ref:GB:nhs") or "", lat=lat, lon=lon,
                sector="Independent" if t.get("operator:type") in ("private", "commercial") else ""))
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
                                sector=r.sector,
                                note=("Named in an official register - a professional at this workplace, not a "
                                      "job-seeker" if r.named_person else "Employer - not a candidate"))
            (named if r.named_person else emp).append(lead)
        return emp, named

    # --- the practice's own website ----------------------------------------------------------------------------
    def _cap(self, leads: list[EmployerLead], kinds: list[str]) -> list[EmployerLead]:
        """First max_leads, but each other kind asked for (independent clinics, urgent care for a locum search) keeps a
        share, so 250 GP practices can't push every other kind out of the list."""
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
                self.site_jobs.extend((l, v) for v in got["vacancies"])     # every matching advert (Jobs page source)
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
            if self._locum_evidence(l, got, src):
                found = True
            if found and "employer website" not in l.source:
                l.source += " + employer website"
                l.source_url = f"{l.source_url} {src}".strip()
        self.stats.update(practice_sites_checked=len(done), practice_site_emails=n_e, practice_site_managers=n_m,
                          site_phones=n_p, site_careers_pages=n_c, site_vacancies=n_v, site_locum_vacancies=n_l,
                          site_bank_pages=n_b)
        if len(done) < len(todo):
            self.notes.append(f"employer websites: {len(todo) - len(done)} not read (time limit)")

    @staticmethod
    def _locum_evidence(l: EmployerLead, got: dict, src: str) -> bool:
        """What the employer's OWN site shows about locum / bank / sessional hiring: an open locum advert (3), a bank /
        locum page (2), or a sentence on its site that says it uses such staff (1). The strongest evidence wins."""
        vac = next((v for v in got.get("vacancies") or [] if v.get("type") == "locum / bank / temporary"), None)
        if vac and l.hires_locums < 3:
            l.hires_locums, l.locum_evidence = 3, f"Open now: {vac['title']}"
            l.locum_evidence_url = vac.get("url") or vac.get("page") or src
            return True
        if got.get("bank_url") and l.hires_locums < 2:
            l.hires_locums, l.locum_evidence = 2, "Has a staff bank / locum page on its own website"
            l.locum_evidence_url = got["bank_url"]
            return True
        if got.get("locum_cue") and l.hires_locums < 1:
            l.hires_locums, l.locum_evidence = 1, got["locum_cue"]
            l.locum_evidence_url = got.get("locum_cue_url") or src
            return True
        return False

    # --- locum evidence: direct employers' own adverts, trusts' own banks ---------------------------------------
    def _start_adverts(self, title: str, place: Place, radius_miles: float):
        """For a locum / bank search: read the employers' own locum / bank adverts (NHS Jobs, NHS Scotland, HSC NI) in the
        background while the registers load. Returns a holder the result is collected from."""
        if not is_locum_search(title) or not self.locum_adverts:
            return None
        box: dict = {"jobs": [], "done": threading.Event()}
        nations = None if place.nationwide else {place.country or "England"}
        loc = "UK" if place.nationwide else place.postcode

        def run():
            from .locum import locum_adverts
            try:
                box["jobs"] = locum_adverts(title, loc, radius_miles, seconds=min(self.advert_seconds, max(10.0, self._left() * 0.35)),
                                            nations=nations)
            except BaseException as e:  # noqa: BLE001
                log.info("locum adverts: %s", e)
            finally:
                box["done"].set()
        threading.Thread(target=run, daemon=True).start()
        return box

    def _attach_adverts(self, leads: list[EmployerLead], box, title: str) -> None:
        if not box:
            return
        box["done"].wait(max(1.0, min(self._left() * 0.3, 25.0)))
        jobs = list(box["jobs"])
        if not box["done"].is_set():
            self.notes.append("locum adverts: stopped at the time limit (partial)")
        from .locum import attach_adverts
        flagged, added = attach_adverts(leads, jobs, EmployerLead)
        for l in leads:
            if l.hires_locums >= 3 and not l.sector:
                l.sector = assign_sector(l)
        self.enrich_trusts(leads)
        self.stats.update(locum_adverts=len(jobs), locum_adverts_matched=flagged, locum_adverts_new_employers=added)

    def trust_leads_uk(self, progress: Callable[[str], None]) -> list[EmployerLead]:
        """NHS trusts (they run their own staff banks) from the NHS ODS API, with the phone and website the NHS publishes
        for each; the contact details are looked up for a spread of trusts across the country."""
        progress("NHS ODS: NHS trusts (their own staff banks)…")
        data = self._cached_json(ODS, {"Roles": ODS_ROLES["trust"], "Status": "Active", "Limit": 1000}) or {}
        recs = [o for o in data.get("Organisations") or [] if o.get("PrimaryRoleId") == "RO197"]
        recs.sort(key=lambda o: (re.sub(r"\d.*", "", o.get("PostCode", "")), o.get("Name", "")))
        step = max(1, len(recs) // max(1, self.uk_trusts))
        leads = [EmployerLead(name=_title(o.get("Name", "")), employer_type=TYPE_LABELS["trust"],
                              postcode=o.get("PostCode", ""), source="NHS ODS", ods_code=o["OrgId"],
                              source_url=f"{ODS}/{o['OrgId']}", sector="NHS") for o in recs[::step][: self.uk_trusts]]
        self.stats["trusts_uk"] = len(leads)
        return leads

    _ADVERT_SRC = ("NHS Jobs", "NHS Scotland Jobs", "HSC NI jobs", "job advert")

    def enrich_trusts(self, leads: list[EmployerLead]) -> None:
        """Phone + website the NHS publishes (ODS record) for trusts, and for employers found only from their own locum /
        bank advert, matched to the ODS record by exact name (a single trust match is enough for trusts). Time-boxed."""
        todo = [l for l in leads if not (l.phone and l.website) and
                (l.employer_type == TYPE_LABELS["trust"] or
                 (l.hires_locums >= 3 and not l.ods_code and l.source.split(" + ")[0] in self._ADVERT_SRC))]
        todo.sort(key=lambda l: l.employer_type == TYPE_LABELS["trust"] and l.hires_locums < 3)   # their own advert first
        todo = todo[:90]
        if not todo:
            return
        end = time.monotonic() + min(25.0, max(12.0, self._left() * 0.3))
        ods_http = HttpClient(min_interval=0.15, timeout=15, retries=1, backoff=1)   # the NHS directory API is open and quick

        def one(l: EmployerLead):
            if time.monotonic() > end:
                return None
            try:
                oid = l.ods_code
                if not oid:
                    trust = l.employer_type == TYPE_LABELS["trust"]
                    q = {"Name": l.name, "Status": "Active", "Limit": 8}
                    if trust:
                        q["Roles"] = ODS_ROLES["trust"]
                    cands = (self._cached_json(ODS, q, http=ods_http) or {}).get("Organisations") or []
                    nk = _name_key(l.name)
                    best = next((o for o in cands if nk and _name_key(o.get("Name", "")) == nk), None) or \
                        (cands[0] if trust and len(cands) == 1 else None)
                    if not best:
                        return None
                    oid = best["OrgId"]
                    l_pc = best.get("PostCode", "")
                else:
                    l_pc = ""
                org = (self._cached_json(f"{ODS}/{oid}", http=ods_http) or {}).get("Organisation") or {}
                return oid, l_pc, org
            except (requests.RequestException, ValueError, KeyError) as e:
                log.info("ODS lookup failed %s: %s", l.name, e)
                return None
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(one, todo))
        for l, got in zip(todo, results):
            if not got:
                continue
            oid, pc, org = got
            l.ods_code = l.ods_code or oid
            l.postcode = l.postcode or pc
            l.source_url = l.source_url or f"{ODS}/{oid}"
            for c in ((org.get("Contacts") or {}).get("Contact") or []):
                if c.get("type") == "tel" and not l.phone:
                    l.phone = c.get("value", "")
                elif c.get("type") in ("http", "https", "url") and not l.website:
                    v = (c.get("value") or "").strip().lower()      # ODS publishes these in capitals
                    l.website = v if v.startswith("http") else f"https://{v}" if v else ""
            if "NHS ODS" not in l.source:
                l.source += " + NHS ODS"
                l.source_url = f"{l.source_url} {ODS}/{oid}".strip()

    # --- both ------------------------------------------------------------------------------------------------
    def find(self, title: str, place: Place, radius_miles: float, entries: Iterable[str] = (),
             progress: Callable[[str], None] = lambda m: None) -> tuple[list[EmployerLead], int]:
        kinds = employer_kinds(title)
        if not kinds:
            return [], 0
        self._deadline = time.monotonic() + self.budget_seconds
        ods: list[EmployerLead] = []
        osm: list[EmployerLead] = []
        adverts_box = self._start_adverts(title, place, radius_miles)
        if place.nationwide and "trust" in kinds:
            ods = self._bounded("NHS trusts", min(25.0, self._left() * 0.2), lambda: self.trust_leads_uk(progress), [])
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
        self._attach_adverts(leads, adverts_box, title)
        # direct employers only: recruitment / staffing / locum agencies are other recruiters
        leads, n_ag = drop_agencies(leads)
        named, n_ag2 = drop_agencies(named)
        self.stats["agencies_removed"] = n_ag + n_ag2 + int(self.stats.pop("register_agencies", 0) or 0)
        for l in leads + named:
            l.sector = assign_sector(l)
        leads, n = suppress_leads(leads, entries)
        named, n2 = suppress_leads(named, entries)
        n += n2
        # the right kind of employer first, then nearest (in 3-mile bands) and most contactable
        locum = is_locum_search(title)   # a locum / bank search: urgent care, OOH, PCNs / federations, hospices first
        if locum:
            kinds = ["urgent_care"] + [k for k in kinds if k != "urgent_care"]
        tiered = len(kinds) <= 3 or locum   # a broad title ('nurse') keeps every kind of employer mixed by distance
        leads.sort(key=lambda l: (locum_rank(l) if locum else 0, _tier(l, kinds) if tiered else 0,
                                  l.distance_miles is None, int((l.distance_miles or 0) // 3),
                                  -contactability(l), l.distance_miles or 0, l.name))
        named.sort(key=lambda l: (l.distance_miles is None, l.distance_miles or 0, l.name))
        leads = self._cap(leads, kinds if tiered else [])
        if self.practice_sites:
            self.practice_sites_contacts(leads, progress, title)
            # employers advertising this role right now first, a locum / bank advert before a permanent one (stable)
            leads.sort(key=lambda l: (-(l.hires_locums if locum else 0), not l.vacancy,
                                      l.vacancy_type != "locum / bank / temporary" if locum else 0))
            leads, n3 = suppress_leads(leads, entries)   # a newly found email may be on the do-not-contact list
            n += n3
        leads = leads + named[: self.max_named]
        self.stats.update(with_phone=sum(1 for l in leads if l.phone), with_website=sum(1 for l in leads if l.website),
                          with_email=sum(1 for l in leads if l.email))
        return leads, n
