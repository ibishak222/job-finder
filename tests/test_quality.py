"""Quality fixes: phrase matching for jobs, people false positives, Overpass fail-fast, Europe PMC tiering."""
from types import SimpleNamespace

import pytest
import requests

from recruit_scraper.candidates import directories
from recruit_scraper.candidates.engine import rank_key
from recruit_scraper.candidates.geo import Place
from recruit_scraper.candidates.matching import RoleMatcher as RM, looks_like_business
from recruit_scraper.candidates.models import Candidate
from recruit_scraper.candidates.research import EuropePMCProvider
from recruit_scraper.candidates.social import MastodonProvider, abroad_signal
from recruit_scraper.core import keyword_hit, matches
from recruit_scraper.models import Job
from recruit_scraper.sources.more import himalayas_country

GLA = Place("G1 1XQ", 55.86, -4.25, "Glasgow City", "Glasgow", "Scotland")
LEEDS = Place("LS1 4AP", 53.80, -1.55, "Leeds", "Leeds", "England")


@pytest.fixture(autouse=True)
def _cache(tmp_path, monkeypatch):
    from recruit_scraper.candidates import cache as cache_mod
    monkeypatch.setattr(cache_mod, "CACHE_ROOT", tmp_path)
    monkeypatch.setattr(cache_mod.DiskCache.__init__, "__defaults__", (tmp_path,))
    import recruit_scraper.candidates.social as social
    monkeypatch.setattr(social.time, "sleep", lambda s: None)
    monkeypatch.setattr(directories, "_overpass_down_until", 0.0)


def test_unpaid_carers_and_false_friends_are_not_the_job():
    cw = RM("care worker")
    for bio in ("Full time carer for my son", "Single mum, carer, crafter", "Proud parent carer", "carer for my Step-Dad",
                "Unpaid carer and campaigner"):
        assert cw.match(bio).score == 0, bio
    for bio in ("Carer for Chey. NHS volunteer", "Trustee, volunteer, carer.", "Carer ME/CFS", "foster carer"):
        assert cw.match(bio).score == 0, bio                       # bare 'carer' = family / unpaid care
    assert cw.match("Unpaid carer, support worker").reason == "profile says 'support worker'"
    assert cw.match("I work as a carer").score >= 2 and cw.match("Senior carer at a care home").score >= 2
    assert cw.match("Now a dementia care worker").score == 3 and cw.match("Professional carer in Leeds").score >= 2
    chef = RM("chef")
    assert chef.match("Cheffe d'un service de maladies infectieuses").score == 0
    assert chef.match("Chef de partie at a bistro").score == 3
    assert chef.match("Monte Cook Games fan").score == 0 and chef.match("I work as a line cook").score >= 2
    assert RM("teacher").match("Lecturer in history").score == 0
    assert chef.match("NHS GP, TV Doctor, Home Cook").score == 0 and chef.match("Private cook for families").score >= 2
    assert cw.match("Student. OOH Veterinary Care Assistant").score == 0 and RM("nurse").match("Vet nurse").score == 0
    assert RM("nurse").match("Vet nurse turned NHS nurse").score > 0
    assert RM("family lawyer").match("Advocate for active travel").score == 0


def test_institution_accounts_are_businesses():
    assert looks_like_business("University of Liverpool", "Official account. One of the UK's leading universities")
    assert looks_like_business("Practice Nurse", "Practice Nurse journal is the leading publication for nurses")
    assert not looks_like_business("Jo Bloggs", "I'm a nurse at a school in Leeds")
    assert not looks_like_business("Ed K", "Academic lawyer at the University of Bristol, interested in housing law")


def _masto(accounts):
    class Api:
        def search_v2(self, q, **kw):
            return {"accounts": [{"acct": n.split()[0].lower(), "url": f"https://m.example/@{n.split()[0]}",
                                  "display_name": n, "note": f"<p>{b}</p>", "discoverable": True, "bot": False,
                                  "noindex": False, "fields": []} for n, b in accounts]}
    return MastodonProvider(fetch_websites=False, servers=("m.example",), factory=lambda s: Api(), hashtags=False)


def test_place_never_comes_from_the_display_name_and_abroad_wins():
    geo = SimpleNamespace(geocode=lambda t: None)
    p = _masto([("Guthrie Glasgow", "Apprentice electrician in Portland Oregon"),
                ("Jo | Electrician | Glasgow", "Domestic and commercial work"),
                ("Sam Smith", "Electrician, I live in DR🇩🇴, born in Glasgow")])
    assert [c.name for c in p.search("electrician", GLA, 10, None, None, [], False, geo)] == ["Jo | Electrician | Glasgow"]
    p = _masto([("Gerry Crosbie", "Accountant. A native of Meath. Dundalk & Leeds United fan"),
                ("Ann", "Accountant based in Leeds")])
    assert [c.name for c in p.search("accountant", LEEDS, 10, None, None, [], False, geo)] == ["Ann"]
    p = _masto([("Doug", "Teacher (retired!), Liverpool fanatic, Sixers fan"), ("Kim", "Teacher in Liverpool")])
    assert [c.name for c in p.search("teacher", Place("L1 8JQ", 53.40, -2.98, "Liverpool", "Liverpool", "England"),
                                     10, None, None, [], False, geo)] == ["Kim"]      # a fan isn't a resident
    assert abroad_signal("I live in 🇩🇴") and not abroad_signal("I live in 🇬🇧") and not abroad_signal("Nurse, Leeds")


def test_overpass_fails_fast_and_cools_down():
    calls = []

    class Down:
        def get_json(self, url, **kw):
            calls.append(url)
            raise requests.ConnectionError("down")
    osm = directories.OpenStreetMapProvider(http=Down(), fetch_websites=False)
    with pytest.raises(RuntimeError, match="busy"):
        list(osm.search("electrician", GLA, 5, None, None, [], False, None))
    assert len(calls) == len(directories.OVERPASS)
    with pytest.raises(RuntimeError, match="busy"):           # second search: skipped, no new waiting
        list(osm.search("plumber", GLA, 5, None, None, [], False, None))
    assert len(calls) == len(directories.OVERPASS)


def test_job_keywords_need_the_role_in_the_title():
    J = lambda t, d="": Job(title=t, description=d)
    assert not keyword_hit(J("Senior Software Engineer", "we care about every worker"), "care worker")
    assert keyword_hit(J("Care Assistant"), "care worker") and keyword_hit(J("Support Worker - nights"), "care worker")
    assert not keyword_hit(J("Sales Executive", "teacher training provided"), "teacher")
    assert keyword_hit(J("Family Solicitor"), "family lawyer") and not keyword_hit(J("Commercial Lawyer"), "family lawyer")
    assert keyword_hit(J("Staff Nurse", "Bank shifts available"), "locum nurse")
    assert not keyword_hit(J("Staff Nurse", "Permanent role"), "locum nurse")
    assert keyword_hit(J("Data Analyst", "We use Python"), "python") and not keyword_hit(J("Analyst", "pythonic"), "python")
    assert matches(J("Practice Nurse"), ["practice nurse"], [])


def test_himalayas_gets_a_country_not_a_town():
    assert himalayas_country("Glasgow") == "United Kingdom" and himalayas_country("M1 1AE") == "United Kingdom"
    assert himalayas_country("germany") == "Germany"


def test_europepmc_research_ranks_after_candidates_and_skips_contract_titles():
    real = Candidate(name="Jo", kind="candidate", provider="bluesky", fit=40)
    paper = Candidate(name="Dr A", kind="candidate", provider="europepmc", fit=90)
    assert sorted([paper, real], key=rank_key)[0] is real

    class Boom:
        def get_json(self, *a, **k):
            raise AssertionError("should not search")
    prov = EuropePMCProvider(http=Boom())
    assert list(prov.search("locum nurse", LEEDS, 10, None, None, [], False, None)) == []
    assert any("locum" in n for n in prov.notes)
