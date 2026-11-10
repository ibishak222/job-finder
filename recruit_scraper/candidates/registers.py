"""Official UK open-data registers (free, no key) as 'Employers to contact' and NAMED PROFESSIONAL CONTACTS.

Everything here is an organisation that employs this kind of worker, or a professional named in a public register
(a care service's registered manager, a nurse prescriber, a barrister, a headteacher). None of them are job-seekers
and the app never says they are. Every row shows the register it came from; the do-not-contact list applies.

Registers (each only downloaded when the role needs it, streamed - never loaded whole - filtered to the postcode
areas around the search, and the filtered subset cached on disk for 7 days):
* CQC care directory (weekly CSV, England)                - care homes, home care, GPs, dentists: phone + website
* Care Inspectorate Scotland datastore (monthly CSV)       - care services, nurse agencies: manager, phone, email
* NHS ODS CSV reports: epraccur (GP practices + phone), ecarehomesite (fallback), enurse (named nurse prescribers)
* FSA Food Hygiene Rating API (no key)                     - restaurants, pubs, hotels for chef / kitchen roles
* DfE Get Information About Schools (daily CSV, England)  - schools: phone, website, headteacher's name
* Charity Commission register extract (England & Wales)   - care / education charities: published email, phone
* Bar Standards Board register of practising barristers   - chambers address + phone, areas of practice
Not used: OSCR's register download (its terms forbid direct marketing); CQC registered managers (only in the
monthly ODS spreadsheet - too heavy to parse on Streamlit Cloud); Companies House bulk data (472 MB zip with no
phone or email - see README).
"""
from __future__ import annotations

import csv
import io
import logging
import re
import tempfile
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable, Iterable, Iterator, Optional

import requests

from ..http import HttpClient
from ..utils import find_emails
from .cache import DiskCache
from .geo import Place, haversine_miles

log = logging.getLogger(__name__)
csv.field_size_limit(10_000_000)

TTL = 7 * 86400
DOWNLOAD_BUDGET = 60.0          # seconds per register download + parse
TOTAL_BUDGET = 150.0            # seconds for all registers in one search
MAX_BYTES = 200 * 1024 * 1024   # never download more than this for one register

ODS_REPORT = "https://www.odsdatasearchandexport.nhs.uk/api/getReport?report="
CQC_PAGE = "https://www.cqc.org.uk/about-us/transparency/using-cqc-data"
CI_PAGE = "https://www.careinspectorate.scot/resources-data/data-and-statistics/datastore"
GIAS = "https://ea-edubase-api-prod.azurewebsites.net/edubase/downloads/public/edubasealldata{d}.csv"
CHARITY_ZIP = "https://ccewuksprdoneregsadata1.blob.core.windows.net/data/txt/publicextract.charity.zip"
BSB_PAGE = "https://www.barstandardsboard.org.uk/for-the-public/search-a-barristers-record/the-barristers-register.html"
FSA_API = "https://api.ratings.food.gov.uk/Establishments"

LICENCES = {
    "CQC": "CQC data (Open Government Licence v3.0)",
    "Care Inspectorate": "Care Inspectorate datastore (Open Government Licence v3.0)",
    "NHS ODS": "NHS Organisation Data Service (Open Government Licence v3.0)",
    "FSA": "Food Standards Agency food hygiene ratings (Open Government Licence v3.0)",
    "DfE GIAS": "DfE Get Information about Schools (Open Government Licence v3.0)",
    "Charity Commission": "Charity Commission register of charities (Open Government Licence v3.0)",
    "BSB": "Bar Standards Board Barristers' Register",
}


class RegisterUnavailable(RuntimeError):
    pass


def pc_norm(s: str) -> str:
    s = re.sub(r"\s+", "", (s or "").upper())
    return f"{s[:-3]} {s[-3:]}" if len(s) > 4 else s


def outcode(pc: str) -> str:
    return pc_norm(pc).split(" ")[0]


def area(oc: str) -> str:
    m = re.match(r"[A-Z]+", oc or "")
    return m.group(0) if m else ""


def _title(s: str) -> str:
    return " ".join(w if (w.isupper() and len(w) <= 3 and w not in {"THE", "AND"}) or any(c.isdigit() for c in w)
                    else w.capitalize() for w in (s or "").split())


def _person(s: str, initials_last: bool = False) -> str:
    """'JANE DOE' -> 'Jane Doe'; NHS 'SMITH AB' -> 'Smith AB' (surname then initials)."""
    w = (s or "").split()
    return " ".join(x if (initials_last and i == len(w) - 1 and len(x) <= 3) or not x.isupper() else x.capitalize()
                    for i, x in enumerate(w))


def _phone(s: str) -> str:
    s = (s or "").strip().replace("(+44) ", "")
    d = re.sub(r"\D", "", s)
    if 9 <= len(d) <= 11 and not s.startswith("0") and not s.startswith("+"):
        s = "0" + d if len(d) == 10 else s        # CQC drops the leading 0 ('2073770990')
    return s


def _site(s: str) -> str:
    s = (s or "").strip()
    if not s or " " in s or "." not in s:
        return ""
    return s if s.lower().startswith("http") else "https://" + s.lstrip("/")


@dataclass
class RegRow:
    name: str
    employer_type: str
    postcode: str
    address: str = ""
    phone: str = ""
    email: str = ""
    website: str = ""
    contact_name: str = ""
    contact_role: str = ""
    source: str = ""
    source_url: str = ""
    ods_code: str = ""
    lat: Optional[float] = None
    lon: Optional[float] = None
    distance: Optional[float] = None
    tags: str = ""            # register-specific filter text (service types, practice areas, activities)
    named_person: bool = False

    def to_dict(self) -> dict:
        return dict(self.__dict__)


# --- streaming helpers ---------------------------------------------------------------------------------------

class _Budget:
    def __init__(self, seconds: float):
        self.end = time.monotonic() + seconds

    def check(self) -> None:
        if time.monotonic() > self.end:
            raise RegisterUnavailable("took too long - skipped for this search")


def stream_csv(http: HttpClient, url: str, budget: _Budget, encoding: str = "utf-8-sig",
               delimiter: str = ",") -> Iterator[list[str]]:
    """Rows of a CSV read straight off the wire (constant memory)."""
    try:
        r = http.session.get(url, stream=True, timeout=(15, 30), headers={"Accept": "text/csv,*/*"})
        r.raise_for_status()
    except requests.RequestException as e:
        raise RegisterUnavailable(f"{type(e).__name__}") from e
    r.raw.decode_content = True
    r.raw.auto_close = False          # else urllib3 closes the stream at EOF and TextIOWrapper errors
    text = io.TextIOWrapper(r.raw, encoding=encoding, errors="replace", newline="")
    try:
        for i, row in enumerate(csv.reader(text, delimiter=delimiter)):
            if i % 2000 == 0:
                budget.check()
            yield row
    except (requests.RequestException, OSError, ValueError) as e:  # connection dropped mid-file
        raise RegisterUnavailable(type(e).__name__) from e
    finally:
        r.close()


def stream_zip_member(http: HttpClient, url: str, budget: _Budget, encoding: str = "utf-8-sig",
                      delimiter: str = "\t") -> Iterator[list[str]]:
    """A zip has its index at the end, so it is streamed to a temp file (not memory), then read row by row."""
    with tempfile.TemporaryFile() as tmp:
        try:
            with http.session.get(url, stream=True, timeout=(15, 30)) as r:
                r.raise_for_status()
                n = 0
                for chunk in r.iter_content(1 << 20):
                    n += len(chunk)
                    if n > MAX_BYTES:
                        raise RegisterUnavailable("file larger than expected - skipped")
                    tmp.write(chunk)
                    budget.check()
        except requests.RequestException as e:
            raise RegisterUnavailable(type(e).__name__) from e
        tmp.seek(0)
        with zipfile.ZipFile(tmp) as z:
            member = max(z.infolist(), key=lambda m: m.file_size)
            with z.open(member) as f:
                text = io.TextIOWrapper(f, encoding=encoding, errors="replace", newline="")
                for i, row in enumerate(csv.reader(text, delimiter=delimiter, quoting=csv.QUOTE_NONE)):
                    if i % 5000 == 0:
                        budget.check()
                    yield row


def latest_link(http: HttpClient, page: str, pattern: str) -> str:
    r = http.get(page)
    m = re.findall(pattern, r.text)
    if not m:
        raise RegisterUnavailable("download link not found on the publisher's page")
    return m[0] if m[0].startswith("http") else requests.compat.urljoin(page, m[0])


CI_CSV = "https://ci-web-media-production-bucket.s3.eu-west-2.amazonaws.com/assets/resources/datastore/{d}datastoreexternal.csv"


def ci_latest_csv(http: HttpClient) -> str:
    """The monthly file is named by the month's last day (260930datastoreexternal.csv); newest that exists."""
    d = date.today().replace(day=1) - timedelta(days=1)
    for _ in range(5):
        url = CI_CSV.format(d=d.strftime("%y%m%d"))
        try:
            r = http.session.head(url, timeout=15)
            if r.status_code == 200:
                return url
        except requests.RequestException:
            pass
        d = d.replace(day=1) - timedelta(days=1)
    raise RegisterUnavailable("no recent datastore file")


# --- registers -----------------------------------------------------------------------------------------------

class Register:
    key = ""
    label = ""
    kinds: set[str] = set()

    def applies(self, kinds: list[str], title: str) -> bool:
        return bool(self.kinds & set(kinds))

    def download(self, http: HttpClient, keep: Callable[[str], bool], budget: _Budget) -> Iterator[RegRow]:
        raise NotImplementedError

    def wanted(self, row: RegRow, kinds: list[str], title: str) -> bool:
        return True


CQC_TYPES = {"gp": ("Doctors/GPs",), "care_home": ("Residential homes", "Nursing homes"),
             "home_care": ("Homecare agencies", "Supported living", "Shared lives"), "dental": ("Dentist",),
             "hospital": ("Hospital", "Hospice"), "clinic": ("Clinic", "Community services - Nursing",
                                                              "Community services - Healthcare")}
CQC_LABEL = {"Doctors/GPs": "GP practice", "Residential homes": "Care home", "Nursing homes": "Nursing home",
             "Homecare agencies": "Home care agency", "Supported living": "Supported living service",
             "Shared lives": "Shared lives scheme", "Dentist": "Dental practice", "Hospital": "Hospital",
             "Hospice": "Hospice", "Clinic": "Clinic", "Community services - Nursing": "Community nursing service",
             "Community services - Healthcare": "Community health service"}


class CQCDirectory(Register):
    key, label, kinds = "cqc", "CQC", {"gp", "care_home", "home_care", "dental", "hospital", "clinic"}

    def download(self, http, keep, budget):
        url = latest_link(http, CQC_PAGE, r'href="(https://www\.cqc\.org\.uk/system/files/[^"]+CQC_directory\.csv)"')
        header = None
        for row in stream_csv(http, url, budget):
            if header is None:
                if row and row[0] == "Name" and "Postcode" in row:   # skip the title / date preamble rows
                    header = {h: i for i, h in enumerate(row)}
                continue
            if len(row) < len(header):
                continue
            g = lambda h: row[header[h]].strip() if h in header else ""
            pc = pc_norm(g("Postcode"))
            if not keep(outcode(pc)):
                continue
            types = g("Service types")
            first = next((CQC_LABEL[t] for t in types.split("|") if t in CQC_LABEL), types.split("|")[0])
            yield RegRow(name=g("Name"), employer_type=first, postcode=pc, address=g("Address").replace(",", ", "),
                         phone=_phone(g("Phone number")), website=_site(g("Service's website (if available)")),
                         source="CQC", source_url=g("Location URL"), tags=types)

    def wanted(self, row, kinds, title):
        types = row.tags.split("|")
        hit = next((t for k in kinds for t in CQC_TYPES.get(k, ()) if t in types), None)
        if hit:
            row.employer_type = CQC_LABEL.get(hit, hit)   # label by the service type that matched the role
        return bool(hit)


class CareInspectorate(Register):
    key, label, kinds = "careinsp", "Care Inspectorate", {"care_home", "home_care", "hospital", "clinic", "gp"}

    def download(self, http, keep, budget):
        url = ci_latest_csv(http)
        header = None
        for row in stream_csv(http, url, budget, encoding="cp1252"):
            if header is None:
                header = {h: i for i, h in enumerate(row)}
                continue
            g = lambda h: row[header[h]].strip() if h in header and header[h] < len(row) else ""
            # childminders work from home - never listed; services not on the public list are skipped too
            if g("CareService") == "Child Minding" or g("PublicList") != "Yes" or g("ServiceStatus") != "Active":
                continue
            pc = pc_norm(g("Service_Postcode"))
            if not keep(outcode(pc)):
                continue
            kind = g("Subtype") if g("CareService") == "Care Home Service" else g("CareService")
            label = {"Care Home Service": "Care home"}.get(g("CareService"), g("CareService"))
            yield RegRow(name=g("ServiceName"), employer_type=f"{label} ({kind})" if kind and kind != label else label,
                         postcode=pc, address=", ".join(filter(None, [g("Address_line_1"), g("Address_line_2"),
                                                                       g("Service_town")])),
                         phone=g("Service_Phone_Number"), email=(find_emails(g("Eforms_email_address")) or [""])[0],
                         contact_name=_person(g("Manager_Name")),
                         contact_role="Registered manager (Care Inspectorate)" if g("Manager_Name") else "",
                         source="Care Inspectorate",
                         source_url=f"https://www.careinspectorate.scot/find-care/search-for-care/care-service-profiles?"
                                    f"service={g('CSNumber')}", tags=f"{g('CareService')}|{g('Subtype')}")

    def wanted(self, row, kinds, title):
        t = row.tags
        nurse = re.search(r"\bnurs|\brgn\b|\brmn\b", title, re.I)
        return (("care_home" in kinds and t.startswith("Care Home Service"))
                or ("home_care" in kinds and (t.startswith("Housing Support") or "Care at Home" in t))
                or (bool(nurse) and (t.startswith("Nurse Agency") or t.startswith("Care Home Service|Older"))))


class ODSPractices(Register):
    key, label, kinds = "epraccur", "NHS ODS", {"gp"}

    def download(self, http, keep, budget):
        for row in stream_csv(http, ODS_REPORT + "epraccur", budget, encoding="cp1252"):
            if len(row) < 26 or row[12] != "ACTIVE" or row[25] != "RO76":
                continue
            pc = pc_norm(row[9])
            if keep(outcode(pc)):
                yield RegRow(name=_title(row[1]), employer_type="GP practice", postcode=pc,
                             address=", ".join(_title(x) for x in row[4:8] if x), phone=row[17], ods_code=row[0],
                             source="NHS ODS", source_url=ODS_REPORT + "epraccur")


class ODSCareHomes(Register):
    """Fallback only (no phone numbers): used when the CQC directory couldn't be read."""
    key, label, kinds = "ecarehomesite", "NHS ODS", {"care_home"}

    def download(self, http, keep, budget):
        for row in stream_csv(http, ODS_REPORT + "ecarehomesite", budget, encoding="cp1252"):
            if len(row) < 12 or row[11]:          # column 12 = close date
                continue
            pc = pc_norm(row[9])
            if keep(outcode(pc)):
                yield RegRow(name=_title(row[1]), employer_type="Care home", postcode=pc,
                             address=", ".join(_title(x) for x in row[4:8] if x), ods_code=row[0],
                             source="NHS ODS", source_url=ODS_REPORT + "ecarehomesite")


class ODSNursePrescribers(Register):
    """Named nurse prescribers and the practice phone they're registered at (NHS ODS 'enurse', England)."""
    key, label, kinds = "enurse", "NHS ODS", {"gp", "clinic", "hospital"}
    per_org = 3

    def applies(self, kinds, title):
        return bool(re.search(r"\bnurs(e|es|ing)\b|\brgn\b|\bprescrib", title, re.I)) and super().applies(kinds, title)

    def download(self, http, keep, budget):
        for row in stream_csv(http, ODS_REPORT + "enurse", budget, encoding="cp1252"):
            if len(row) < 15 or not row[7]:
                continue
            pc = pc_norm(row[13])
            if keep(outcode(pc)):
                yield RegRow(name=_title(row[8]), employer_type="Nurse prescriber's workplace", postcode=pc,
                             address=", ".join(_title(x) for x in row[9:12] if x), phone=row[14],
                             contact_name=_person(row[7], initials_last=True),
                             contact_role="Nurse prescriber (NHS ODS register)",
                             source="NHS ODS", source_url=f"{ODS_REPORT}enurse#{row[2]}", tags=row[1],
                             named_person=True)


class GIASSchools(Register):
    key, label, kinds = "gias", "DfE GIAS", {"school"}

    def download(self, http, keep, budget):
        last = None
        for back in range(0, 8):              # the file is published daily, named by date
            url = GIAS.format(d=(date.today() - timedelta(days=back)).strftime("%Y%m%d"))
            try:
                header = None
                for row in stream_csv(http, url, budget, encoding="cp1252"):
                    if header is None:
                        if not row or row[0] != "URN":
                            raise RegisterUnavailable("not a GIAS file")
                        header = {h: i for i, h in enumerate(row)}
                        continue
                    g = lambda h: row[header[h]].strip() if h in header and header[h] < len(row) else ""
                    if g("EstablishmentStatus (name)") != "Open":
                        continue
                    pc = pc_norm(g("Postcode"))
                    if not keep(outcode(pc)):
                        continue
                    head = " ".join(filter(None, [g("HeadTitle (name)").replace("Not applicable", ""),
                                                  g("HeadFirstName"), g("HeadLastName")])).strip()
                    yield RegRow(name=g("EstablishmentName"), employer_type=f"School ({g('PhaseOfEducation (name)')})"
                                 if g("PhaseOfEducation (name)") not in ("", "Not applicable") else
                                 g("TypeOfEstablishment (name)") or "School", postcode=pc,
                                 address=", ".join(filter(None, [g("Street"), g("Locality"), g("Town")])),
                                 phone=g("TelephoneNum"), website=_site(g("SchoolWebsite")),
                                 contact_name=head, contact_role=(g("HeadPreferredJobTitle") or "Headteacher")
                                 + " (DfE GIAS)" if head else "", source="DfE GIAS",
                                 source_url=f"https://get-information-schools.service.gov.uk/Establishments/"
                                            f"Establishment/Details/{g('URN')}",
                                 tags=f"{g('PhaseOfEducation (name)')}|{g('TypeOfEstablishment (name)')}")
                return
            except RegisterUnavailable as e:
                last = e
                if header is not None:        # failed part-way through a real file: don't try older ones
                    raise
        raise RegisterUnavailable(f"no recent GIAS file ({last})")

    def wanted(self, row, kinds, title):
        t = title.lower()
        phase = row.tags.split("|")[0].lower()
        if "primary" in t:
            return "primary" in phase or "all-through" in phase
        if "secondary" in t or "sixth form" in t:
            return "secondary" in phase or "16 plus" in phase or "all-through" in phase
        if "nursery" in t or "early years" in t:
            return "nursery" in phase or "primary" in phase
        return True


_CARE_CHARITY = re.compile(r"\b(care home|nursing home|residential care|home care|domiciliary|dementia|elderly|older "
                           r"people|disabilit|learning difficult|respite|hospice|carers?)\b", re.I)


class CharityRegister(Register):
    # care charities only: for schools, GIAS is better, and a charity's registered contact is often its accountant
    key, label, kinds = "charities", "Charity Commission", {"care_home", "home_care"}

    def download(self, http, keep, budget):
        header = None
        for row in stream_zip_member(http, CHARITY_ZIP, budget):
            if header is None:
                header = {h: i for i, h in enumerate(row)}
                continue
            g = lambda h: row[header[h]].strip() if h in header and header[h] < len(row) else ""
            if g("charity_registration_status") != "Registered" or g("linked_charity_number") not in ("0", ""):
                continue
            pc = pc_norm(g("charity_contact_postcode"))
            if not keep(outcode(pc)):
                continue
            what = f"{g('charity_name')} {g('charity_activities')}"
            tag = "care" if _CARE_CHARITY.search(what) else ""
            if not tag or not (g("charity_contact_email") or g("charity_contact_phone")):
                continue
            yield RegRow(name=_title(g("charity_name")), employer_type=f"Charity ({tag})", postcode=pc,
                         address=", ".join(_title(g(f"charity_contact_address{i}")) for i in range(1, 6)
                                           if g(f"charity_contact_address{i}")),
                         phone=g("charity_contact_phone"), email=(find_emails(g("charity_contact_email")) or [""])[0],
                         website=_site(g("charity_contact_web")), source="Charity Commission",
                         source_url=f"https://register-of-charities.charitycommission.gov.uk/charity-search/-/charity-"
                                    f"details/{g('registered_charity_number')}", tags=tag)

    def wanted(self, row, kinds, title):
        return row.tags == "care"


class BarristersRegister(Register):
    key, label, kinds = "bsb", "BSB", {"barristers"}

    def download(self, http, keep, budget):
        url = latest_link(http, BSB_PAGE, r'href="(https://www\.barstandardsboard\.org\.uk/asset/[^"]+)"\s+download')
        header = None
        for row in stream_csv(http, url, budget):
            if header is None:
                header = {h: i for i, h in enumerate(row)}
                continue
            g = lambda h: row[header[h]].strip() if h in header and header[h] < len(row) else ""
            pc = pc_norm(g("Address Postcode"))
            if g("Address Country") not in ("United Kingdom", "") or not keep(outcode(pc)):
                continue
            # disciplinary-finding columns are deliberately not read
            name = " ".join(filter(None, [g("Title"), g("Forenames"), g("Surname"), g("Honours")]))
            yield RegRow(name=g("Address Name") or "Chambers", employer_type=f"Barristers' chambers ({g('Primary status')})",
                         postcode=pc, address=", ".join(filter(None, [g("Address Floor Street"), _title(g("Address Town"))])),
                         phone=g("Address Tel").replace("(+44) ", ""), contact_name=name,
                         contact_role=f"Barrister, called {g('Date of call')} (BSB register)", source="BSB",
                         source_url=BSB_PAGE + "#" + re.sub(r"\W+", "-", name.lower()).strip("-"),
                         tags=g("Areas of practice"), named_person=True)

    def wanted(self, row, kinds, title):
        if re.search(r"\bfamily\b", title, re.I):
            return "family" in row.tags.lower()
        m = re.search(r"\b(crim\w*|employment|immigration|commercial|property|personal injury)\b", title, re.I)
        return not m or m.group(1)[:5].lower() in row.tags.lower()


REGISTERS: list[Register] = [CQCDirectory(), CareInspectorate(), ODSPractices(), ODSNursePrescribers(),
                             GIASSchools(), CharityRegister(), BarristersRegister()]
FALLBACKS = {"cqc": ODSCareHomes()}

FSA_TYPES = {1: "Restaurant / cafe / canteen", 7843: "Pub / bar / nightclub", 7842: "Hotel / B&B",
             5: "Hospital / care home kitchen"}


# --- the finder ----------------------------------------------------------------------------------------------

@dataclass
class RegistersFinder:
    http: Optional[HttpClient] = None
    max_rows: int = 600            # rows to geocode per register
    max_named: int = 150           # named professional contacts per search
    total_budget: float = TOTAL_BUDGET
    notes: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    def __post_init__(self):
        self.http = self.http or HttpClient(min_interval=1.0, timeout=30, retries=1, backoff=2)
        self.cache = DiskCache("registers", TTL)

    def subset(self, reg: Register, wanted_outcodes: set[str], budget: _Budget) -> list[RegRow]:
        """Rows in the wanted postcode areas: from the 7-day cache, else one streamed pass over the register."""
        areas = {area(o) for o in wanted_outcodes if area(o)}
        rows: list[dict] = []
        missing = set()
        for a in areas:
            hit = self.cache.get(f"{reg.key}:{a}")
            if hit is None:
                missing.add(a)
            else:
                rows += hit
        if missing:
            fresh: dict[str, list[dict]] = {a: [] for a in missing}
            for r in reg.download(self.http, lambda oc: area(oc) in missing, budget):
                fresh[area(outcode(r.postcode))].append(r.to_dict())
            for a, rs in fresh.items():
                self.cache.set(f"{reg.key}:{a}", rs)
                rows += rs
        return [RegRow(**r) for r in rows if outcode(r["postcode"]) in wanted_outcodes]

    def fsa(self, place: Place, radius: float, budget: _Budget) -> list[RegRow]:
        out = []
        for tid, label in FSA_TYPES.items():
            budget.check()
            params = {"latitude": round(place.lat, 4), "longitude": round(place.lon, 4),
                      "maxDistanceLimit": min(radius, 5), "businessTypeId": tid, "pageSize": 200, "pageNumber": 1,
                      "sortOptionKey": "distance"}
            key = "fsa:" + "&".join(f"{k}={v}" for k, v in params.items())
            data = self.cache.get(key)
            if data is None:
                try:
                    data = self.http.get_json(FSA_API, params=params,
                                              headers={"x-api-version": "2", "accept": "application/json"})
                except (requests.RequestException, ValueError) as e:
                    raise RegisterUnavailable(type(e).__name__) from e
                data = [{k: e.get(k) for k in ("BusinessName", "AddressLine1", "AddressLine2", "AddressLine3",
                                                "PostCode", "Phone", "FHRSID", "geocode", "RatingValue")}
                        for e in data.get("establishments") or []]
                self.cache.set(key, data)
            for e in data:
                geo = e.get("geocode") or {}
                lat, lon = geo.get("latitude"), geo.get("longitude")
                out.append(RegRow(name=e.get("BusinessName") or "", employer_type=label,
                                  postcode=pc_norm(e.get("PostCode") or ""),
                                  address=", ".join(filter(None, [e.get(f"AddressLine{i}") for i in (1, 2, 3)])),
                                  phone=e.get("Phone") or "", source="FSA",
                                  source_url=f"https://ratings.food.gov.uk/business/{e.get('FHRSID')}",
                                  lat=float(lat) if lat else None, lon=float(lon) if lon else None,
                                  tags=f"hygiene rating {e.get('RatingValue')}"))
        return out

    def find(self, kinds: list[str], title: str, place: Place, radius: float, wanted_outcodes: list[str],
             geocode: Callable[[list[str]], dict], progress: Callable[[str], None] = lambda m: None) -> list[RegRow]:
        total = _Budget(self.total_budget)
        rank = {o: i for i, o in enumerate(wanted_outcodes)}
        wanted = set(wanted_outcodes)
        out: list[RegRow] = []
        todo = [r for r in REGISTERS if r.applies(kinds, title)]
        if "food" in kinds:
            progress("Food Standards Agency: restaurants, pubs and hotels near the postcode…")
            try:
                rows = self.fsa(place, radius, total)
                self.stats["FSA"] = len(rows)
                out += rows
            except RegisterUnavailable as e:
                self.notes.append(f"FSA food hygiene register unavailable ({e})")
        def one(reg: Register) -> list[RegRow]:
            label = LICENCES[reg.label].split(" (")[0]
            for attempt in (reg, FALLBACKS.get(reg.key)):
                if attempt is None:
                    break
                if time.monotonic() > total.end:
                    self.notes.append(f"{label}: skipped - the registers' time budget for this search ran out")
                    break
                try:
                    budget = _Budget(min(DOWNLOAD_BUDGET, max(5.0, total.end - time.monotonic())))
                    rows = [r for r in self.subset(attempt, wanted, budget) if attempt.wanted(r, kinds, title)]
                    rows.sort(key=lambda r: rank.get(outcode(r.postcode), 999))
                    self.stats[attempt.key] = len(rows)
                    return rows[: self.max_rows]
                except (RegisterUnavailable, requests.RequestException, zipfile.BadZipFile, OSError, csv.Error) as e:
                    log.info("register %s failed: %s", attempt.key, e)
                    self.notes.append(f"{label} register unavailable ({e}); the other sources still ran")
            return []
        if todo:
            progress(f"Official registers: {', '.join(dict.fromkeys(LICENCES[r.label].split(' (')[0] for r in todo))}…")
            # registers download in parallel (each streamed, each with its own time budget)
            with ThreadPoolExecutor(max_workers=4) as pool:
                for rows in pool.map(one, todo):
                    out += rows
        need = [r.postcode for r in out if r.lat is None and r.postcode]
        coords = geocode(need) if need else {}
        kept = []
        for r in out:
            if r.lat is None:
                co = coords.get(re.sub(r"\s+", "", r.postcode.upper()))
                if co:
                    r.lat, r.lon = co
            r.distance = round(haversine_miles(place.lat, place.lon, r.lat, r.lon), 1) if r.lat is not None else None
            if r.distance is not None and r.distance > radius:
                continue
            kept.append(r)
        # named people: a few per organisation, nearest first, capped
        named = sorted([r for r in kept if r.named_person], key=lambda r: (r.distance is None, r.distance or 0))
        per: dict[str, int] = {}
        keep_named = []
        for r in named:
            k = (r.name.lower(), r.postcode)
            per[k] = per.get(k, 0) + 1
            if per[k] <= 5 and len(keep_named) < self.max_named:
                keep_named.append(r)
        return [r for r in kept if not r.named_person] + keep_named
