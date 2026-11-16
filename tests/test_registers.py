"""Official open-data registers as employers / register-named professionals (offline, fake HTTP)."""
import io
import zipfile

import pytest
import requests

from recruit_scraper.candidates import registers as R
from recruit_scraper.candidates.employers import EmployerLeadsFinder, employer_kinds, suppress_leads
from recruit_scraper.candidates.geo import Place

MAN = Place("M1 1AE", 53.48, -2.23, "Manchester", "North West", "England")
CO = {"M11AE": (53.48, -2.23), "M14BT": (53.479, -2.24), "M27AB": (53.49, -2.24), "G11XQ": (55.86, -4.25),
      "M11AA": (53.481, -2.231)}
geocode = lambda pcs: {k: v for k, v in CO.items() if k in {p.replace(" ", "").upper() for p in pcs}}

CQC_CSV = ("CQC Locations data,,\n,,\nThis data was produced on 04 November 2026,,\n,,\n"
           "Name,Also known as,Address,Postcode,Phone number,Service's website (if available),Service types,"
           "Date of latest check,Specialisms/services,Provider name,Local authority,Region,Location URL,"
           "CQC Location ID (for office use only),CQC Provider ID (for office use only)\n"
           'Oak House,,"1 Oak St,Manchester",M1 4BT,1612345678,www.oak.example,Residential homes|Nursing homes,,,'
           "Oak Ltd,Manchester,North West,https://www.cqc.org.uk/location/1-1,1-1,1-2\n"
           'Smile Dental,,"2 St,Manchester",M2 7AB,1610000000,,Dentist,,,X,Manchester,NW,https://www.cqc.org.uk/location/1-3,1-3,1-4\n'
           'Far Home,,"3 St,Leeds",LS1 1AA,1130000000,,Residential homes,,,X,Leeds,Y,https://www.cqc.org.uk/location/1-5,1-5,1-6\n')
CI_HEAD = ("CSNumber,CareService,Subtype,ServiceName,Address_line_1,Address_line_2,Service_town,Service_Postcode,"
           "Manager_Name,Service_Phone_Number,Eforms_email_address,PublicList,ServiceStatus\n")
CI_CSV = CI_HEAD + ("CS1,Care Home Service,Older People,Glen Care,1 Rd,,Glasgow,G1 1XQ,JANE DOE,0141 000,glen@care.example,Yes,Active\n"
                    "CS2,Child Minding,,A Childminder,2 Rd,,Glasgow,G1 1XQ,Ann Minder,0141 111,ann@x.example,Yes,Active\n"
                    "CS3,Care Home Service,Older People,Hidden,3 Rd,,Glasgow,G1 1XQ,Bob,0141 222,h@x.example,No,Active\n")
ENURSE = "\n".join(f'"","Y1","PIN{i}","20200101","","","","NURSE{i} A","CITY SURGERY","1 ST","","MANCHESTER","","M1 1AA","0161 111"'
                   for i in range(8)) + "\n"
BSB_PAGE_HTML = '<a href="https://www.barstandardsboard.org.uk/asset/ABC/" download>Download</a>'
BSB_CSV = ('"Title","Forenames","Surname","Honours","Primary status","Date of call","Address Name","Address Floor Street",'
           '"Address Town","Address Postcode","Address Country","Address Tel","Areas of practice","Finding Offence Details"\n'
           '"Ms","Fam","Lawyer","","Self Employed","Jul 2001","Family Chambers","1 Court","MANCHESTER","M1 4BT",'
           '"United Kingdom","(+44) 0161 9999","Family, Children","SECRET-FINDING"\n'
           '"Mr","Crim","Only","","Self Employed","Jul 2001","Crime Chambers","2 Court","MANCHESTER","M2 7AB",'
           '"United Kingdom","(+44) 0161 8888","Crime",""\n')


def _zip(text):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("publicextract.charity.txt", text)
    return b.getvalue()


CHAR_HEAD = ("organisation_number\tregistered_charity_number\tlinked_charity_number\tcharity_name\t"
             "charity_registration_status\tcharity_contact_postcode\tcharity_contact_phone\tcharity_contact_email\t"
             "charity_contact_web\tcharity_activities\tcharity_contact_address1\n")
CHARITY = _zip(CHAR_HEAD + "1\t111\t0\tOAK DEMENTIA CARE\tRegistered\tM1 4BT\t0161 1\tinfo@oakcare.example\t\tdementia care\t1 Oak\n"
                           "2\t222\t0\tCHESS CLUB\tRegistered\tM1 4BT\t0161 2\tchess@x.example\t\tchess\t2 Rd\n")


class Resp:
    def __init__(self, body=b"", status=200):
        self.body, self.status_code = body if isinstance(body, bytes) else body.encode("utf-8"), status
        self.raw = io.BytesIO(self.body)
        self.raw.decode_content = True
        self.text = self.body.decode("utf-8", "replace")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def iter_content(self, n):
        yield self.body

    def close(self):
        pass

    def json(self):
        import json
        return json.loads(self.text)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class FakeHttp:
    def __init__(self, routes):
        self.routes, self.calls = routes, []
        self.session = self

    def _find(self, url):
        self.calls.append(url)
        for k, v in self.routes.items():
            if k in url:
                if isinstance(v, Exception):
                    raise v
                return Resp(v) if not isinstance(v, Resp) else v
        return Resp(b"", 404)

    def get(self, url, **kw):
        r = self._find(url)
        r.raise_for_status()
        return r if not kw.get("stream") else Resp(r.body)

    def head(self, url, **kw):
        return self._find(url)

    def get_json(self, url, **kw):
        return self.get(url).json()


ROUTES = {"using-cqc-data": '<a href="https://www.cqc.org.uk/system/files/2026-11/04_November_2026_CQC_directory.csv">x</a>',
          "CQC_directory.csv": CQC_CSV, "datastoreexternal.csv": CI_CSV, "report=enurse": ENURSE,
          "the-barristers-register": BSB_PAGE_HTML, "asset/ABC": BSB_CSV, "publicextract.charity.zip": CHARITY,
          "report=epraccur": "", "edubasealldata": ""}


@pytest.fixture(autouse=True)
def _cache(tmp_path, monkeypatch):
    from recruit_scraper.candidates import cache as cache_mod
    monkeypatch.setattr(cache_mod, "CACHE_ROOT", tmp_path)
    monkeypatch.setattr(cache_mod.DiskCache.__init__, "__defaults__", (tmp_path,))


def _find(title, routes=ROUTES, kinds=None, outcodes=("M1", "M2", "M4")):
    f = R.RegistersFinder(http=FakeHttp(dict(routes)))
    rows = f.find(kinds or employer_kinds(title), title, MAN, 10, list(outcodes), geocode)
    return f, rows


def test_new_role_kinds():
    assert employer_kinds("sous chef") == ["food"] and employer_kinds("primary teacher") == ["school"]
    assert employer_kinds("barrister") == ["barristers"] and employer_kinds("electrician") == ["trades"] and employer_kinds("family lawyer") == ["barristers"] and employer_kinds("solicitor") == ["law"]


def test_cqc_directory_skips_preamble_and_filters_by_role_and_area():
    f, rows = _find("care worker")
    cqc = [r for r in rows if r.source == "CQC"]
    assert [r.name for r in cqc] == ["Oak House"]                     # dentist and Leeds home filtered out
    assert cqc[0].phone == "01612345678" and cqc[0].website == "https://www.oak.example"
    assert cqc[0].employer_type == "Care home" and not cqc[0].named_person
    char = [r for r in rows if r.source == "Charity Commission"]
    assert [r.email for r in char] == ["info@oakcare.example"]        # care charity only, chess club dropped


def test_named_professionals_are_labelled_and_capped():
    f, rows = _find("practice nurse")
    nurses = [r for r in rows if r.named_person]
    assert len(nurses) == 5 and all(r.contact_role.startswith("Nurse prescriber") for r in nurses)  # 5 per workplace
    assert all("not" not in r.contact_role.lower() for r in nurses) and nurses[0].phone == "0161 111"


def test_barristers_family_filter_and_no_disciplinary_data():
    f, rows = _find("family lawyer")
    assert [r.contact_name for r in rows] == ["Ms Fam Lawyer"]
    assert rows[0].phone == "0161 9999" and "SECRET" not in repr(rows[0].to_dict())


def test_care_inspectorate_never_lists_childminders_or_private_services():
    g = Place("G1 1XQ", 55.86, -4.25, "Glasgow", "Scotland", "Scotland")
    f = R.RegistersFinder(http=FakeHttp(dict(ROUTES)))
    rows = f.find(["care_home", "home_care"], "care worker", g, 10, ["G1"], geocode)
    ci = [r for r in rows if r.source == "Care Inspectorate"]
    assert [r.name for r in ci] == ["Glen Care"] and ci[0].contact_name == "Jane Doe"
    assert ci[0].contact_role.startswith("Registered manager") and ci[0].email == "glen@care.example"


def test_register_down_is_a_note_not_a_failure_and_cache_is_used():
    routes = dict(ROUTES, **{"CQC_directory.csv": requests.ConnectionError("down")})
    f, rows = _find("care worker", routes)
    assert any("CQC" in n and "unavailable" in n for n in f.notes)
    assert [r.source for r in rows] == ["Charity Commission"]           # the other register still ran
    http = FakeHttp(dict(ROUTES))
    f1 = R.RegistersFinder(http=http)
    f1.find(["barristers"], "barrister", MAN, 10, ["M1", "M2"], geocode)
    n = len(http.calls)
    f1.find(["barristers"], "barrister", MAN, 10, ["M1", "M2"], geocode)
    assert len(http.calls) == n                                         # second search: cached subset, no download


def test_time_budget_stops_a_slow_register(monkeypatch):
    monkeypatch.setattr(R, "DOWNLOAD_BUDGET", -1.0)
    f, rows = _find("family lawyer")
    assert not rows and any("too long" in n for n in f.notes)


def test_finder_merges_registers_and_suppresses_named_people():
    class Off:
        notes, stats = [], {}

        def find(self, kinds, title, place, radius, dists, geo, progress):
            return [R.RegRow(name="Family Chambers", employer_type="Barristers' chambers", postcode="M1 4BT",
                             contact_name="Ms Fam Lawyer", contact_role="Barrister (BSB register)", source="BSB",
                             source_url="https://bsb.example/#ms-fam", named_person=True, distance=0.5)]
    f = EmployerLeadsFinder(registers=Off())
    f.districts = lambda place, r: ["M1"]
    f.ods_leads = lambda *a, **k: []
    leads, n = f.find("family lawyer", MAN, 10, entries=[])
    assert leads[0].contact_name == "Ms Fam Lawyer" and "not a job-seeker" in leads[0].note
    leads, n = f.find("family lawyer", MAN, 10, entries=["ms fam lawyer"])
    assert not leads and n == 1
    assert suppress_leads(leads, [])[1] == 0
