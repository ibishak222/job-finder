"""Practice nurse / ANP / HCA / district nurse searches, perm and locum, across the whole UK (offline)."""
import io
import types

import pytest
import requests

from recruit_scraper.candidates import registers as R
from recruit_scraper.candidates.contacts import SiteContactFinder, practice_managers
from recruit_scraper.candidates.employers import EmployerLead, EmployerLeadsFinder, _tier, employer_kinds
from recruit_scraper.candidates.geo import Place, is_nationwide, lookup_postcode
from recruit_scraper.candidates.matching import RoleMatcher, looks_like_business, place_terms
from recruit_scraper.candidates.social import BlueskyProvider, MastodonProvider
from recruit_scraper.models import Job
from recruit_scraper.sources.nhs import NHSJobsSource

MAN = Place("M1 1AE", 53.48, -2.23, "Manchester", "North West", "England", ["Manchester", "Salford", "Trafford"])
GLA = Place("G1 1XQ", 55.86, -4.25, "Glasgow City", "", "Scotland", ["Glasgow City"])


@pytest.fixture(autouse=True)
def _cache(tmp_path, monkeypatch):
    from recruit_scraper.candidates import cache as cache_mod
    monkeypatch.setattr(cache_mod, "CACHE_ROOT", tmp_path)
    monkeypatch.setattr(cache_mod.DiskCache.__init__, "__defaults__", (tmp_path,))


# --- job titles -------------------------------------------------------------------------------------------------

def test_practice_nurse_taxonomy():
    m = RoleMatcher("practice nurse")
    for bio in ("GPN in Salford", "Treatment room nurse at a busy surgery", "General Practice Nurse, Leeds"):
        assert m.match(bio).score >= 2, bio
    for bio in ("ANP at a GP surgery", "Nurse prescriber, diabetes and asthma", "Cervical sample taker and RGN"):
        assert 0 < m.match(bio).score < 2, bio          # related: shown, ranked after real practice nurses
    assert m.match("Writer: Call the Midwife").score == 0            # midwives aren't nurses
    assert m.match("Mum, GP-Practice Nurse before, now a coder").score == 0
    assert m.match("Practice nurse (retired)").score == 0
    assert m.match("AP at a GP surgery. Aspiring to become a general practice nurse").score == 0
    assert m.match("Practice nurse who wants to travel").score == 3
    assert RoleMatcher("ANP").match("Advanced Nurse Practitioner, Leeds").score >= 2
    assert RoleMatcher("hca").match("Healthcare support worker at my local surgery").score >= 2
    assert RoleMatcher("district nurse").match("Community staff nurse, Bristol").score >= 2
    assert RoleMatcher("locum practice nurse").contract == "temporary"
    assert looks_like_business("Pallant Medical Chambers", "") and looks_like_business("Oak Surgery", "")


def test_social_queries_add_family_and_locum_words():
    qs = BlueskyProvider().queries(RoleMatcher("practice nurse"), MAN)
    assert "nurse Manchester" in qs and "general practice nurse Manchester" in qs
    lq = MastodonProvider().queries(RoleMatcher("locum practice nurse"), MAN)
    assert lq[:3] == ["locum practice nurse", "bank nurse", "locum nurse Manchester"]
    uk = BlueskyProvider().queries(RoleMatcher("practice nurse"), lookup_postcode(None, "UK"))
    assert "practice nurse UK" in uk and not any("United Kingdom" in q for q in uk)


# --- whole-UK search --------------------------------------------------------------------------------------------

def test_nationwide_place_needs_no_lookup():
    assert is_nationwide("UK") and is_nationwide(" united kingdom ") and not is_nationwide("M1 1AE")
    p = lookup_postcode(None, "United Kingdom")         # no HTTP at all
    assert p.nationwide and p.town == "United Kingdom" and p.nearby_towns == []
    terms = place_terms(p)
    assert {"Glasgow", "Belfast", "Cardiff", "Truro", "Scotland", "Northern Ireland"} <= set(terms)


def test_social_search_nationwide_keeps_any_uk_town_without_distance():
    class P(BlueskyProvider):
        def _collect(self, qs, progress):
            return {"1": {"id": "1", "name": "Jo", "bio": "Practice nurse", "location": "Belfast",
                          "url": "https://bsky.app/profile/jo", "links": []},
                    "2": {"id": "2", "name": "Sam", "bio": "Practice nurse, UK based", "location": "",
                          "url": "https://bsky.app/profile/sam", "links": []},
                    "3": {"id": "3", "name": "Al", "bio": "Practice nurse", "location": "Toronto, Canada",
                          "url": "https://bsky.app/profile/al", "links": []}}

        def extra_people(self, matcher, progress):
            return {}

    class Geo:
        def geocode(self, t):
            return {"Belfast": (54.6, -5.93)}.get(t)
    p = lookup_postcode(None, "UK")
    got = list(P(fetch_websites=False, uk_unknown=True).search("practice nurse", p, 600, None, None, [], False, Geo()))
    names = {c.name: c for c in got}
    assert set(names) == {"Jo", "Sam"}
    assert names["Jo"].distance_miles is None and names["Jo"].location == "Belfast"
    assert "no nearby town" not in names["Sam"].location


# --- employers: kinds, nations, ranking ---------------------------------------------------------------------------

def test_locum_kinds_and_tiers():
    assert employer_kinds("practice nurse") == ["gp", "independent"]
    assert employer_kinds("private practice nurse") == ["independent", "gp"]
    assert employer_kinds("locum practice nurse") == ["gp", "urgent_care", "independent"]
    assert "nurse_agency" not in employer_kinds("locum practice nurse")      # agencies are never employers to contact
    assert employer_kinds("ANP") == ["gp", "urgent_care", "independent"]
    assert employer_kinds("district nurse")[0] == "community"
    kinds = employer_kinds("locum practice nurse")
    leads = [EmployerLead(name="A", employer_type="Independent clinic"), EmployerLead(name="B", employer_type="GP practice"),
             EmployerLead(name="C", employer_type="Urgent care / walk-in centre")]
    assert [l.name for l in sorted(leads, key=lambda l: _tier(l, kinds))] == ["B", "C", "A"]


def test_nations_of_area():
    assert R.nations_of_area("G") == {"Scotland"} and R.nations_of_area("BT") == {"Northern Ireland"}
    assert R.nations_of_area("CF") == {"Wales"} and R.nations_of_area("M") == {"England"}
    assert R.nations_of_area("SY") == {"England", "Wales"}


class _Reg(R.Register):
    def __init__(self, key, nations, rows):
        self.key, self.label, self.kinds, self.nations, self.rows, self.called = key, "NHS ODS", {"gp"}, nations, rows, 0

    def download(self, http, keep, budget):
        self.called += 1
        for r in self.rows:
            if keep(R.outcode(r.postcode)):
                yield r


def _row(name, pc, **kw):
    return R.RegRow(name=name, employer_type="GP practice", postcode=pc, source="NHS ODS", **kw)


def test_registers_run_only_for_the_nation_searched(monkeypatch):
    eng = _Reg("eng", {"England"}, [_row("Manc GP", "M1 1AA", phone="0161")])
    sco = _Reg("sco", {"Scotland"}, [_row("Glasgow GP", "G1 1XQ", phone="0141")])
    monkeypatch.setattr(R, "REGISTERS", [eng, sco])
    f = R.RegistersFinder(http=object())
    rows = f.find(["gp"], "practice nurse", GLA, 10, ["G1", "G2"], lambda pcs: {"G11XQ": (55.86, -4.25)})
    assert [r.name for r in rows] == ["Glasgow GP"] and eng.called == 0 and sco.called == 1
    assert f.stats["nations"] == "Scotland"


def test_whole_uk_samples_contactable_rows_per_area(monkeypatch):
    rows = [_row(f"M{i}", f"M{i} 1AA", phone="0161" if i % 2 else "", website="https://x.example" if i == 3 else "")
            for i in range(1, 9)]
    rows += [_row("Belfast GP", "BT1 5GS", phone="028"), _row("Truro GP", "TR1 2HE", phone="01872")]
    reg = _Reg("all", {"England", "Northern Ireland"}, rows)
    monkeypatch.setattr(R, "REGISTERS", [reg])
    f = R.RegistersFinder(http=object(), per_area_uk=2)
    got = f.find(["gp"], "practice nurse", lookup_postcode(None, "UK"), 600, None, lambda pcs: 1 / 0)
    m = [r.name for r in got if r.postcode.startswith("M")]
    assert m[0] == "M3" and len(m) == 4 and {"Belfast GP", "Truro GP"} <= {r.name for r in got}
    assert all(r.distance is None for r in got)          # no geocoding for a whole-UK search
    again = f.find(["gp"], "practice nurse", lookup_postcode(None, "UK"), 600, None, lambda pcs: 1 / 0)
    assert len(again) == len(got) and reg.called == 1     # cached


class _Resp:
    def __init__(self, body=b"", status=200):
        self.body = body if isinstance(body, bytes) else body.encode()
        self.status_code, self.raw = status, io.BytesIO(self.body)
        self.raw.decode_content = True

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def iter_content(self, n):
        yield self.body

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class _Http:
    def __init__(self, routes, json_routes=None):
        self.routes, self.json_routes, self.session = routes, json_routes or {}, self

    def get(self, url, **kw):
        for k, v in self.routes.items():
            if k in url:
                return _Resp(v)
        return _Resp(b"", 404)

    def get_json(self, url, **kw):
        return next(v for k, v in self.json_routes.items() if k in url)


def _xlsx(rows):
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in rows:
        ws.append(list(r))
    b = io.BytesIO()
    wb.save(b)
    return b.getvalue()


def test_scotland_ni_registers_parse():
    phs = ("PracticeCode,GPPracticeName,PracticeListSize,AddressLine1,AddressLine2,AddressLine3,AddressLine4,Postcode,"
           "TelephoneNumber,PracticeType,HB,HSCP,DataZone,GPCluster\n"
           "43210,CITY MEDICAL PRACTICE,5000,1 HIGH ST,,GLASGOW,,G1 1XQ,0141 111 2222,17J,S08,S37,S01,Central\n")
    http = _Http({"practice_contact": phs}, {"package_show": {"result": {"resources": [
        {"url": "https://x/practice_contact_old.csv", "created": "2026-01-01"},
        {"url": "https://x/practice_contact_new.csv", "created": "2026-10-01"}]}}})
    rows = list(R.PHSScotlandPractices().download(http, lambda oc: True, R._Budget(30)))
    assert rows[0].name == "City Medical Practice" and rows[0].phone == "0141 111 2222" and rows[0].source == "PHS"
    bso = _xlsx([("Date Last Modified", "10 November 2026"),
                 ("PracNo", "PartnershipNo", "PartnershipName", "practiceName", "Address1", "Address2", "Address3",
                  "Address4", "Postcode", "TelNo", "LCG"),
                 ("64", "PE0007", "Dr.DONAGHY & PARTNERS", "DUNVILLE HEALTH CENTRE", "DUNVILLE HEALTH CENTRE",
                  "309 GROSVENOR ROAD", "BELFAST", None, "BT12 4LP", "02890325185", "Belfast")])
    rows = list(R.NIPractices().download(_Http({"rptMedicalPracticeList": bso}), lambda oc: True, R._Budget(30)))
    assert rows[0].name == "Dunville Health Centre" and rows[0].phone == "02890325185"
    assert rows[0].address == "309 Grosvenor Road, Belfast" and rows[0].source == "HSC BSO"
    rq = _xlsx([("Current Register",), (), ("Service ID", "Service", "Address Line1", "Address Line2", "Town", "Postcode",
                                             "Phone", None, "Category", "Max", "Cats", "Def", "Cond", "Responsible Person",
                                             "Manager", "Provider"),
                ("1", "Bay Nurses", "1 Rd", None, "Belfast", "BT1 1AA", "028 1", None, "Nursing Agency (NA)", None, "",
                 None, None, "Ann Owner", "Kim Lee", "Bay Ltd"),
                ("2", "Dublin Agency", "2 Rd", None, "Dublin", "D02V960", "+353", None, "Nursing Agency (NA)", None, "",
                 None, None, "X", "Y", "Z"),
                ("3", "Oak Home", "3 Rd", None, "Lisburn", "BT27 4AA", "028 2", None, "Nursing (NH)", 40, "", None,
                 None, "X", "Pat Doe", "Oak")])
    reg = R.RQIARegister()
    rows = list(reg.download(_Http({"all-registered-services": rq}), lambda oc: True, R._Budget(30)))
    assert [r.name for r in rows] == ["Bay Nurses", "Oak Home"]                 # Dublin office skipped
    assert rows[0].contact_name == "Kim Lee" and rows[0].contact_role == "Registered manager (RQIA)"
    # nursing agencies are recruiters: never wanted, whatever the search
    assert [r.name for r in rows if reg.wanted(r, ["gp", "urgent_care", "care_home"], "locum practice nurse")] == ["Oak Home"]


# --- practice websites ------------------------------------------------------------------------------------------

def test_practice_managers_from_own_site_text():
    t = ("Meet the team. Practice Manager: Mrs Jane Smith. Dr Alan Jones (GP). Sarah Brown - Lead Nurse. "
         "Please contact the Practice Manager for complaints. Opening Hours Practice Manager")
    assert practice_managers(t) == [("Mrs Jane Smith", "Practice Manager"), ("Sarah Brown", "Lead Nurse")]


def test_practice_site_keeps_only_own_or_nhs_emails(monkeypatch):
    pages = {"https://www.oaksurgery.example/": {"emails": ["oak.surgery@nhs.net", "someone@gmail.com"],
                                                 "managers": [], "url": "https://www.oaksurgery.example/"},
             "https://www.oaksurgery.example/contact-us": {"emails": ["admin@oaksurgery.example"],
                                                           "managers": [("Jane Smith", "Practice Manager")],
                                                           "url": "https://www.oaksurgery.example/contact-us"}}
    f = SiteContactFinder(http=None)
    monkeypatch.setattr(f, "_fetch", lambda u: pages.get(u))
    got = f.practice("https://www.oaksurgery.example/")
    assert got["emails"] == ["oak.surgery@nhs.net", "admin@oaksurgery.example"]
    assert got["managers"] == [("Jane Smith", "Practice Manager")]


def test_employers_add_site_contacts_careers_and_vacancies(monkeypatch):
    from recruit_scraper.candidates import contacts
    monkeypatch.setattr(contacts.SiteContactFinder, "employer", lambda self, u, kw=None, **k: {
        "emails": ["hello@oak.example"], "phones": ["+44 161 496 0000"], "managers": [("Jane Smith", "Practice Manager")],
        "source_url": "https://oak.example/contact", "careers_url": "https://oak.example/careers",
        "vacancies": [{"title": "Practice Nurse", "url": "https://oak.example/careers/pn", "pay": "£32,000",
                       "closing_date": "2026-12-01", "page": "https://oak.example/careers"}]})
    f = EmployerLeadsFinder(registers=False)
    leads = [EmployerLead(name="Oak", employer_type="GP practice", website="https://oak.example", source="CQC"),
             EmployerLead(name="Elm", employer_type="GP practice", website="")]
    f.practice_sites_contacts(leads, lambda m: None, "practice nurse")
    assert leads[0].email == "hello@oak.example" and leads[0].contact_name == "Jane Smith" and leads[0].phone
    assert "practice's own website" in leads[0].contact_role and "employer website" in leads[0].source
    assert "https://oak.example/contact" in leads[0].source_url
    assert leads[0].careers_url.endswith("/careers") and leads[0].vacancy == "Practice Nurse (£32,000 · closes 2026-12-01)"
    assert leads[0].vacancy_url.endswith("/careers/pn") and leads[1].vacancy == ""


# --- NHS Jobs ---------------------------------------------------------------------------------------------------

def test_nhs_jobs_queries_synonyms_and_locum_contracts():
    q = [p for p, _ in NHSJobsSource(None, keyword="locum practice nurse")._queries()]
    assert q[0] == {"keyword": "practice nurse", "contractType": "Locum"} and q[1]["contractType"] == "Bank"
    assert {"keyword": "general practice nurse"} in q
    assert all("contractType" not in p for p in NHSJobsSource(None, keyword="practice nurse")._queries()[0][:1])


def test_nhs_jobs_drops_far_away_results_for_places_it_does_not_cover():
    class H:
        def __init__(self):
            self.session = self

        def get_json(self, url, params=None):
            return {"result": [{"name_1": "Glasgow", "latitude": 55.86, "longitude": -4.25}]}

        def _throttle(self, host):
            pass

        def post(self, url, json=None, timeout=None):
            r = _Resp(b"")
            r.json = lambda: {"result": [{"query": "WV58DX", "result": {"latitude": 52.5, "longitude": -2.2}},
                                         {"query": "G11XQ", "result": {"latitude": 55.86, "longitude": -4.25}}]}
            return r
    src = NHSJobsSource(H(), keyword="practice nurse", location="Glasgow")
    src.info = []
    jobs = [Job(title="Practice Nurse", location="Wolverhampton, WV5 8DX", url="a"),
            Job(title="Practice Nurse", location="Glasgow, G1 1XQ", url="b")]
    assert [j.url for j in src._near(jobs)] == ["b"] and "dropped" in src.info[0]


def test_nhs_jobs_sends_a_town_as_its_centre_postcode():
    class H:
        def get_json(self, url, params=None):
            if url.endswith("/places"):
                return {"result": [{"name_1": "Cardiff", "latitude": 51.48, "longitude": -3.18, "country": "Wales"}]}
            assert params["lat"] == 51.48
            return {"result": [{"postcode": "CF10 1GP"}]}
    src = NHSJobsSource(H(), keyword="practice nurse", location="Cardiff")
    assert src._feed_location("Cardiff") == "CF10 1GP"
    assert src._feed_location("M1 1AE") == "M1 1AE" and src._feed_location("LS1") == "LS1"


# --- locum vacancies ----------------------------------------------------------------------------------------------

def test_locum_vacancy_matching_is_strict_on_contract_and_wide_on_primary_care_titles():
    from recruit_scraper.core import keyword_hit
    J = lambda t, ty: Job(title=t, employment_type=ty, url="u")
    kw = "locum practice nurse"
    assert keyword_hit(J("Locum Advanced Nurse Practitioner", "locum"), kw)
    assert keyword_hit(J("Bank District Nurse", "bank"), kw)
    assert keyword_hit(J("Sessional Immunisation Nurse", None), kw)              # 'sessional' counts as locum work
    assert not keyword_hit(J("Practice Nurse", "permanent"), kw)                # a permanent post isn't locum
    assert not keyword_hit(J("Staff Nurse - Ward 4", "bank"), kw)               # not primary / community nursing
    assert not keyword_hit(J("Nurse Practitioner Pre-op Assessment", "fixed-term"), kw)
    assert not keyword_hit(J("Locum Advanced Nurse Practitioner", "locum"), "practice nurse")


def test_pay_hours_and_closing_date_from_nhs_feed():
    from recruit_scraper.sources.nhs import pay_and_hours
    assert pay_and_hours("£22.50", "Salary: £22.50 per hour Hours: Bank Help us") == ("£22.50 per hour", "Bank")
    assert pay_and_hours("", "We pay £45 per session for 2 sessions per week") == ("£45 per session",
                                                                                     "2 sessions per week")
    import xml.etree.ElementTree as ET
    v = ET.fromstring("<vacancyDetails><title>Bank Practice Nurse</title><employer>Op Health</employer><type>Bank"
                      "</type><salary>£22.50</salary><closeDate>2026-11-13</closeDate><postDate>2026-10-15T05:30:44"
                      "</postDate><url>https://www.jobs.nhs.uk/candidate/jobadvert/E1</url><description>Salary: £22.50"
                      " per hour Hours: Bank</description></vacancyDetails>")
    j = NHSJobsSource._job(v)
    assert j.closing_date == "2026-11-13" and j.hours == "Bank" and j.salary_raw == "£22.50 per hour"
    assert j.employment_type == "bank" and j.salary_period == "hour"


def test_nhs_jobs_locum_search_asks_for_locum_and_bank_across_primary_care_titles():
    q = [p for p, _ in NHSJobsSource(None, keyword="locum practice nurse", location="UK")._queries()]
    kws = {p["keyword"] for p in q if p.get("contractType") == "Locum"}
    assert {"practice nurse", "nurse practitioner", "district nurse", "community nurse"} <= kws
    assert sum(1 for p in q if p.get("contractType") == "Bank") == len(kws)


SCOT_CARD = '''<div tabindex="-1" class="card mb-7 mb-md-20 ">
<a tabindex="0" class="mb-15" href="/Job/JobDetail?JobId=275085" data-testid="x">
    Practice Nurse / General Practice Nurse
</a>
<p class="jobdetailsitem salary"><strong>Salary:</strong> &#xA3;25.50 per hour</p>
<p class="jobdetailsitem closingdate"><strong>Closing date:</strong> 24/11/2026</p>
<p class="jobdetailsitem location"><strong>Location:</strong> See below</p>
<p class="jobdetailsitem employmenttype"><strong>Employment type:</strong> Staff Bank</p>
<p class="jobdetailsitem hours"><strong>Hours per week:</strong> 30 </p>
<p class="jobdetailsitem livedate"><strong>Live date:</strong> 10/11/2026</p>
<p class="jobdetailsitem school"><strong>Employer (NHS Board):</strong> NHS Fife   </p>
<p class="jobdetailsitem shift"><strong>Department:</strong> Park Road Practice Rosyth </p>
</div><input id="totalMatchRecords" value="1" />'''

HSC_PAGE = '''<article class="job-result Standard" data-id="1" data-category="Armagh-GP-Federation Band-6 Nursing">
<h2><a class="font-black" href="/Job/1/gpn">General Practice Nurse (temporary)</a></h2>
<ul class="job-overview"><li><strong>Salary: </strong>&#163;38,682 - &#163;46,580</li>
<li><strong>Location: </strong>Armagh</li><li><strong>Contract Type: </strong>Temporary</li></ul>
<div class="job-closing"><strong>Closing:</strong> Wed, 18 November 2026 5:00 PM</div></article>'''


def test_scotland_and_ni_job_sites_parse(monkeypatch):
    from recruit_scraper.sources import nations
    from recruit_scraper.sources import SOURCES
    assert {"nhsscotland", "hscni"} <= set(SOURCES)

    class H:
        def __init__(self, body):
            self.body, self.calls = body, []

        def get(self, url, params=None, **kw):
            self.calls.append(params)
            r = type("R", (), {})()
            r.text = self.body
            return r
    monkeypatch.setattr(nations, "geocode_place", lambda http, loc: (56.0, -3.4, "Scotland"))
    h = H(SCOT_CARD)
    jobs = list(nations.NHSScotlandSource(h, keyword="locum practice nurse", location="Dunfermline").fetch())
    j = jobs[0]
    assert j.title == "Practice Nurse / General Practice Nurse" and j.company == "NHS Fife"
    assert j.employment_type == "bank" and j.closing_date == "2026-11-24" and j.hours == "30 hours per week"
    assert j.salary_raw == "£25.50 per hour" and j.url.endswith("JobId=275085") and h.calls[0]["Miles"] == 15
    monkeypatch.setattr(nations, "geocode_place", lambda http, loc: (53.8, -1.5, "England"))
    src = nations.NHSScotlandSource(H(SCOT_CARD), keyword="practice nurse", location="Leeds")
    assert list(src.fetch()) == [] and "Scotland only" in src.info[0]
    monkeypatch.setattr(nations, "geocode_place", lambda http, loc: (54.6, -5.9, "Northern Ireland"))
    jobs = list(nations.HSCNIJobsSource(H(HSC_PAGE), keyword="locum practice nurse", location="Belfast").fetch())
    assert jobs[0].title.startswith("General Practice Nurse") and jobs[0].employment_type == "temporary"
    assert jobs[0].closing_date == "2026-11-18" and jobs[0].url == "https://jobs.hscni.net/Job/1/gpn"


def _feed(titles, total_pages):
    rows = "".join(f"<vacancyDetails><id>{t}</id><url>https://www.jobs.nhs.uk/v/{t}</url><title>{t.split('#')[0]}"
                   f"</title><employer>E</employer><type>Locum</type><locations><location>Leeds, LS1 4AP</location>"
                   f"</locations><description>locum</description></vacancyDetails>" for t in titles)
    return f"<root><totalPages>{total_pages}</totalPages>{rows}</root>".encode()


def test_nhs_jobs_stops_paging_a_search_after_three_pages_with_nothing_relevant():
    calls = []

    class H:
        def get(self, url, params=None, headers=None):
            calls.append((params["keyword"], params.get("contractType"), params["page"]))
            p = params["page"]
            titles = [f"Locum Practice Nurse#{params['keyword']}{params.get('contractType')}{p}"] if p == 1 else \
                [f"Ward Clerk#{params['keyword']}{params.get('contractType')}{p}"]
            return types.SimpleNamespace(content=_feed(titles, 8))
    src = NHSJobsSource(H(), keyword="locum practice nurse", location="UK")
    jobs = list(src.fetch())
    first = [c for c in calls if c[:2] == ("practice nurse", "Locum")]
    assert [c[2] for c in first] == [1, 2, 3, 4]         # page 1 relevant, 2-4 not -> stop, not all 8 pages
    assert all("Practice Nurse" in j.title for j in jobs[:3])


def test_nhs_jobs_asks_again_when_the_feed_answers_404():
    n = {"calls": 0}

    class H:
        def get(self, url, params=None, headers=None):
            n["calls"] += 1
            if n["calls"] == 1:
                r = _Resp(b"", 404)
                raise requests.HTTPError("404", response=r)
            return types.SimpleNamespace(content=_feed(["Practice Nurse#a"], 1))
    src = NHSJobsSource(H(), keyword="practice nurse", location="UK")
    src.retry_pause = 0
    assert src._page({"keyword": "practice nurse", "page": 1}) is not None and n["calls"] == 2
    src2 = NHSJobsSource(H(), keyword="practice nurse", location="UK")
    src2.retry_pause = 0

    class Always404:
        def get(self, url, params=None, headers=None):
            raise requests.HTTPError("404", response=_Resp(b"", 404))
    src2.http = Always404()
    assert src2._page({"page": 1}) is None and "did not answer" in src2.warnings[0]


def test_locum_employer_list_keeps_a_share_for_other_kinds():
    f = EmployerLeadsFinder.__new__(EmployerLeadsFinder)
    f.max_leads = 30
    gps = [EmployerLead(name=f"GP {i}", employer_type="GP practice") for i in range(40)]
    ag = [EmployerLead(name=f"OOH {i}", employer_type="Urgent care / walk-in / out-of-hours") for i in range(10)]
    out = f._cap(gps + ag, ["gp", "urgent_care", "independent"])
    assert len(out) == 30 and sum(l.employer_type.startswith("Urgent") for l in out) == 5
    assert f._cap(gps + ag, [])[-1].name == "GP 29"


def test_locum_search_through_core_keeps_locum_anp_and_bank_acp_ads(monkeypatch):
    from recruit_scraper import core
    from recruit_scraper.sources.base import Source

    class Fake(Source):
        name = "fakenhs"
        server_side_location = True
        seen_kw = []

        def fetch(self):
            Fake.seen_kw.append(self.keyword)
            yield Job(title="Advanced Nurse Practitioner", company="Omnes", employment_type="locum", url="u1",
                      location="Leeds", description="Locum ANP sessions in general practice")
            yield Job(title="Bank Advanced Clinical Practitioner - Primary Care", company="PC24", url="u2",
                      employment_type="bank", location="Leeds", description="bank")
            yield Job(title="Advanced Nurse Practitioner - Pre-op Assessment", company="Trust", url="u3",
                      employment_type="bank", location="Leeds", description="bank")
            yield Job(title="Practice Nurse", company="Surgery", employment_type="permanent", url="u4",
                      location="Leeds", description="permanent")
    monkeypatch.setitem(core.SOURCES, "fakenhs", Fake)
    res = core.search(["fakenhs"], keywords=["locum practice nurse"], locations=["Leeds"], delay=0)
    assert sorted(j.url for j in res.jobs) == ["u1", "u2"]
    Fake.seen_kw.clear()
    res = core.search(["fakenhs"], keywords=["practice nurse"], locations=["Leeds"], delay=0, contract="temporary")
    assert sorted(j.url for j in res.jobs) == ["u1", "u2"] and Fake.seen_kw == ["locum practice nurse"]


def test_nhs_jobs_gives_up_quickly_when_the_feed_keeps_failing():
    calls = []

    class Always404:
        def get(self, url, params=None, headers=None):
            calls.append(params.get("keyword"))
            raise requests.HTTPError("404", response=_Resp(b"", 404))
    src = NHSJobsSource(Always404(), keyword="locum practice nurse", location="UK")
    src.retry_pause = 0
    assert list(src.fetch()) == []
    assert len(calls) == 2 * (src.retries + 1) and any("stopped answering" in i for i in src.info)


def test_nhs_jobs_is_skipped_with_a_note_for_scotland_and_ni(monkeypatch):
    from recruit_scraper.sources import nhs
    monkeypatch.setattr(nhs, "geocode_place", lambda http, loc: (55.86, -4.25, "Scotland"))

    class NoCalls:
        def get(self, *a, **k):
            raise AssertionError("feed should not be asked")
    src = NHSJobsSource(NoCalls(), keyword="locum practice nurse", location="Glasgow")
    assert list(src.fetch()) == [] and "NHS Scotland Jobs" in src.info[0]


def test_northern_ireland_towns_are_placed_without_postcodes_io():
    from recruit_scraper.sources.nhs import geocode_place
    assert geocode_place(None, "Belfast")[2] == "Northern Ireland"
    assert geocode_place(None, "Derry, Northern Ireland")[2] == "Northern Ireland"
