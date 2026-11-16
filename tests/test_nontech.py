"""Non-tech coverage: legal / nursing synonyms, contract type, OpenStreetMap businesses, team pages, Bluesky starter
packs, NHS Jobs feed and permanent-vs-locum tagging on the Jobs side."""
from types import SimpleNamespace

import pytest

from recruit_scraper.candidates import engine
from recruit_scraper.candidates.directories import OpenStreetMapProvider, TeamPageProvider, osm_tags_for
from recruit_scraper.candidates.engine import merge_duplicates
from recruit_scraper.candidates.geo import Place
from recruit_scraper.candidates.matching import RoleMatcher, contract_type
from recruit_scraper.candidates.models import Candidate
from recruit_scraper.candidates.social import BlueskyProvider
from recruit_scraper.core import split_contract_words
from recruit_scraper.http import RobotsDisallowed
from recruit_scraper.utils import contract_bucket, detect_employment_type, normalise_employment_type

LONDON = Place("SE1 7PB", 51.50, -0.12, "Lambeth", "London", "England", ["Lambeth", "Southwark", "Westminster"])


class FakeGeo:
    def geocode(self, text):
        return {"lambeth": (51.49, -0.11), "london": (51.50, -0.12), "leeds": (53.80, -1.55)}.get(
            (text or "").split(",")[0].strip().lower())


@pytest.fixture(autouse=True)
def _cache(tmp_path, monkeypatch):
    from recruit_scraper.candidates import cache as cache_mod
    monkeypatch.setattr(cache_mod, "CACHE_ROOT", tmp_path)
    monkeypatch.setattr(cache_mod.DiskCache.__init__, "__defaults__", (tmp_path,))
    import recruit_scraper.candidates.social as social
    monkeypatch.setattr(social.time, "sleep", lambda s: None)


def test_legal_and_nursing_synonyms_and_contract():
    sol = RoleMatcher("solicitor")
    assert sol.match("Trainee solicitor at Smith LLP").score == 3          # an employer name isn't a business flag
    assert RoleMatcher("lawyer").match("Barrister, called to the bar 2015").score >= 2
    assert RoleMatcher("conveyancer").match("Licensed conveyancer").score >= 2
    assert RoleMatcher("legal secretary").match("Legal PA at a city firm").score == 2
    mh = RoleMatcher("mental health nurse")
    assert mh.match("RMN on an acute ward").score == 2
    assert mh.match("Community psychiatric nurse").score == 2
    assert 0 < mh.match("Staff nurse").score < 2                            # related, ranked below specialists
    assert RoleMatcher("healthcare assistant").match("HCA on nights").score == 2
    locum = RoleMatcher("locum nurse")
    assert locum.core == "nurse" and locum.contract == "temporary"
    assert RoleMatcher("permanent accountant").contract == "permanent"
    assert contract_type("RGN doing bank shifts") == "temporary" and contract_type("agency nurse") == "temporary"
    assert contract_type("Permanent staff nurse") == "permanent" and contract_type("Staff nurse") == ""


class FakeHttp:
    def __init__(self, json_data=None, html="", robots_ok=True):
        self.json_data, self.html, self.robots_ok, self.calls = json_data, html, robots_ok, []

    def get_json(self, url, params=None, **kw):
        self.calls.append(params["data"])
        return self.json_data

    def get(self, url, check_robots=False, **kw):
        if check_robots and not self.robots_ok:
            raise RobotsDisallowed(url)
        return SimpleNamespace(text=self.html, content=self.html.encode(), url=url, headers={})


def test_openstreetmap_businesses_are_labelled_business():
    assert osm_tags_for(RoleMatcher("electrician")) == [("craft", "electrician")]
    assert ("office", "lawyer") in osm_tags_for(RoleMatcher("solicitor"))
    assert osm_tags_for(RoleMatcher("care worker")) == []
    els = [{"type": "node", "id": 1, "lat": 51.49, "lon": -0.11,
            "tags": {"craft": "electrician", "name": "Sparks Ltd", "email": "info@sparks.example",
                     "phone": "+44 20 7946 0000", "addr:postcode": "SE11 5AA"}},
           {"type": "way", "id": 2, "center": {"lat": 53.8, "lon": -1.55}, "tags": {"craft": "electrician", "name": "Far"}},
           {"type": "node", "id": 3, "lat": 51.5, "lon": -0.1, "tags": {"craft": "electrician"}}]  # no name
    http = FakeHttp({"elements": els})
    p = OpenStreetMapProvider(http=http, fetch_websites=False)
    out = list(p.search("electrician", LONDON, 10, None, None, [], False, FakeGeo()))
    assert [c.name for c in out] == ["Sparks Ltd"]
    c = out[0]
    assert c.kind == "business" and c.provider == "openstreetmap" and "not a job seeker" in c.headline
    assert c.email == "info@sparks.example" and "OpenStreetMap" in c.email_source and "ODbL" in c.email_source
    assert c.profile_url == "https://www.openstreetmap.org/node/1"
    assert 'nwr["craft"="electrician"](around:16093' in http.calls[0]
    p2 = OpenStreetMapProvider(http=FakeHttp({"elements": []}), fetch_websites=False)
    assert list(p2.search("care worker", LONDON, 10, None, None, [], False, FakeGeo())) == []
    assert "No OpenStreetMap business category" in p2.notes[0]


TEAM_HTML = """<html><head><script type="application/ld+json">
{"@context":"https://schema.org","@graph":[
 {"@type":"Person","name":"Asha Patel","jobTitle":"Associate Solicitor","email":"mailto:asha@firm.example",
  "telephone":"020 7946 0001","worksFor":{"@type":"Organization","name":"Firm LLP"}},
 {"@type":"Person","name":"Tom Reed","jobTitle":"Office Manager"}]}
</script></head><body></body></html>"""


def test_team_pages_keep_matching_job_titles():
    p = TeamPageProvider(["https://firm.example/our-people"], http=FakeHttp(html=TEAM_HTML))
    out = list(p.search("solicitor", LONDON, 10, None, None, [], False, FakeGeo()))
    assert [c.name for c in out] == ["Asha Patel"]
    assert out[0].email == "asha@firm.example" and "published by their employer" in out[0].email_source
    assert out[0].company == "Firm LLP" and out[0].provider == "team page (firm.example)"
    blocked = TeamPageProvider(["https://firm.example/our-people"], http=FakeHttp(html=TEAM_HTML, robots_ok=False))
    assert list(blocked.search("solicitor", LONDON, 10, None, None, [], False, FakeGeo())) == []
    assert "robots.txt" in blocked.notes[0]


def test_bluesky_starter_pack_members():
    member = SimpleNamespace(did="d9", handle="rmn.bsky.social", display_name="RMN", labels=[],
                             description="Mental health nurse (RMN) in Lambeth, bank shifts")
    pack = SimpleNamespace(uri="at://pack/1", record=SimpleNamespace(name="UK Nurses", description="NHS nurses"))
    graph = SimpleNamespace(
        search_starter_packs=lambda p: SimpleNamespace(starter_packs=[pack]),
        get_starter_pack=lambda p: SimpleNamespace(starter_pack=SimpleNamespace(list=SimpleNamespace(uri="at://list/1"))),
        get_list=lambda p: SimpleNamespace(items=[SimpleNamespace(subject=member)], cursor=None))
    actor = SimpleNamespace(search_actors=lambda p: SimpleNamespace(actors=[], cursor=None))
    client = SimpleNamespace(app=SimpleNamespace(bsky=SimpleNamespace(actor=actor, graph=graph)))
    p = BlueskyProvider(fetch_websites=False, client=client)
    out = list(p.search("mental health nurse", LONDON, 10, None, None, [], False, FakeGeo()))
    assert [c.profile_url for c in out] == ["https://bsky.app/profile/rmn.bsky.social"]
    assert out[0].contract == "temporary" and p.stats["starter_packs"] == 1


def test_engine_contract_filter_and_business_rows_kept_apart(monkeypatch):
    monkeypatch.setattr(engine, "lookup_postcode", lambda http, pc, r: LONDON)
    rows = [Candidate(name="Locum", profile_url="https://x/1", provider="bluesky", match_score=2, contract="temporary"),
            Candidate(name="Perm", profile_url="https://x/2", provider="bluesky", match_score=3, contract="permanent"),
            Candidate(name="Unsaid", profile_url="https://x/3", provider="bluesky", match_score=3),
            Candidate(name="Biz", profile_url="https://osm/1", website="https://x/2", provider="openstreetmap",
                      kind="business", match_score=2)]

    class Fake:
        name = "bluesky"

        def __init__(self, *a, **kw):
            self.notes, self.stats = [], {}

        def search(self, *a, **kw):
            yield from [Candidate(**vars(r)) for r in rows]

    for cls in ("BlueskyProvider", "MastodonProvider", "OpenStreetMapProvider"):
        monkeypatch.setattr(engine, cls, Fake if cls == "BlueskyProvider" else type("N", (), {
            "name": "none", "__init__": lambda self, *a, **k: setattr(self, "notes", []) or setattr(self, "stats", {}),
            "search": lambda self, *a, **k: iter(())}))
    res = engine.find_candidates("locum nurse", "SE1 7PB", providers=["bluesky", "openstreetmap"],
                                 suppression_entries=[])
    names = [c.name for c in res.candidates]
    assert names.index("Locum") < names.index("Unsaid") or names[0] == "Perm"  # locum boosted for 'locum nurse'
    assert "Biz" in names and names[-1] == "Biz"                                 # business separate, after people
    res = engine.find_candidates("nurse", "SE1 7PB", providers=["bluesky"], contract="temporary",
                                 suppression_entries=[])
    assert [c.name for c in res.candidates if c.kind == "candidate"] == ["Locum"]


def test_merge_keeps_business_and_person_apart():
    a = Candidate(name="Jo", profile_url="https://bsky.app/profile/jo", website="https://jo.example", provider="bluesky",
                  match_score=3)
    b = Candidate(name="Jo Ltd", profile_url="https://www.openstreetmap.org/node/1", website="https://jo.example",
                  provider="openstreetmap", kind="business", match_score=2)
    assert len(merge_duplicates([a, b])) == 2


def test_jobs_contract_tagging():
    assert normalise_employment_type("Bank") == "bank" and normalise_employment_type("Locum") == "locum"
    assert contract_bucket("fixed-term") == "temporary" and contract_bucket("permanent") == "permanent"
    assert contract_bucket(None) == ""
    assert detect_employment_type("Locum Staff Nurse") == "locum"
    assert detect_employment_type("Staff Nurse", "This is a permanent position on our ward") == "permanent"
    assert detect_employment_type("Nurse", "Join our bank staff team") == "bank"
    assert detect_employment_type("Nurse", "Great team") is None
    assert split_contract_words(["locum nurse"]) == (["nurse"], "temporary")
    assert split_contract_words(["permanent solicitor", "lawyer"]) == (["solicitor", "lawyer"], "permanent")


NHS_XML = b"""<?xml version='1.0' encoding='UTF-8'?><nhsJobs>
<vacancyDetails><id>1</id><title>Bank Staff Nurse</title><description>Join our bank.</description>
<employer>Leeds Teaching Hospitals</employer><type>Bank</type><salary>\xc2\xa331000.00 to \xc2\xa337000.00</salary>
<postDate>2026-11-01T10:00:00.1</postDate><url>https://www.jobs.nhs.uk/candidate/jobadvert/A1</url>
<locations><location>Leeds, LS1 3EX</location></locations></vacancyDetails>
<vacancyDetails><id>2</id><title>Practice Nurse</title><description>GP surgery</description><employer>Surgery</employer>
<type>Permanent</type><salary></salary><postDate>2026-11-02T10:00:00.1</postDate>
<url>https://www.jobs.nhs.uk/candidate/jobadvert/A2</url><locations><location>Leeds, LS6 1AA</location></locations>
</vacancyDetails><totalPages>1</totalPages><totalResults>2</totalResults></nhsJobs>"""


def test_nhs_jobs_feed(monkeypatch):
    from recruit_scraper import core
    from recruit_scraper.sources.nhs import NHSJobsSource

    class Http:
        def __init__(self, *a, **kw):
            self.params = []

        def get(self, url, params=None, **kw):
            self.params.append(dict(params))
            return SimpleNamespace(content=NHS_XML)

    src = NHSJobsSource(Http(), keyword="nurse", location="Leeds", limit=10)
    jobs = list(src.fetch())
    assert [j.employment_type for j in jobs] == ["bank", "permanent"]
    assert jobs[0].company == "Leeds Teaching Hospitals" and jobs[0].salary_min == 31000
    assert src.http.params[0]["keyword"] == "nurse" and src.http.params[0]["distance"] == 15
    monkeypatch.setattr(core, "HttpClient", Http)
    res = core.search(["nhsjobs"], keywords=["locum nurse"], locations=["Leeds"], max_results=10, delay=0)
    assert [j.title for j in res.jobs] == ["Bank Staff Nurse"]   # 'locum' -> temporary contracts only
    res = core.search(["nhsjobs"], keywords=["nurse"], locations=["Manchester"], max_results=10, delay=0,
                      contract="permanent")
    assert [j.title for j in res.jobs] == ["Practice Nurse"]      # feed already filtered by distance
