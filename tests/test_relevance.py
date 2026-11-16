"""Relevance: people only appear when their own profile text matches the job; extra sources; cross-source merge."""
from types import SimpleNamespace

import pytest

from recruit_scraper.candidates import engine
from recruit_scraper.candidates.engine import merge_duplicates
from recruit_scraper.candidates.geo import Place
from recruit_scraper.candidates.matching import RoleMatcher, is_tech_role, years_from_text
from recruit_scraper.candidates.models import Candidate
from recruit_scraper.candidates.providers import GitHubProvider
from recruit_scraper.candidates.social import BlueskyProvider, MastodonProvider

LONDON = Place("SE1 7PB", 51.50, -0.12, "Lambeth", "London", "England", ["Lambeth", "Southwark", "Westminster"])
COORDS = {"lambeth": (51.49, -0.11), "southwark": (51.50, -0.09), "london": (51.50, -0.12),
          "westminster": (51.50, -0.13), "leeds": (53.80, -1.55)}


class FakeGeo:
    def geocode(self, text):
        return COORDS.get((text or "").split(",")[0].strip().lower())


@pytest.fixture(autouse=True)
def _cache(tmp_path, monkeypatch):
    from recruit_scraper.candidates import cache as cache_mod
    monkeypatch.setattr(cache_mod, "CACHE_ROOT", tmp_path)
    monkeypatch.setattr(cache_mod.DiskCache.__init__, "__defaults__", (tmp_path,))
    import recruit_scraper.candidates.social as social
    monkeypatch.setattr(social.time, "sleep", lambda s: None)


def test_matcher_requires_the_job_or_a_synonym():
    m = RoleMatcher("Senior Electrician")
    assert m.match("Qualified electrician in Lambeth").score == 3
    assert m.match("NICEIC approved electrical contractor, Southwark").score >= 2
    assert m.match("Python developer and open-source person").score == 0      # the old bug: generic developers
    assert m.match("Senior engineer").score == 0                               # generic words never match
    assert m.match("Retired electrician, London").score == 0                   # not a current match
    dev = RoleMatcher("python developer")
    assert dev.match("Occupational therapist, and in a previous life, a frontend developer").score == 0
    assert dev.match("Ex-teacher, now a developer at Acme").score >= 1.5
    assert dev.match("", "", "Jo", "https://jo.dev").score == 0                 # a .dev domain isn't a job
    care = RoleMatcher("care worker")
    assert care.match("Care assistant living in Manchester UK").score == 2
    assert care.match("I'm a support worker").score == 2
    assert RoleMatcher("accountant", ["tax"]).match("Chartered accountant").score == 0  # extra keyword missing
    assert "accountant" in RoleMatcher("accountant").search_terms()[0]
    assert not is_tech_role("care worker") and not is_tech_role("accountant") and is_tech_role("software engineer")
    assert years_from_text("Sparky with 12 years experience") == 12 and years_from_text("12 cats") is None


def test_github_skipped_for_non_tech_jobs():
    gh = GitHubProvider(http=None, site_http=object())
    assert gh.build_queries("care worker", LONDON, None, None) == []
    assert "isn't a software" in gh.notes[0]
    assert list(gh.search("electrician", LONDON, 10, None, None, [], False, FakeGeo())) == []


def test_github_no_language_title_keeps_only_bio_matches():
    gh = GitHubProvider(http=None, site_http=object(), fetch_websites=False)
    gh._collect_logins = lambda q, p: {"a": "Lambeth", "b": "Lambeth"}
    users = {"a": {"login": "a", "bio": "UX designer at a studio", "location": "Lambeth", "type": "User",
                   "created_at": "2015-01-01T00:00:00Z"},
             "b": {"login": "b", "bio": "I write Rust", "location": "Lambeth", "type": "User",
                   "created_at": "2015-01-01T00:00:00Z"}}
    gh.user_cache = SimpleNamespace(get=users.get, set=lambda *a: None)
    out = list(gh.search("UX designer", LONDON, 10, None, None, [], False, FakeGeo()))
    assert [c.name for c in out] == ["a"] and out[0].match_score > 0 and "ux designer" in out[0].match_reason


def _actor(did, handle, desc, labels=()):
    return SimpleNamespace(did=did, handle=handle, display_name=handle.split(".")[0], description=desc,
                           labels=[SimpleNamespace(val=v) for v in labels])


class FakeBsky:
    def __init__(self, actors):
        self.calls = []
        self.app = SimpleNamespace(bsky=SimpleNamespace(actor=SimpleNamespace(search_actors=self._search)))
        self.actors = actors

    def _search(self, params):
        self.calls.append(params["q"])
        return SimpleNamespace(actors=self.actors, cursor=None)


def test_bluesky_keeps_only_job_and_area_matches():
    client = FakeBsky([
        _actor("d1", "sparky.bsky.social", "Electrician based in Southwark. https://sparky.example"),
        _actor("d2", "dev.bsky.social", "Python developer in Lambeth"),                   # wrong job
        _actor("d3", "leeds.bsky.social", "Electrician in Leeds"),                         # too far
        _actor("d4", "hidden.bsky.social", "Electrician, Lambeth", ["!no-unauthenticated"]),  # opted out
        _actor("d5", "nowhere.bsky.social", "Electrician"),                                # no place
    ])
    p = BlueskyProvider(fetch_websites=False, client=client)
    out = list(p.search("electrician", LONDON, 10, None, None, [], False, FakeGeo()))
    assert [c.profile_url for c in out] == ["https://bsky.app/profile/sparky.bsky.social"]
    c = out[0]
    assert c.provider == "bluesky" and c.website == "https://sparky.example" and c.distance_miles is not None
    assert c.email == "" and "none in their Bluesky bio" in c.email_source
    assert any("electrician" in q for q in client.calls)


def test_mastodon_only_discoverable_people():
    acct = lambda **kw: {"acct": kw.get("acct", "x"), "url": f"https://m.example/@{kw.get('acct', 'x')}",
                         "display_name": kw.get("acct", "x"), "note": kw.get("note", ""),
                         "discoverable": kw.get("disc", True), "bot": kw.get("bot", False), "noindex": False,
                         "fields": kw.get("fields", [])}

    class Api:
        def search_v2(self, q, **kw):
            assert kw["resolve"] is False
            return {"accounts": [
                acct(acct="ok", note="<p>Chartered accountant</p>", fields=[{"name": "Location", "value": "Westminster"}]),
                acct(acct="hidden", note="<p>Accountant in Lambeth</p>", disc=False),
                acct(acct="bot", note="<p>Accountant bot, Lambeth</p>", bot=True)]}

    p = MastodonProvider(fetch_websites=False, servers=("m.example",), factory=lambda s: Api())
    out = list(p.search("accountant", LONDON, 10, None, None, [], False, FakeGeo()))
    assert [c.name for c in out] == ["ok"] and out[0].location == "Westminster" and out[0].provider == "mastodon"


def test_merge_duplicates_across_sources():
    a = Candidate(name="Jo", profile_url="https://bsky.app/profile/jo.dev", website="https://jo.dev", provider="bluesky",
                  match_score=3)
    b = Candidate(name="Jo", profile_url="https://m.example/@jo", website="https://jo.dev/", provider="mastodon",
                  match_score=2, email="jo@jo.dev", email_source="self-published on personal website https://jo.dev")
    c = Candidate(name="Sam", profile_url="https://github.com/sam", provider="github", match_score=1,
                  other_profiles="https://bsky.app/profile/sam.example")
    d = Candidate(name="Sam", profile_url="https://bsky.app/profile/sam.example", provider="bluesky", match_score=3)
    out = merge_duplicates([a, b, c, d])
    assert len(out) == 2
    jo = next(x for x in out if x.name == "Jo")
    assert jo.provider == "bluesky + mastodon" and jo.email == "jo@jo.dev" and "m.example/@jo" in jo.other_profiles
    sam = next(x for x in out if x.name == "Sam")
    assert sam.provider == "bluesky + github" and "github.com/sam" in sam.other_profiles


def test_find_candidates_drops_unmatched_and_explains_coverage(monkeypatch):
    monkeypatch.setattr(engine, "lookup_postcode", lambda http, pc, r: LONDON)

    class Fake:
        def __init__(self, *a, **kw):
            self.notes, self.stats = [], {}

        def search(self, *a, **kw):
            yield Candidate(name="Match", profile_url=f"https://{self.name}/m", provider=self.name, match_score=3)
            yield Candidate(name="NoMatch", profile_url=f"https://{self.name}/n", provider=self.name, match_score=0)

    monkeypatch.setattr(engine, "BlueskyProvider", type("B", (Fake,), {"name": "bluesky"}))
    monkeypatch.setattr(engine, "MastodonProvider", type("M", (Fake,), {"name": "mastodon"}))
    res = engine.find_candidates("care worker", "SE1 7PB", providers=["github", "bluesky", "mastodon"],
                                 suppression_entries=[])
    assert sorted(c.provider for c in res.candidates) == ["bluesky", "mastodon"]
    assert all(c.name == "Match" for c in res.candidates)
    assert not res.tech_role and "Jobs page" in res.coverage
    gh = next(r for r in res.reports if r.name == "github")
    assert gh.status == "skipped" and gh.found == 0
