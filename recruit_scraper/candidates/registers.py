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
* Public Health Scotland open data: GP practices (quarterly CSV, OGL)            - Scottish GP practices + phone
* HSC Business Services Organisation (NI) practice list ('for professional use') - NI GP practices + phone
* RQIA register of services (Northern Ireland, xlsx)       - nursing homes, nursing agencies: registered manager, phone
Each register only runs for the UK nation(s) it covers (CQC / GIAS / enurse: England; ODS epraccur, Charity
Commission, BSB: England + Wales; Care Inspectorate + PHS: Scotland; BSO + RQIA: Northern Ireland). A whole-UK
search samples a few of the most contactable rows per postcode area instead of reading everything.
Not used: Care Inspectorate Wales (no bulk download of its register - only a search page); OpenDataNI's GP file
(no phone numbers; BSO's list has them).
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
PHS_GP = "https://www.opendata.nhs.scot/api/3/action/package_show?id=gp-practice-contact-details-and-list-sizes"
PHS_PAGE = "https://www.opendata.nhs.scot/dataset/gp-practice-contact-details-and-list-sizes"
BSO_PRACTICES = "https://hscweb.hscni.net/pmp/rptMedicalPracticeList.xlsx"
BSO_PAGE = ("https://bso.hscni.net/directorates/operations/family-practitioner-services/medical-services/"
            "contractor-information/northern-ireland-gp-practice-lists-for-professional-use/")
RQIA_XLSX = "https://www.rqia.org.uk/download/32/services-registered-with-rqia/4452/all-registered-services.xlsx"
RQIA_PAGE = "https://www.rqia.org.uk/what-we-do/register/"

LICENCES = {
    "CQC": "CQC data (Open Government Licence v3.0)",
    "Care Inspectorate": "Care Inspectorate datastore (Open Government Licence v3.0)",
    "NHS ODS": "NHS Organisation Data Service (Open Government Licence v3.0)",
    "FSA": "Food Standards Agency food hygiene ratings (Open Government Licence v3.0)",
    "DfE GIAS": "DfE Get Information about Schools (Open Government Licence v3.0)",
    "Charity Commission": "Charity Commission register of charities (Open Government Licence v3.0)",
    "BSB": "Bar Standards Board Barristers' Register",
    "PHS": "Public Health Scotland open data (Open Government Licence v3.0)",
    "HSC BSO": "HSC Business Services Organisation practice list (published for professional use)",
    "RQIA": "RQIA register of services (Northern Ireland)",
}

# postcode areas -> UK nation (border areas count for both sides)
_SCOT = {"AB", "DD", "DG", "EH", "FK", "G", "HS", "IV", "KA", "KW", "KY", "ML", "PA", "PH", "ZE"}
_WALES = {"CF", "LD", "LL", "NP", "SA"}
_BORDER = {"TD": {"Scotland", "England"}, "SY": {"England", "Wales"}, "CH": {"England", "Wales"},
           "HR": {"England", "Wales"}, "GL": {"England", "Wales"}}
ALL_NATIONS = {"England", "Scotland", "Wales", "Northern Ireland"}


def nations_of_area(a: str) -> set[str]:
    a = (a or "").upper()
    if a == "BT":
        return {"Northern Ireland"}
    if a in _SCOT:
        return {"Scotland"}
    if a in _WALES:
        return {"Wales"}
    return set(_BORDER.get(a, {"England"}))


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
    nations: set[str] = {"England"}    # which UK nations the register covers

    def applies(self, kinds: list[str], title: str) -> bool:
        return bool(self.kinds & set(kinds))

    def download(self, http: HttpClient, keep: Callable[[str], bool], budget: _Budget) -> Iterator[RegRow]:
        raise NotImplementedError

    def wanted(self, row: RegRow, kinds: list[str], title: str) -> bool:
        return True


CQC_TYPES = {"gp": ("Doctors/GPs",), "care_home": ("Residential homes", "Nursing homes"),
             "home_care": ("Homecare agencies", "Supported living", "Shared lives"), "dental": ("Dentist",),
             "hospital": ("Hospital", "Hospice"), "clinic": ("Clinic", "Community services - Nursing",
                                                              "Community services - Healthcare"),
             "community": ("Community services - Nursing", "Community services - Healthcare"),
             "urgent_care": ("Urgent care centres", "Mobile doctors")}
CQC_LABEL = {"Doctors/GPs": "GP practice", "Residential homes": "Care home", "Nursing homes": "Nursing home",
             "Homecare agencies": "Home care agency", "Supported living": "Supported living service",
             "Shared lives": "Shared lives scheme", "Dentist": "Dental practice", "Hospital": "Hospital",
             "Hospice": "Hospice", "Clinic": "Clinic", "Community services - Nursing": "Community nursing service",
             "Community services - Healthcare": "Community health service",
             "Urgent care centres": "Urgent care / walk-in centre", "Mobile doctors": "Out-of-hours GP service"}


class CQCDirectory(Register):
    key, label, kinds = "cqc", "CQC", {"gp", "care_home", "home_care", "dental", "hospital", "clinic", "community",
                                       "urgent_care"}

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
    key, label, kinds = "careinsp", "Care Inspectorate", {"care_home", "home_care", "hospital", "clinic",
                                                          "nurse_agency"}
    nations = {"Scotland"}

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
                or ("nurse_agency" in kinds and t.startswith("Nurse Agency"))
                or (bool(nurse) and "gp" not in kinds and (t.startswith("Nurse Agency")
                                                          or t.startswith("Care Home Service|Older"))))


class ODSPractices(Register):
    key, label, kinds = "epraccur", "NHS ODS", {"gp"}
    nations = {"England", "Wales"}

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
    nations = {"England", "Wales"}

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
    nations = {"England", "Wales"}

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


def read_xlsx(http: HttpClient, url: str, budget: _Budget, max_bytes: int = 20 * 1024 * 1024) -> list[tuple]:
    """A small official spreadsheet (a few hundred KB): downloaded with a size cap, every row as a tuple."""
    import openpyxl
    import warnings
    buf = io.BytesIO()
    try:
        with http.session.get(url, stream=True, timeout=(15, 30)) as r:
            r.raise_for_status()
            for chunk in r.iter_content(1 << 16):
                buf.write(chunk)
                if buf.tell() > max_bytes:
                    raise RegisterUnavailable("file larger than expected - skipped")
                budget.check()
    except requests.RequestException as e:
        raise RegisterUnavailable(type(e).__name__) from e
    buf.seek(0)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            wb = openpyxl.load_workbook(buf, read_only=True, data_only=True)
        except Exception as e:  # noqa: BLE001 - not a spreadsheet (an error page)
            raise RegisterUnavailable("not a spreadsheet") from e
        ws = wb.active
        ws.reset_dimensions()         # these files carry a wrong 'A1' dimension
        rows = [tuple("" if v is None else str(v).strip() for v in row) for row in ws.iter_rows(values_only=True)]
        wb.close()
    return rows


def _header_rows(rows: list[tuple], first: str) -> Iterator[Callable[[str], str]]:
    """Rows after the header row whose first cell is `first`, as column-name getters."""
    header = None
    for row in rows:
        if header is None:
            if row and row[0] == first:
                header = {h: i for i, h in enumerate(row) if h}
            continue
        yield (lambda r: (lambda h: r[header[h]] if h in header and header[h] < len(r) else ""))(row)


class PHSScotlandPractices(Register):
    """Every GP practice in Scotland with its phone (Public Health Scotland open data, OGL, quarterly)."""
    key, label, kinds = "phs_gp", "PHS", {"gp"}
    nations = {"Scotland"}

    def download(self, http, keep, budget):
        try:
            res = http.get_json(PHS_GP)["result"]["resources"]
        except (requests.RequestException, ValueError, KeyError, TypeError) as e:
            raise RegisterUnavailable(type(e).__name__) from e
        url = max(res, key=lambda x: x.get("created") or "")["url"]   # the newest quarter
        header = None
        for row in stream_csv(http, url, budget):
            if header is None:
                header = {h: i for i, h in enumerate(row)}
                continue
            g = lambda h: row[header[h]].strip() if h in header and header[h] < len(row) else ""
            pc = pc_norm(g("Postcode"))
            if not keep(outcode(pc)):
                continue
            yield RegRow(name=_title(g("GPPracticeName")), employer_type="GP practice", postcode=pc,
                         address=", ".join(_title(g(f"AddressLine{i}")) for i in range(1, 5) if g(f"AddressLine{i}")),
                         phone=g("TelephoneNumber"), ods_code=g("PracticeCode"), source="PHS",
                         source_url=f"{PHS_PAGE}#{g('PracticeCode')}", tags=g("GPCluster"))


class NIPractices(Register):
    """Every GP practice in Northern Ireland with its phone (HSC Business Services Organisation's practice list,
    published 'for people or organisations looking to create address labels' - i.e. to contact practices)."""
    key, label, kinds = "bso_gp", "HSC BSO", {"gp"}
    nations = {"Northern Ireland"}

    def download(self, http, keep, budget):
        for g in _header_rows(read_xlsx(http, BSO_PRACTICES, budget), "PracNo"):
            pc = pc_norm(g("Postcode"))
            if not pc or not keep(outcode(pc)):
                continue
            name = g("practiceName") or g("PartnershipName")
            yield RegRow(name=_title(name), employer_type="GP practice", postcode=pc,
                         address=", ".join(_title(g(f"Address{i}")) for i in range(1, 5)
                                           if g(f"Address{i}") and g(f"Address{i}") != name),
                         phone=g("TelNo"), ods_code=g("PartnershipNo"), source="HSC BSO",
                         source_url=f"{BSO_PAGE}#{g('PracNo')}", tags=g("LCG"))


class RQIARegister(Register):
    """Northern Ireland's register of care services (RQIA): nursing homes and nursing agencies with the
    registered manager named by the regulator."""
    key, label, kinds = "rqia", "RQIA", {"care_home", "home_care", "nurse_agency"}
    nations = {"Northern Ireland"}

    def download(self, http, keep, budget):
        for g in _header_rows(read_xlsx(http, RQIA_XLSX, budget), "Service ID"):
            pc = pc_norm(g("Postcode"))
            if not pc or not keep(outcode(pc)) or not pc.startswith("BT"):
                continue      # a few agencies are registered from Dublin / GB offices
            cat = g("Category")
            label = re.sub(r"\s*\([A-Z]+\)", "", cat).strip() or "Care service"
            label = {"Nursing": "Nursing home", "Residential": "Residential care home"}.get(label, label)
            yield RegRow(name=g("Service"), employer_type=label, postcode=pc,
                         address=", ".join(filter(None, [g("Address Line1"), g("Address Line2"), g("Town")])),
                         phone=g("Phone"), contact_name=_person(g("Manager")),
                         contact_role="Registered manager (RQIA)" if g("Manager") else "", source="RQIA",
                         source_url=f"{RQIA_PAGE}#{g('Service ID')}", tags=cat)

    def wanted(self, row, kinds, title):
        t = row.tags
        nurse = bool(re.search(r"\bnurs|\brgn\b|\brmn\b", title, re.I))
        return (("nurse_agency" in kinds and t.startswith("Nursing Agency"))
                or ("care_home" in kinds and (t.startswith("Nursing (") or (not nurse and t.startswith("Residential"))))
                or ("home_care" in kinds and t.startswith("Domiciliary Care")))


REGISTERS: list[Register] = [CQCDirectory(), CareInspectorate(), ODSPractices(), PHSScotlandPractices(),
                             NIPractices(), RQIARegister(), ODSNursePrescribers(), GIASSchools(), CharityRegister(),
                             BarristersRegister()]
FALLBACKS = {"cqc": ODSCareHomes()}

FSA_TYPES = {1: "Restaurant / cafe / canteen", 7843: "Pub / bar / nightclub", 7842: "Hotel / B&B",
             5: "Hospital / care home kitchen"}


# --- the finder ----------------------------------------------------------------------------------------------

def uk_bucket(oc: str) -> str:
    """Sampling unit for a whole-UK search: the postcode area ('M', 'G'); Northern Ireland is one area (BT) with
    ~3% of the UK, so it is split by district group (BT1-9, BT2x … BT9x) to be fairly represented."""
    a = area(oc)
    if a == "BT":
        d = re.sub(r"\D", "", oc)
        return f"BT{d[0] if len(d) == 2 else '0'}" if d else "BT"
    return a


def contact_score(r) -> int:
    """How reachable a row is: email > website > phone (+ a named contact)."""
    return 4 * bool(r.email) + 2 * bool(r.website) + 2 * bool(r.phone) + bool(r.contact_name)


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

    per_area_uk: int = 3           # whole-UK search: rows kept per postcode area (most contactable first)

    def sample_uk(self, reg: Register, budget: _Budget, want: Callable[[RegRow], bool] = lambda r: True,
                  key: str = "") -> list[RegRow]:
        """Whole-UK search: one streamed pass keeping the few most contactable rows of each postcode area (so every
        region and nation is represented and memory stays small). Cached for 7 days."""
        ck = f"{reg.key}:UK3-{self.per_area_uk}:{key}"
        hit = self.cache.get(ck)
        if hit is not None:
            return [RegRow(**r) for r in hit]
        best: dict[str, list[RegRow]] = {}
        n_named: dict[str, int] = {}
        for r in reg.download(self.http, lambda oc: True, budget):
            a = uk_bucket(outcode(r.postcode))
            if not a or not want(r):
                continue
            if r.named_person:      # named people: the first few per area (register order spreads them out)
                if n_named.get(a, 0) < self.per_area_uk:
                    n_named[a] = n_named.get(a, 0) + 1
                    best.setdefault(f"{a}|named", []).append(r)
                continue
            lst = best.setdefault(f"{a}|{r.employer_type}", [])   # every kind of employer in every area
            lst.append(r)
            if len(lst) > self.per_area_uk * 4:   # keep memory flat: trim to the most contactable
                lst.sort(key=contact_score, reverse=True)
                del lst[self.per_area_uk * 2:]
        out = []
        for lst in best.values():
            named = [r for r in lst if r.named_person]
            rest = sorted([r for r in lst if not r.named_person], key=contact_score, reverse=True)[: self.per_area_uk * 2]
            out += rest + named
        self.cache.set(ck, [r.to_dict() for r in out])
        return out

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

    def find(self, kinds: list[str], title: str, place: Place, radius: float, wanted_outcodes: Optional[list[str]],
             geocode: Callable[[list[str]], dict], progress: Callable[[str], None] = lambda m: None) -> list[RegRow]:
        total = _Budget(self.total_budget)
        uk = bool(getattr(place, "nationwide", False)) or wanted_outcodes is None
        wanted_outcodes = list(wanted_outcodes or [])
        rank = {o: i for i, o in enumerate(wanted_outcodes)}
        wanted = set(wanted_outcodes)
        out: list[RegRow] = []
        # only the registers for the UK nation(s) the search area is in (Glasgow: no CQC; Belfast: BSO + RQIA)
        nations = ALL_NATIONS if uk else ({n for o in wanted for n in nations_of_area(area(o))}
                                          | ({place.country} & ALL_NATIONS))
        todo = [r for r in REGISTERS if r.applies(kinds, title) and r.nations & nations]
        self.stats["nations"] = ", ".join(sorted(nations))
        if "food" in kinds and not uk:
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
                    got = (self.sample_uk(attempt, budget, lambda r: attempt.wanted(r, kinds, title),
                                          f"{'-'.join(sorted(kinds))}:{title.lower().strip()}") if uk
                           else self.subset(attempt, wanted, budget))
                    rows = [r for r in got if attempt.wanted(r, kinds, title)]
                    rows.sort(key=lambda r: (-contact_score(r), r.postcode) if uk else rank.get(outcode(r.postcode), 999))
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
        coords = geocode(need) if need and not uk else {}   # whole UK: distance from the middle means nothing
        kept = []
        for r in out:
            if r.lat is None:
                co = coords.get(re.sub(r"\s+", "", r.postcode.upper()))
                if co:
                    r.lat, r.lon = co
            r.distance = (round(haversine_miles(place.lat, place.lon, r.lat, r.lon), 1)
                          if r.lat is not None and not uk else None)
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
