"""Locum / bank excellence: direct employers that hire locums (with evidence), trusts' own banks, no agencies or apps."""
import threading
import time
import types

import pytest

from recruit_scraper import agencies
from recruit_scraper.candidates import locum
from recruit_scraper.candidates.contacts import is_third_party_bank, locum_cue
from recruit_scraper.candidates.employers import (EmployerLead, EmployerLeadsFinder, TYPE_LABELS, employer_kinds,
                                                  is_locum_search, locum_rank)
from recruit_scraper.core import keyword_hit
from recruit_scraper.models import Job
from recruit_scraper.sources.nhs import LOCUM_KEYWORDS, NHSJobsSource


# --- evidence from the employer's own words ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["We use our own bank of sessional GPs and locum nurses for cover.",
                                  "Join our bank of healthcare assistants today", "Ad hoc shifts are available for nurses"])
def test_locum_cue_finds_the_sentence(text):
    assert locum_cue(text)


@pytest.mark.parametrize("text", ["We do not use locums here.", "Locum insurance is available", "Opening hours 8am-6pm", ""])
def test_locum_cue_ignores_negatives_and_unrelated_text(text):
    assert locum_cue(text) == ""


def test_third_party_banks_are_never_the_employers_bank_page():
    for u in ("https://www.nhsprofessionals.nhs.uk/join", "https://lantum.com/signup", "https://www.locumsnest.co.uk/x",
              "https://patchwork.health/apply"):
        assert is_third_party_bank(u)
    assert not is_third_party_bank("https://www.leedsth.nhs.uk/staff-bank")
    for n in ("Lantum Ltd", "Patchwork Health", "LocumKit", "NHS Professionals", "Locum's Nest"):
        assert agencies.is_agency_name(n)
    assert not agencies.is_agency_name("Leeds Teaching Hospitals NHS Trust")


def test_evidence_score_strongest_wins_and_cites_the_page():
    l = EmployerLead(name="Oaks Surgery")
    ev = EmployerLeadsFinder._locum_evidence
    assert ev(l, {"locum_cue": "We use sessional GPs", "locum_cue_url": "https://oaks.test/about"}, "https://oaks.test")
    assert (l.hires_locums, l.locum_evidence_url) == (1, "https://oaks.test/about")
    assert ev(l, {"bank_url": "https://oaks.test/bank"}, "https://oaks.test") and l.hires_locums == 2
    assert ev(l, {"vacancies": [{"title": "Locum Practice Nurse", "type": "locum / bank / temporary",
                                 "url": "https://oaks.test/job/1"}]}, "x")
    assert (l.hires_locums, l.locum_evidence_url) == (3, "https://oaks.test/job/1") and "Locum Practice Nurse" in l.locum_evidence
    assert not ev(l, {"locum_cue": "weaker", "bank_url": "https://oaks.test/bank"}, "x") and l.hires_locums == 3


# --- adverts as evidence ----------------------------------------------------------------------------------------------

def _ad(title, company, loc="Leeds, LS1 4AP", url=None, src="nhsjobs", close="2026-12-01", pay="£28 per hour"):
    return Job(title=title, company=company, location=loc, url=url or f"https://www.jobs.nhs.uk/{abs(hash((title, company)))}",
               source=src, closing_date=close, salary_raw=pay, employment_type="locum")


def test_adverts_flag_known_employers_and_add_new_ones_but_never_agencies():
    leads = [EmployerLead(name="Oaks Medical Centre", employer_type="GP practice", postcode="LS1 2AB", phone="0113 1"),
             EmployerLead(name="Park Surgery", employer_type="GP practice", postcode="M1 1AA")]
    jobs = [_ad("Locum Practice Nurse", "Oaks Medical Centre"), _ad("Bank Practice Nurse", "Oaks Medical Centre"),
            _ad("Locum Practice Nurse", "Leeds Teaching Hospitals NHS Trust"),
            _ad("Locum Practice Nurse", "Pulse Healthcare"), _ad("Locum Practice Nurse", "Lantum")]
    flagged, added = locum.attach_adverts(leads, jobs, EmployerLead)
    oaks = leads[0]
    assert flagged == 1 and added == 1 and len(leads) == 3
    assert oaks.hires_locums == 3 and "Advertising now (NHS Jobs)" in oaks.locum_evidence and oaks.locum_evidence_url.startswith("https://www.jobs.nhs.uk/")
    assert leads[1].hires_locums == 0                                    # a different practice is not flagged
    trust = leads[2]
    assert trust.employer_type == TYPE_LABELS["trust"] and trust.hires_locums == 3 and trust.vacancy_type.startswith("locum")
    assert not any(agencies.is_agency_name(l.name) for l in leads)


def test_locum_rank_open_advert_then_direct_locum_employers_then_the_rest():
    adv = EmployerLead(name="Oaks Surgery", employer_type="GP practice", hires_locums=3)
    ooh = EmployerLead(name="Leeds Out of Hours Service", employer_type="Urgent care / walk-in / out-of-hours")
    bank = EmployerLead(name="Elm Surgery", employer_type="GP practice", hires_locums=2)
    plain = EmployerLead(name="Ash Surgery", employer_type="GP practice")
    assert sorted([plain, bank, ooh, adv], key=locum_rank) == [adv, bank, ooh, plain] or \
        [locum_rank(x) for x in (adv, ooh, bank, plain)] == [0, 1, 1, 2]


def test_finder_attaches_adverts_and_looks_up_trust_contacts_from_ods(monkeypatch):
    f = EmployerLeadsFinder(http=types.SimpleNamespace(), registers=False)
    f._deadline = time.monotonic() + 60
    calls = []

    def cached(url, params=None, **kw):
        calls.append((url, params))
        if params and params.get("Name"):
            return {"Organisations": [{"Name": "LEEDS TEACHING HOSPITALS NHS TRUST", "OrgId": "RR8", "PostCode": "LS9 7TF"}]}
        return {"Organisation": {"Contacts": {"Contact": [{"type": "tel", "value": "0113 243 3144"},
                                                          {"type": "http", "value": "HTTPS://WWW.LEEDSTH.NHS.UK/"}]}}}
    monkeypatch.setattr(f, "_cached_json", cached)
    box = {"jobs": [_ad("Bank Practice Nurse", "Leeds Teaching Hospitals NHS Trust")], "done": threading.Event()}
    box["done"].set()
    leads: list[EmployerLead] = []
    f._attach_adverts(leads, box, "locum practice nurse")
    assert len(leads) == 1 and leads[0].phone == "0113 243 3144" and leads[0].website == "https://www.leedsth.nhs.uk/"
    assert leads[0].ods_code == "RR8" and "NHS ODS" in leads[0].source
    assert f.stats["locum_adverts"] == 1 and f.stats["locum_adverts_new_employers"] == 1


# --- which employers, which searches ----------------------------------------------------------------------------------

def test_gp_pharmacist_and_ahp_locum_searches_get_employers():
    for t in ("locum GP", "locum pharmacist", "bank physiotherapist", "locum paramedic"):
        assert is_locum_search(t) and employer_kinds(t), t
    assert "trust" in employer_kinds("locum GP") and "gp" in employer_kinds("locum GP")
    assert "trust" not in employer_kinds("practice nurse") and not is_locum_search("practice nurse")
    assert "gp" not in employer_kinds("locum chef")


def test_gp_search_does_not_match_gp_surgery_staff():
    ok = lambda t: keyword_hit(Job(title=t, description="locum bank", company="X", url="u"), "locum GP", temporary=True)
    assert ok("Locum GP") and ok("Sessional GP") and ok("Bank General Practitioner") and ok("GP - Locum sessions")
    for t in ("GP Receptionist (bank)", "GP Practice Nurse", "Nurse - GP Practice", "Locum Doctor"):
        assert not ok(t), t


def test_nhs_jobs_locum_queries_are_contract_filtered_and_role_specific():
    class H:
        pass
    src = NHSJobsSource(H(), keyword="locum mental health nurse", location="UK")
    qs = src._queries()
    contract_qs = [q for q, _ in qs if q.get("contractType")]
    assert {"Locum", "Bank"} == {q["contractType"] for q in contract_qs}
    assert {q["keyword"] for q in contract_qs} >= {"mental health nurse", "community psychiatric nurse", "rmn"}
    src.only_contract = True
    assert all(q.get("contractType") for q, _ in src._queries())          # evidence mode: only Locum / Bank searches
    assert "gp" in LOCUM_KEYWORDS and "sessional gp" in LOCUM_KEYWORDS["gp"]


def test_locum_adverts_use_only_direct_employers_and_drop_agencies(monkeypatch):
    class FakeNHS:
        def __init__(self, http, keyword=None, location=None, **kw):
            self.keyword = keyword

        def fetch(self):
            yield _ad("Locum Practice Nurse", "Oaks Medical Centre")
            yield _ad("Locum Practice Nurse", "Medacs Healthcare")
            yield _ad("Ward Clerk", "Oaks Medical Centre")
            yield Job(title="Locum Practice Nurse", company="Confidential", url="https://x/9", source="nhsjobs",
                      description="On behalf of our client, a GP practice in Leeds", employment_type="locum")
    monkeypatch.undo()                                    # drop the conftest stub: this test exercises the real function
    import recruit_scraper.sources.nhs as nhs_mod
    monkeypatch.setattr(nhs_mod, "NHSJobsSource", FakeNHS)
    got = locum.locum_adverts("locum practice nurse", "LS1 4AP", 15, seconds=5, nations={"England"})
    assert [j.company for j in got] == ["Oaks Medical Centre"]


def test_patient_registration_links_are_not_staff_bank_pages():
    from recruit_scraper.crawl import classify_links
    html = ('<a href="/new-patients.aspx">Register with us</a><a href="/registering-with-us/">Registering</a>'
            '<a href="/staff-bank">Join our bank</a><a href="/locum-sessions">Locum sessions</a>'
            '<a href="/register-your-interest-jobs">Register your interest</a>')
    bank = classify_links(html, "https://oaks.nhs.uk/")["bank"]
    assert "https://oaks.nhs.uk/staff-bank" in bank and "https://oaks.nhs.uk/locum-sessions" in bank
    assert not any("patients" in b or "registering" in b for b in bank)


def test_adverts_do_not_match_a_longer_unrelated_name_and_nhs_boards_are_trusts():
    leads = [EmployerLead(name="Herne Bay Community Radio Association", employer_type="Charity (care)")]
    flagged, added = locum.attach_adverts(leads, [_ad("Bank Healthcare Assistant", "Herne Bay", loc="Herne Bay, CT6 5AA"),
                                                  _ad("Bank Healthcare Assistant", "NHS Lothian", loc="Edinburgh, EH1 1AA")],
                                          EmployerLead)
    assert flagged == 0 and added == 2 and leads[0].hires_locums == 0
    assert next(l for l in leads if l.name == "NHS Lothian").employer_type == TYPE_LABELS["trust"]
    assert agencies.is_agency_name("GTD Healthcare")


def test_ods_contacts_for_employers_found_only_from_their_advert(monkeypatch):
    f = EmployerLeadsFinder(http=types.SimpleNamespace(), registers=False)
    f._deadline = time.monotonic() + 60

    def cached(url, params=None, **kw):
        if params and params.get("Name"):
            assert "Roles" not in params           # not a trust: any organisation type
            return {"Organisations": [{"Name": "KERNOW HEALTH CIC", "OrgId": "AYG", "PostCode": "TR1 1AA"},
                                      {"Name": "KERNOW HEALTH CIC - WOMENS HEALTH HUBS", "OrgId": "Y9U0B"}]}
        return {"Organisation": {"Contacts": {"Contact": [{"type": "tel", "value": "01872 000000"}]}}}
    monkeypatch.setattr(f, "_cached_json", cached)
    ad = EmployerLead(name="Kernow Health CIC", employer_type="Employer advertising locum / bank work", hires_locums=3,
                      source="NHS Jobs", source_url="https://www.jobs.nhs.uk/x")
    other = EmployerLead(name="Elm Surgery", employer_type="GP practice", hires_locums=3, source="NHS ODS")
    f.enrich_trusts([ad, other])
    assert ad.phone == "01872 000000" and ad.ods_code == "AYG" and "NHS ODS" in ad.source and not other.phone
