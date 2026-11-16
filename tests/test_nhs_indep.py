"""Direct employers only (no agencies), complete NHS Jobs paging, independent healthcare registers, NHS / Independent."""
import io
import types

import pytest
import requests

from recruit_scraper import agencies, core
from recruit_scraper.candidates import registers as R
from recruit_scraper.candidates.employers import (EmployerLead, assign_sector, drop_agencies, employer_kinds,
                                                  is_locum_search, locum_priority)
from recruit_scraper.models import Job
from recruit_scraper.sector import job_sector
from recruit_scraper.sources import SOURCES
from recruit_scraper.sources.base import Source
from recruit_scraper.sources.nhs import NHSJobsSource


# --- no agencies ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["Reed", "Hays Specialist Recruitment", "Pertemps Medical", "Randstad Care", "Medacs Healthcare",
                                  "Pulse Healthcare", "ID Medical", "Thornbury Nursing Services", "Your World Healthcare",
                                  "Sanctuary Personnel", "NHS Professionals", "Acme Nursing Agency", "Smith Staffing Ltd",
                                  "Anytime Recruitment", "Locum's Nest", "Teaching Personnel", "Totaljobs"])
def test_agency_names_are_detected(name):
    assert agencies.is_agency_name(name)


@pytest.mark.parametrize("name", ["Royal Free London NHS Foundation Trust", "Oaks Medical Centre", "Sanctuary Care", "Bluebird Care",
                                  "Barchester Healthcare", "Hayes Dental", "Manchester University NHS FT Bank", "Care UK",
                                  "Castle PCN", "St Mary's Hospice", "", None])
def test_direct_employers_are_not_agencies(name):
    assert not agencies.is_agency_name(name)


def test_ad_text_that_says_it_is_placed_for_a_client():
    assert agencies.is_agency_text("Our client, a busy GP practice in Leeds, is seeking a practice nurse")
    assert agencies.is_agency_text("We are recruiting on behalf of our client, a care group")
    assert agencies.is_agency_text("We are a recruitment agency specialising in healthcare")
    assert not agencies.is_agency_text("We advise our clients on tax. Join our friendly practice team")
    assert not agencies.is_agency_text("Join the bank at the Trust: flexible shifts for our patients")


def test_agency_ads_are_dropped_from_a_search_and_counted(monkeypatch):
    class Fake(Source):
        name = "fakeads"

        def fetch(self):
            yield Job(title="Practice Nurse", company="Oaks Medical Centre", url="https://x/1", description="direct")
            yield Job(title="Practice Nurse", company="Pulse Healthcare", url="https://x/2", description="agency")
            yield Job(title="Practice Nurse", company="Confidential", url="https://x/3",
                      description="Our client, a GP practice, is looking for a practice nurse")
            yield Job(title="Practice Nurse", company="Hays", url="https://x/4", description="x")
    monkeypatch.setitem(core.SOURCES, "fakeads", Fake)
    res = core.search(["fakeads"], keywords=["practice nurse"], max_results=50)
    assert [j.company for j in res.jobs] == ["Oaks Medical Centre"]
    assert res.agencies_removed == 3 and res.sources[0].agencies == 3
    assert "3 agency ads left out" in res.sources[0].message


def test_agency_boards_are_not_sources():
    assert "reed" not in SOURCES and "adzuna" not in SOURCES
    assert "reed" not in core.default_sources([], []) and "adzuna" not in core.default_sources(["greenhouse:x"], ["https://x"])


def test_agency_employers_are_dropped_and_never_ranked_up():
    leads = [EmployerLead(name="Oaks Surgery", employer_type="GP practice"),
             EmployerLead(name="Acme Nursing Agency", employer_type="Nurse / staffing agency"),
             EmployerLead(name="Bright Staffing Ltd", employer_type="Home care agency"),
             EmployerLead(name="Sunrise Care Ltd", employer_type="Home care agency"),
             EmployerLead(name="Pulse Healthcare", employer_type="Clinic")]
    kept, n = drop_agencies(leads)
    assert [l.name for l in kept] == ["Oaks Surgery", "Sunrise Care Ltd"] and n == 3
    assert locum_priority(EmployerLead(name="Acme Nursing Agency", employer_type="Nurse / staffing agency")) == 1
    assert locum_priority(EmployerLead(name="Leeds Out of Hours Service", employer_type="Urgent care")) == 0
    assert locum_priority(EmployerLead(name="Mid Yorks NHS Trust", employer_type="Hospital")) == 0   # a trust's own bank
    assert "nurse_agency" not in employer_kinds("locum practice nurse") and is_locum_search("bank healthcare assistant")


def test_registers_never_return_agencies():
    cqc = R.CQCDirectory()
    ag = R.RegRow(name="Acme Nursing Agency Ltd", employer_type="", postcode="M1 1AE", tags="Homecare agencies",
                  extra="Acme Ltd§Services for everyone")
    care = R.RegRow(name="Sunrise Care Ltd", employer_type="", postcode="M1 1AE", tags="Homecare agencies",
                    extra="Sunrise Care Ltd§Dementia")
    assert not cqc.wanted(ag, ["home_care", "independent"], "care worker") and cqc.wanted(care, ["home_care"], "care worker")
    ci = R.CareInspectorate()
    assert not ci.wanted(R.RegRow(name="Bay Nurses", employer_type="", postcode="G1 1XQ", tags="Nurse Agency|"),
                         ["care_home"], "locum nurse")
    rq = R.RQIARegister()
    assert not rq.wanted(R.RegRow(name="Bay Nurses", employer_type="", postcode="BT1 1AA", tags="Nursing Agency (NA)"),
                         ["care_home", "independent"], "locum nurse")


# --- NHS Jobs: complete paging, cache, link ---------------------------------------------------------------------------

def _feed(rows, total_pages):
    body = "".join(f"<vacancyDetails><id>{r}</id><reference>{r}</reference><url>https://beta.jobs.nhs.uk/candidate/jobadvert/{r}</url>"
                   f"<title>{t}</title><employer>{e}</employer><type>{ty}</type><salary>{sal}</salary>"
                   f"<closeDate>2026-12-31</closeDate><locations><location>Leeds, LS1 4AP</location></locations>"
                   f"<description>{d}</description></vacancyDetails>" for r, t, e, ty, sal, d in rows)
    return f"<root><totalPages>{total_pages}</totalPages>{body}</root>".encode()


def test_nhs_jobs_pages_past_ten_while_ads_keep_fitting_and_uses_public_links():
    calls = []

    class H:
        def get(self, url, params=None, headers=None, interval=None):
            calls.append((params["keyword"], params["page"], interval))
            p = params["page"]
            rows = [(f"R{params['keyword']}-{p}-{i}", "Practice Nurse", "Oaks Medical Centre", "Permanent",
                     "£34428 to £41659", "Band 6 role. Hours: 30 hours per week") for i in range(3)] if p <= 25 else \
                [(f"X{p}", "Ward Clerk", "Trust", "Permanent", "£20000", "")]
            return types.SimpleNamespace(content=_feed(rows, 40))
    src = NHSJobsSource(H(), keyword="practice nurse", location="UK")
    src.budget_seconds = 1000
    jobs = list(src.fetch())
    main = [c for c in calls if c[0] == "practice nurse"]
    assert max(c[1] for c in main) == 28 and len(main) == 28          # 25 relevant pages + 3 misses, not stopped at 10
    assert all(c[2] == 1.5 for c in calls)                              # paced, one request at a time
    assert jobs[0].url.startswith("https://www.jobs.nhs.uk/candidate/jobadvert/")
    assert "(Band 6)" in jobs[0].salary_raw and jobs[0].closing_date == "2026-12-31"
    assert src.stats["title_matches"] >= 75 and any("pages read" in i for i in src.info)


def test_nhs_jobs_same_ad_from_two_searches_is_listed_once_and_pages_are_cached():
    calls = []

    class H:
        def get(self, url, params=None, headers=None, interval=None):
            calls.append((params["keyword"], params["page"]))
            return types.SimpleNamespace(content=_feed([("SAME-1", "Practice Nurse", "Oaks", "Permanent", "£30000", "")], 1))
    s1 = NHSJobsSource(H(), keyword="practice nurse", location="UK")
    jobs = list(s1.fetch())
    assert [j.url for j in jobs].count("https://www.jobs.nhs.uk/candidate/jobadvert/SAME-1") == 1
    n = len(calls)
    s2 = NHSJobsSource(H(), keyword="practice nurse", location="UK")
    assert len(list(s2.fetch())) == 1 and len(calls) == n and s2.stats["cached_pages"] >= 1   # served from the cache


def test_nhs_jobs_backs_off_on_429_and_404_then_succeeds():
    n = {"c": 0}

    class _R:
        def __init__(self, code):
            self.status_code = code

    class H:
        def get(self, url, params=None, headers=None, interval=None):
            n["c"] += 1
            if n["c"] <= 2:
                raise requests.HTTPError("x", response=_R(429 if n["c"] == 1 else 404))
            return types.SimpleNamespace(content=_feed([("A1", "Practice Nurse", "Oaks", "Permanent", "", "")], 1))
    s = NHSJobsSource(H(), keyword="practice nurse", location="UK")
    s.retry_pause = 0
    assert s._page({"keyword": "practice nurse", "page": 1}) is not None and n["c"] == 3 and not s.warnings


# --- independent healthcare ----------------------------------------------------------------------------------------

def test_cqc_independent_clinics_and_private_gps():
    cqc = R.CQCDirectory()
    mk = lambda name, types_, prov, spec="Caring for adults under 65 yrs": R.RegRow(
        name=name, employer_type="", postcode="W1G 9QW", tags=types_, extra=f"{prov}§{spec}")
    clinic = mk("Harley Street Aesthetic Clinic", "Clinic", "HSAC Ltd")
    assert cqc.wanted(clinic, ["gp", "independent"], "practice nurse")
    assert clinic.sector == "Independent" and clinic.employer_type == "Aesthetic clinic"
    priv = mk("Kensington Private GP", "Doctors/GPs", "KPGP Ltd")
    assert cqc.wanted(priv, ["gp", "independent"], "practice nurse") and priv.sector == "Independent"
    drx = mk("Dr Lee and Partners", "Doctors/GPs", "Dr Lee and Partners")           # looks like an NHS partnership: not guessed private
    assert cqc.wanted(drx, ["gp", "independent"], "practice nurse") and drx.sector == ""
    nhs_gp = mk("Twyford Surgery", "Doctors/GPs", "Twyford Surgery", "Services for everyone")
    assert cqc.wanted(nhs_gp, ["gp", "independent"], "practice nurse") and nhs_gp.sector == ""   # an ordinary GP practice
    nhs_clinic = mk("Leeds Community Clinic", "Clinic", "Leeds Teaching Hospitals NHS Trust")
    assert not cqc.wanted(nhs_clinic, ["independent"], "practice nurse")                           # NHS provider
    hosp = mk("Nuffield Health Tees Hospital", "Hospital", "Nuffield Health")
    assert cqc.wanted(hosp, ["independent", "hospital"], "practice nurse") and hosp.sector == "Independent"
    care_home = mk("Oak Lodge", "Rehabilitation (illness/injury)", "Oak Care Ltd")
    assert not cqc.wanted(care_home, ["independent"], "practice nurse")                           # a care home, not a clinic


def test_hiw_and_rqia_independent_registers():
    csv_text = ('"Service name","Type of establishment","Services provided","Address line 1","Address line 2",Postcode,'
                '"Last inspection date",Telephone,Sector\n'
                '"Cardiff Laser Clinic",Hospitals,"Laser","1 High St",Cardiff,CF10 1AA,22/10/2025,"\'02920 111222",Private/Independent\n'
                '"Cardiff Surgery","GP Surgeries","GP services","2 Low St",Cardiff,CF10 1AB,,"\'02920 333444",NHS\n'
                '"Bay Dental","Dental Practices",Dental,"3 Bay Rd",Cardiff,CF10 1AC,,"\'02920 555666","NHS, Private/Independent"\n')

    class Http:
        def get(self, url, **kw):
            return types.SimpleNamespace(iter_lines=None, content=csv_text.encode(), text=csv_text, status_code=200,
                                         raise_for_status=lambda: None)
    reg = R.HIWRegister()
    rows = [R.RegRow(name="Cardiff Laser Clinic", employer_type="Hospitals", postcode="CF10 1AA", phone="02920 111222",
                     tags="Hospitals|Private/Independent|Laser", source="HIW"),
            R.RegRow(name="Cardiff Surgery", employer_type="GP Surgeries", postcode="CF10 1AB",
                     tags="GP Surgeries|NHS|GP services", source="HIW"),
            R.RegRow(name="Bay Dental", employer_type="Dental Practices", postcode="CF10 1AC",
                     tags="Dental Practices|NHS, Private/Independent|Dental", source="HIW")]
    got = [r for r in rows if reg.wanted(r, ["independent"], "practice nurse")]
    assert [r.name for r in got] == ["Cardiff Laser Clinic"] and got[0].employer_type == "Aesthetic clinic"
    assert [r.name for r in rows if reg.wanted(r, ["dental"], "dentist")] == ["Bay Dental"]
    assert "Wales" in reg.nations and "hiw" in [r.key for r in R.REGISTERS]
    rq = R.RQIARegister()
    mk = lambda name, cat: R.RegRow(name=name, employer_type="", postcode="BT1 1AA", tags=cat)
    ih, ic, ima = mk("Belfast Private Hospital", "Independent Hospital (IH)"), mk("City Clinic", "Independent Clinic (IC)"), \
        mk("Dr Lee Private GP", "Independent Medical Agency (IMA)")
    assert all(rq.wanted(x, ["independent"], "practice nurse") and x.sector == "Independent" for x in (ih, ic, ima))
    assert ih.employer_type == "Independent hospital" and ic.employer_type == "Independent clinic"
    assert not rq.wanted(mk("Bay Nurses", "Nursing Agency (NA)"), ["independent"], "locum nurse")


def test_sector_labels_and_kinds():
    assert employer_kinds("private practice nurse")[0] == "independent" and employer_kinds("practice nurse")[-1] == "independent"
    assert "independent" not in employer_kinds("dental nurse") and "independent" not in employer_kinds("chef")
    assert assign_sector(EmployerLead(name="Park Surgery", employer_type="GP practice", source="NHS ODS")) == "NHS"
    assert assign_sector(EmployerLead(name="Leeds NHS Trust", employer_type="Hospital", source="CQC")) == "NHS"
    assert assign_sector(EmployerLead(name="Spire Leeds", employer_type="Independent hospital", source="CQC",
                                      sector="Independent")) == "Independent"
    assert assign_sector(EmployerLead(name="Oak Lodge", employer_type="Care home", source="CQC")) == ""
    assert job_sector("nhsjobs", "Leeds Teaching Hospitals NHS Trust") == "NHS"
    assert job_sector("nhsjobs", "Practice Plus Group - Health in Justice") == "Independent"
    assert job_sector("greenhouse:monzo", "Monzo") == "" and job_sector("jsonld", "Spire Healthcare") == "Independent"
