"""Scraping helpers (t29u): Protego robots rules, careers / contact link discovery, vacancies on employer sites, RSS feeds,
schema.org contact details, Teaching Vacancies open API."""
import types

import pytest
import requests

from recruit_scraper import crawl
from recruit_scraper.candidates import contacts
from recruit_scraper.candidates.contacts import SiteContactFinder, parse_page
from recruit_scraper.candidates.employers import EmployerLeadsFinder, EmployerLead, employer_kinds
from recruit_scraper.http import HttpClient, RobotsDisallowed
from recruit_scraper.models import Job
from recruit_scraper.sources import SOURCES
from recruit_scraper.sources.feeds import RSSSource
from recruit_scraper.sources.gov import TeachingVacanciesSource


class _R:
    def __init__(self, body="", status=200, ctype="text/html", url="https://x.example/"):
        self.text, self.content, self.status_code, self.url = body, body.encode() if isinstance(body, str) else body, status, url
        self.headers = {"Content-Type": ctype}

    def json(self):
        import json
        return json.loads(self.content)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code), response=self)


# --- robots.txt (Protego) ---------------------------------------------------------------------------------------

def test_robots_wildcards_are_honoured():
    c = HttpClient(min_interval=0)
    c.session = types.SimpleNamespace(get=lambda url, timeout=None: _R(
        "User-agent: *\nDisallow: /*-jobs*?*\nDisallow: /private/\nAllow: /\n"), headers={})
    assert not c.allowed_by_robots("https://x.example/teaching-jobs?page=2")      # urllib.robotparser would allow this
    assert not c.allowed_by_robots("https://x.example/private/a")
    assert c.allowed_by_robots("https://x.example/careers")
    with pytest.raises(RobotsDisallowed):
        c.get("https://x.example/private/a", check_robots=True)


def test_robots_unreadable_or_missing():
    c = HttpClient(min_interval=0)
    c.session = types.SimpleNamespace(get=lambda url, timeout=None: _R("", 404), headers={})
    assert c.allowed_by_robots("https://a.example/anything")
    c.session = types.SimpleNamespace(get=lambda url, timeout=None: _R("", 503), headers={})
    assert not c.allowed_by_robots("https://b.example/anything")


# --- link discovery, feeds ---------------------------------------------------------------------------------------

HOME = """<html><head><link rel="alternate" type="application/rss+xml" href="/jobs.xml"></head><body>
<nav><a href="/">Home</a><a href="/get-in-touch/">Get in touch</a><a href="/join-our-team">Join our team</a></nav>
<footer><a href="https://apply.workable.com/oak">Careers</a><a href="/x.pdf">Contact pdf</a><a href="/privacy">Privacy</a></footer></body></html>"""


def test_classify_links_finds_contact_and_careers_pages():
    got = crawl.classify_links(HOME, "https://oak.example/")
    assert got["contact"] == ["https://oak.example/get-in-touch/"]
    assert "https://oak.example/join-our-team" in got["careers"] and "https://apply.workable.com/oak" in got["careers"]
    assert crawl.discover_feeds(HOME, "https://oak.example/") == ["https://oak.example/jobs.xml"]


def test_vacancies_from_jsonld_and_from_headings():
    ld = ('<script type="application/ld+json">{"@type":"JobPosting","title":"Locum Practice Nurse","datePosted":"2026-11-01",'
          '"validThrough":"2026-12-01","employmentType":"TEMPORARY","hiringOrganization":{"name":"Oak"},'
          '"baseSalary":{"currency":"GBP","value":{"minValue":28,"maxValue":30,"unitText":"HOUR"}}}</script>')
    got = crawl.vacancies_on_page(f"<html><body>{ld}</body></html>", "https://oak.example/jobs", "locum practice nurse")
    assert len(got) == 1 and got[0].closing_date == "2026-12-01" and got[0].salary_min == 28
    page = ("<html><body><h2>Vacancies</h2><ul><li><a href='/v/pn'>Practice Nurse - 24 hours</a></li><li>Receptionist</li>"
            "<li>We are a friendly practice looking for a practice nurse to join our team and help us</li></ul></body></html>")
    got = crawl.vacancies_on_page(page, "https://oak.example/jobs", "practice nurse")
    assert [(j.title, j.url) for j in got] == [("Practice Nurse - 24 hours", "https://oak.example/v/pn")]
    assert crawl.vacancies_on_page(page, "https://oak.example/jobs", "locum practice nurse") == []   # not a locum ad
    assert crawl.vacancies_on_page("<html><body><p>Sorry, no current vacancies.</p><a>Practice Nurse</a></body></html>",
                                   "https://oak.example/jobs", "practice nurse") == []


def test_parse_feed_reads_rss_and_atom():
    rss = (b"<?xml version='1.0'?><rss version='2.0'><channel><title>Oak Jobs</title><item><title>Locum Practice Nurse</title>"
           b"<link>https://oak.example/j/1</link><description>&lt;p&gt;Bank shifts, apply to jobs@oak.example&lt;/p&gt;"
           b"</description><pubDate>Tue, 10 Nov 2026 10:00:00 GMT</pubDate></item></channel></rss>")
    j = crawl.parse_feed(rss, "https://oak.example/f.xml")[0]
    assert (j.title, j.company, j.posted_date, j.contact_email, j.source) == (
        "Locum Practice Nurse", "Oak Jobs", "2026-11-10", "jobs@oak.example", "rss:oak.example")
    atom = (b"<?xml version='1.0'?><feed xmlns='http://www.w3.org/2005/Atom'><title>T</title><entry><title>Chef</title>"
            b"<link href='https://t.example/c'/><updated>2026-11-01T10:00:00Z</updated><summary>Kitchen</summary></entry></feed>")
    assert crawl.parse_feed(atom, "https://t.example/a")[0].url == "https://t.example/c"


def test_rss_source_follows_feed_links_from_a_careers_page():
    rss = (b"<?xml version='1.0'?><rss version='2.0'><channel><title>Oak</title><item><title>Bank Practice Nurse</title>"
           b"<link>https://oak.example/j/1</link></item></channel></rss>")

    class H:
        def get(self, url, check_robots=False, headers=None, **kw):
            assert check_robots
            if url.endswith("jobs.xml"):
                return _R(rss, ctype="application/rss+xml", url=url)
            return _R(HOME, url=url)
    jobs = list(RSSSource(H(), urls=["https://oak.example/careers"]).fetch())
    assert [j.title for j in jobs] == ["Bank Practice Nurse"]
    assert "rss" in SOURCES and "teachingvacancies" in SOURCES


# --- schema.org contact details ----------------------------------------------------------------------------------

def test_parse_page_reads_any_local_business_type_and_microdata():
    html = ("<html><body><script type='application/ld+json'>{\"@context\":\"https://schema.org\",\"@type\":\"Electrician\","
            "\"name\":\"Bolt Ltd\",\"telephone\":\"0113 496 0000\",\"email\":\"hello@bolt.example\"}</script>"
            "<script type='application/ld+json'>{\"@type\":\"AccountingService\",\"contactPoint\":[{\"@type\":\"ContactPoint\","
            "\"email\":\"mail@sums.example\",\"telephone\":\"+44 117 496 0001\"}]}</script>"
            "<div itemscope itemtype='https://schema.org/Restaurant'><span itemprop='telephone'>01273 496000</span>"
            "<a itemprop='email' href='mailto:book@cafe.example'>book</a></div></body></html>")
    got = parse_page(html, "https://bolt.example/")
    assert {"hello@bolt.example", "mail@sums.example"} <= set(got["emails"])
    assert len(got["phones"]) >= 2
    assert not parse_page('<script type="application/ld+json">{"@type":"JobPosting","title":"x","email":"a@b.co"}</script>',
                          "https://x.example/")["emails"]


# --- employer websites -------------------------------------------------------------------------------------------

def test_employer_site_finds_contact_page_careers_page_and_vacancy(monkeypatch):
    base = "https://oak.example"
    pages = {
        base: {"emails": [], "phones": [], "managers": [], "url": base + "/",
                     "contact_links": [base + "/get-in-touch/"], "careers_links": [base + "/join-us", "https://jobs.nhs.uk/o/1"]},
        base + "/get-in-touch/": {"emails": ["admin@oak.example", "x@gmail.com"], "phones": ["+44 161 496 0000"],
                                  "managers": [("Jane Smith", "Practice Manager")], "url": base + "/get-in-touch/"},
        base + "/join-us#careers": {"url": base + "/join-us", "emails": [], "phones": [], "managers": [], "vacancies": [
            {"title": "Practice Nurse - 24 hrs", "url": base + "/v/1", "description": "Practice Nurse", "company": "Oak"},
            {"title": "Receptionist", "url": base + "/v/2"}]}}
    seen = []

    def fake(self, url, careers=False):
        seen.append(url)
        return pages.get(url + ("#careers" if careers else ""))
    monkeypatch.setattr(SiteContactFinder, "_fetch", fake)
    got = SiteContactFinder(http=None).employer(base, "practice nurse")
    assert got["emails"] == ["admin@oak.example"] and got["managers"] == [("Jane Smith", "Practice Manager")]
    assert got["phones"] == ["+44 161 496 0000"] and got["careers_url"] == base + "/join-us"
    assert [v["title"] for v in got["vacancies"]] == ["Practice Nurse - 24 hrs"]
    assert not any("nhs.uk" in u for u in seen)            # a job platform is recorded, never crawled from here
    assert SiteContactFinder(http=None).employer("https://www.linkedin.com/company/x")["emails"] == []


def test_employer_kinds_cover_trades_accountants_and_law():
    assert employer_kinds("electrician") == ["trades"] and employer_kinds("accountant") == ["accountancy"]
    assert employer_kinds("solicitor") == ["law"] and employer_kinds("family lawyer") == ["barristers"]
    assert EmployerLeadsFinder._osm_kind({"craft": "electrician"}, ["trades"]) == "trades"
    assert EmployerLeadsFinder._osm_kind({"office": "accountant"}, ["accountancy"]) == "accountancy"
    assert EmployerLeadsFinder._osm_kind({"amenity": "pub"}, ["food"]) == "food"


# --- Teaching Vacancies ------------------------------------------------------------------------------------------

def _tv_job(title, pc, locality, contract="Permanent", org="St Mary's"):
    return {"@type": "JobPosting", "title": title, "datePosted": "2026-11-10", "employmentType": ["FULL_TIME"],
            "description": f"<p><strong>Contract Type: </strong>{contract}</p><p>Salary: x</p>",
            "validThrough": "2026-12-01T09:00:00+00:00", "url": f"https://tv.example/jobs/{title}-{pc}".replace(" ", "-"),
            "hiringOrganization": {"@type": "Organization", "name": org, "sameAs": "https://stmarys.example"},
            "baseSalary": {"currency": "GBP", "value": {"value": "£34,069 - £52,835 per annum", "unitText": "YEAR"}},
            "jobLocation": {"address": {"addressLocality": locality, "addressRegion": "North West", "postalCode": pc}}}


class _TVHttp:
    def __init__(self):
        self.calls = []

    def get(self, url, params=None, check_robots=False, interval=None, **kw):
        assert check_robots and "api/v1/jobs.json" in url
        self.calls.append(params["page"])
        data = {1: [_tv_job("Class Teacher", "L1 8JQ", "Liverpool"), _tv_job("Teacher of PE", "G1 1XQ", "Glasgow")],
                2: [_tv_job("Maths Teacher", "L17 7JA", "Liverpool", "Fixed term"), _tv_job("School Cook", "L1 8JQ", "Liverpool")]}
        return _R(__import__("json").dumps({"data": data[params["page"]], "meta": {"totalPages": 2}}), ctype="application/json")

    def get_json(self, url, params=None, **kw):
        return {"result": [{"outcode": "L1"}, {"outcode": "L17"}]}


def test_teaching_vacancies_filters_keyword_place_and_reads_pay_contract_closing(monkeypatch, tmp_path):
    from recruit_scraper.candidates.cache import DiskCache
    from recruit_scraper.sources import gov
    monkeypatch.setattr(gov, "geocode_place", lambda http, loc: (53.4, -2.99, "England"))
    monkeypatch.setattr(gov, "DiskCache", lambda ns, ttl: DiskCache(ns, ttl, root=tmp_path))
    h = _TVHttp()
    src = TeachingVacanciesSource(h, keyword="teacher", location="Liverpool")
    jobs = list(src.fetch())
    assert sorted(j.title for j in jobs) == ["Class Teacher", "Maths Teacher"]       # Glasgow and the cook are out
    maths = [j for j in jobs if j.title == "Maths Teacher"][0]
    assert maths.employment_type == "fixed-term" and maths.closing_date == "2026-12-01"
    assert maths.salary_raw.startswith("£34,069") and maths.location == "Liverpool, North West, L17 7JA"
    assert "stmarys.example" in maths.description
    n = len(h.calls)
    assert [j.title for j in TeachingVacanciesSource(h, keyword="teacher", location="UK").fetch()].count("Teacher of PE") == 1
    assert len(h.calls) == n                                                          # second search came from the cache


def test_teaching_vacancies_only_for_education_searches():
    src = TeachingVacanciesSource(_TVHttp(), keyword="practice nurse", location="Liverpool")
    assert list(src.fetch()) == [] and "school / college" in src.info[0] and not src.http.calls


# --- vacancy page without schema.org data ------------------------------------------------------------------------

def test_jsonld_text_fallback_uses_main_text_not_the_menu():
    from recruit_scraper.sources.jsonld import JsonLdSource
    html = ("<html><head><title>x</title></head><body><nav><a href='/'>Home</a> <a href='/jobs'>Jobs</a></nav><main>"
            "<h1>Care Assistant</h1><p>" + "We are a friendly care home looking for a care assistant to join our team. " * 6 +
            "Apply to jobs@oakcare.example.</p></main><footer>Cookies Privacy</footer></body></html>")
    job = JsonLdSource._text_job(html, "https://oakcare.example/jobs/care-assistant", "oakcare.example")
    assert job.title == "Care Assistant" and "friendly care home" in job.description and "Cookies" not in job.description
    assert job.contact_email == "jobs@oakcare.example"
    assert JsonLdSource._text_job("<html><body><h1>Jobs</h1><p>short</p></body></html>", "https://x.example/jobs/a", "x") is None


# --- health / care and locum --------------------------------------------------------------------------------------

def test_health_roles_map_to_employer_kinds():
    assert employer_kinds("mental health nurse")[0] == "mental_health"
    assert employer_kinds("nursing home nurse") == ["care_home", "home_care"]
    assert employer_kinds("support worker") == ["care_home", "home_care"]
    assert employer_kinds("locum practice nurse")[-3:] == ["urgent_care", "trust", "independent"]
    assert EmployerLeadsFinder._osm_kind({"healthcare": "hospice"}, ["hospital"]) == "hospital"


def test_cqc_nurse_agencies_are_never_listed():
    from recruit_scraper.candidates.registers import CQCDirectory, RegRow
    r = CQCDirectory()
    ag = RegRow(name="Acme Nursing Agency Ltd", employer_type="", postcode="M1 1AE", tags="Homecare agencies")
    direct = RegRow(name="Sunrise Care Ltd", employer_type="", postcode="M1 1AE", tags="Homecare agencies")
    assert not r.wanted(ag, ["home_care", "gp", "urgent_care"], "locum nurse")        # a staffing agency: another recruiter
    assert r.wanted(direct, ["home_care"], "care worker")                                # a home-care provider employs its carers
    mh = RegRow(name="Hill MH Unit", employer_type="", postcode="G1 1XQ", tags="Hospitals - Mental health/capacity")
    assert r.wanted(mh, ["mental_health"], "mental health nurse") and mh.employer_type == "Mental health hospital"


def test_bank_and_locum_pages_are_classified():
    html = ('<nav><a href="/bank">Join our nurse bank</a><a href="/careers">Careers</a><a href="/about">About</a></nav>'
            '<footer><a href="/locums">Locum opportunities</a></footer>')
    got = crawl.classify_links(html, "https://agency.example/")
    assert "https://agency.example/bank" in got["bank"] and "https://agency.example/locums" in got["bank"]
    assert "https://agency.example/careers" in got["careers"]


def test_employer_flags_locum_vacancies_and_reads_bank_page_and_feed(monkeypatch):
    base = "https://hub.example"
    pages = {
        base: {"emails": [], "phones": [], "managers": [], "url": base + "/", "contact_links": [],
               "careers_links": [base + "/careers"], "bank_links": [base + "/join-our-bank"]},
        base + "/careers#careers": {"url": base + "/careers", "emails": [], "phones": [], "managers": [], "vacancies": [],
                                    "feeds": [base + "/jobs.rss"]},
        base + "/join-our-bank#careers": {"url": base + "/join-our-bank", "emails": [], "phones": [], "managers": [],
                                          "vacancies": [{"title": "Locum Practice Nurse - sessional", "url": base + "/v/9",
                                                         "description": "Locum Practice Nurse sessional", "company": "Hub"}]}}
    monkeypatch.setattr(SiteContactFinder, "_fetch", lambda self, url, careers=False: pages.get(url + ("#careers" if careers else "")))
    monkeypatch.setattr(SiteContactFinder, "_feed_vacancies", lambda self, f, kw: [
        Job(title="Bank Practice Nurse", url=base + "/f/1", company="Hub", source="site", description="bank")])
    got = SiteContactFinder(http=None).employer(base, "locum practice nurse")
    assert got["bank_url"] == base + "/join-our-bank" and got["careers_url"] == base + "/careers"
    assert got["vacancies"][0]["type"] == "locum / bank / temporary"
    # a permanent search does not take the locum advert
    perm = SiteContactFinder(http=None).employer(base, "practice nurse")
    assert all(v["type"] != "locum / bank / temporary" or "practice nurse" in v["title"].lower() for v in perm["vacancies"])


def test_locum_searches_rank_direct_urgent_care_and_federations_first():
    from recruit_scraper.candidates.employers import locum_priority
    mk = lambda n, t: EmployerLead(name=n, employer_type=t)
    assert locum_priority(mk("Acme Nursing Agency", "Nurse / staffing agency")) == 1   # agencies are not ranked up (or listed)
    assert locum_priority(mk("Oldham GP Federation", "GP practice")) == 0
    assert locum_priority(mk("Castle PCN", "GP practice")) == 0
    assert locum_priority(mk("Central Out of Hours", "Out-of-hours GP service")) == 0
    assert locum_priority(mk("Park Surgery", "GP practice")) == 1


def test_employer_crawl_stops_at_its_deadline(monkeypatch):
    import time as _t
    f = SiteContactFinder(http=types.SimpleNamespace(get=lambda *a, **k: (_ for _ in ()).throw(AssertionError("fetched"))))
    f.deadline = _t.monotonic() - 1
    assert f._fetch("https://x.example/") is None
    got = f.employer("https://x.example/", "practice nurse")
    assert got["emails"] == [] and got["pages"] == 0


def test_stalled_stage_is_cut_at_its_time_limit():
    import time as _t
    f = EmployerLeadsFinder()
    t0 = _t.monotonic()
    assert f._bounded("Official registers", 1.0, lambda: _t.sleep(30), "partial") == "partial"
    assert _t.monotonic() - t0 < 5 and "time limit" in f.notes[-1]
    assert f._bounded("x", 5, lambda: 42, 0) == 42
    with pytest.raises(ValueError):
        f._bounded("x", 5, lambda: (_ for _ in ()).throw(ValueError("boom")), 0)
