"""Law, accountancy, care, trades, hospitality and teaching: direct employers and their own vacancies, never agencies."""
import types

import pytest

from recruit_scraper import agencies
from recruit_scraper.candidates import registers
from recruit_scraper.candidates.registers import LegalAidProviders, RegRow, _Budget
from recruit_scraper.crawl import classify_links, matching_vacancies
from recruit_scraper.models import Job
from recruit_scraper.sources import SOURCES
from recruit_scraper.sources.employer_sites import EmployerSitesSource
from recruit_scraper.sources.gov import PHASES, split_phase

LAA_ROWS = [("", "Summary of List of provider offices"), ("",),
            ("", "Firm Name", "Address Line 1", "Address Line 2", "Address Line 3", "City", "County", "Postcode",
             "Telephone Number", "Crime", "Family", "Housing"),
            ("", "JMW Solicitors LLP", "1 Byrom Place", "", "", "Manchester", "", "M3 3HG", "0345 872 6666", "Yes", "", ""),
            ("", "Family First Law", "2 King St", "", "", "Manchester", "", "M2 4WU", "0161 000 0000", "", "Yes", ""),
            ("", "Leeds Law Centre", "3 Park Row", "", "", "Leeds", "", "LS1 5HD", "0113 000 0000", "", "", "Yes")]


def test_legal_aid_directory_lists_law_firms_with_phone_and_areas(monkeypatch):
    monkeypatch.setattr(registers, "latest_link", lambda http, page, rx: "https://assets.publishing.service.gov.uk/x.xlsx")
    monkeypatch.setattr(registers, "read_xlsx", lambda http, url, budget, sheet="": LAA_ROWS)
    reg = LegalAidProviders()
    rows = list(reg.download(None, lambda oc: oc.startswith("M"), _Budget(30)))
    assert [r.name for r in rows] == ["JMW Solicitors LLP", "Family First Law"]
    jmw = rows[0]
    assert jmw.phone == "0345 872 6666" and jmw.postcode == "M3 3HG" and jmw.source == "LAA" and jmw.tags == "Crime"
    assert reg.wanted(rows[1], ["law"], "family solicitor") and not reg.wanted(jmw, ["law"], "family solicitor")
    assert reg.wanted(jmw, ["law"], "criminal defence solicitor") and reg.wanted(jmw, ["law"], "conveyancing solicitor")
    assert any(isinstance(r, LegalAidProviders) for r in registers.REGISTERS) and reg.applies(["law"], "solicitor") and "LAA" in registers.LICENCES


def test_register_agencies_are_counted_once_per_organisation_and_never_listed():
    rows = [RegRow(name="Acme Nursing Agency", employer_type="x", postcode="M1 1AA", extra="Acme Nursing Agency§"),
            RegRow(name="Acme Nursing Agency", employer_type="x", postcode="M1 1AB", extra="Acme Nursing Agency§"),
            RegRow(name="Oaks Care Home", employer_type="x", postcode="M1 1AC")]
    ag = [r for r in rows if registers._agency_row(r)]
    assert len(ag) == 2 and len({(r.extra.partition("§")[0] or r.name).lower() for r in ag}) == 1


@pytest.mark.parametrize("kw,core,hit,miss", [
    ("primary teacher", "teacher", "Class Teacher (UKS2) Hill Avenue Primary", "Teacher of Science - GCSE"),
    ("secondary teacher", "teacher", "Teacher of Science - GCSE", "EYFS Teacher"),
    ("nursery teacher", "teacher", "EYFS Teacher", "Teacher of Chemistry A-level")])
def test_school_phase_is_matched_on_the_school_not_the_title(kw, core, hit, miss):
    c, phase = split_phase(kw)
    assert c == core and phase.search(hit) and not phase.search(miss)
    assert split_phase("teacher") == ("teacher", None) and split_phase("primary")[1] is None


def test_service_pages_are_not_vacancies_but_real_titles_are():
    page = lambda t: Job(title=t, description=t, url="https://firm.test/x", source="site:firm.test")
    items = [page(t) for t in ("Divorce Solicitors Family Law", "Find a lawyer", "Our Manchester employment solicitors can help",
                               "How to choose the best solicitor", "Conveyancing Solicitor", "Trainee Solicitor (Sept 2027)")]
    assert [j.title for j in matching_vacancies(items, "solicitor")] == ["Conveyancing Solicitor", "Trainee Solicitor (Sept 2027)"]
    full = Job(title="Solicitors - Private Client", description="Full JobPosting ad text", url="u")
    assert matching_vacancies([full], "solicitor")          # a real JobPosting is trusted


def test_employment_law_pages_are_not_careers_pages():
    html = ('<a href="/services/employment-law">Employment law</a><a href="/careers">Careers</a>'
            '<a href="/work-with-us">Employment opportunities</a>')
    careers = classify_links(html, "https://firm.test/")["careers"]
    assert "https://firm.test/careers" in careers and not any("employment-law" in c for c in careers)


def test_sector_agencies_are_recruiters_but_similar_employers_are_not():
    for n in ("Sammons Legal", "Marks Sattin", "Berkeley Scott", "Randstad CPE", "Gi Group", "Temper Works", "Hays Accountancy",
              "Remedy Education"):
        assert agencies.is_agency_name(n), n
    for n in ("Irwin Mitchell", "Temper", "Florence Nightingale Hospice", "Nexus Education Trust", "Smith Plumbing Ltd",
              "Hilton London Metropole", "Bupa Care Homes"):
        assert not agencies.is_agency_name(n), n


class _Lead(types.SimpleNamespace):
    pass


def test_employer_sites_source_yields_only_direct_employer_adverts(monkeypatch):
    import recruit_scraper.candidates.employers as emp
    import recruit_scraper.sources.nhs as nhs
    firm = _Lead(name="JMW Solicitors LLP", address="Manchester", postcode="M3 3HG", email="", phone="0345 872 6666",
                 employer_type="Law firm", source="LAA + OpenStreetMap")
    agency = _Lead(name="Sammons Legal", address="", postcode="", email="", phone="", employer_type="Law firm", source="OSM")

    class FakeFinder:
        def __init__(self, **kw):
            self.stats = {"practice_sites_checked": 2}
            self.site_jobs = [(firm, {"title": "Conveyancing Solicitor", "url": "https://jmw.test/jobs/1", "pay": "£50,000",
                                      "type": "permanent / other"}),
                              (firm, {"title": "Locum Conveyancer", "url": "https://jmw.test/jobs/2", "type": "locum / bank / temporary"}),
                              (firm, {"title": "Conveyancing Solicitor", "url": "https://jmw.test/jobs/1"}),
                              (agency, {"title": "Conveyancing Solicitor", "url": "https://sammons.test/1"})]

        def find(self, title, place, radius):
            return [firm], 0
    monkeypatch.setattr(emp, "EmployerLeadsFinder", FakeFinder)
    monkeypatch.setattr(nhs, "geocode_place", lambda http, loc: (53.48, -2.24, "England"))
    src = EmployerSitesSource(http=None, keyword="conveyancing solicitor", location="Manchester")
    jobs = list(src.fetch())
    assert [(j.title, j.company) for j in jobs] == [("Conveyancing Solicitor", "JMW Solicitors LLP"),
                                                    ("Locum Conveyancer", "JMW Solicitors LLP")]
    assert jobs[1].employment_type == "temporary" and jobs[0].contact_phone == "0345 872 6666"
    assert "employersites" in SOURCES
    assert not EmployerSitesSource(http=None, keyword="practice nurse", location="Leeds").kinds()   # NHS Jobs covers it
    assert EmployerSitesSource(http=None, keyword="electrician", location="Bristol").kinds() == ["trades"]


def test_overpass_asks_mirrors_only_when_the_main_server_fails(monkeypatch):
    import requests
    from recruit_scraper.candidates import directories
    monkeypatch.setattr(directories, "_overpass_down_until", 0.0)
    calls = []

    class FirstDown:
        def get_json(self, url, **kw):
            calls.append(url)
            if url == directories.OVERPASS[0]:
                raise requests.ConnectionError("down")
            return {"elements": [{"id": 1}]}
    assert directories.overpass_elements(FirstDown(), "q", budget=10) == [{"id": 1}]
    assert calls[0] == directories.OVERPASS[0] and len(calls) >= 2


def test_sources_that_match_the_role_themselves_are_not_refiltered_on_title(monkeypatch):
    from recruit_scraper import core
    from recruit_scraper.sources.base import Source

    class Phase(Source):
        name = "fakephase"
        server_side_keyword = True

        def fetch(self):
            yield Job(title="Class Teacher", company="Oak Primary School", url="https://x/1", description="KS2")
            yield Job(title="Class Teacher", company="Teachers R Us Supply", url="https://x/2",
                      description="Our client, a primary school, needs a supply teacher")
    monkeypatch.setitem(core.SOURCES, "fakephase", Phase)
    res = core.search(["fakephase"], keywords=["primary teacher"], max_results=50)
    assert [j.company for j in res.jobs] == ["Oak Primary School"]   # kept, but agencies still removed
    assert getattr(SOURCES["teachingvacancies"], "server_side_keyword", False)
