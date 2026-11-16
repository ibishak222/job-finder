"""More discovery (Bluesky profession feeds, Mastodon hashtags + UK servers, cross-site handles, own-domain
handles), better contact quality (validated phones), fuzzy cross-source dedupe and the 'best match' score."""
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from recruit_scraper.candidates import engine
from recruit_scraper.candidates.contacts import clean_phones, parse_page
from recruit_scraper.candidates.engine import merge_similar, rank_key, score_fit
from recruit_scraper.candidates.geo import Place
from recruit_scraper.candidates.matching import RoleMatcher
from recruit_scraper.candidates.models import Candidate
from recruit_scraper.candidates.social import (BlueskyProvider, MastodonProvider, _acct_dict, cross_links,
                                               handle_site, uk_servers)

LONDON = Place("SE1 7PB", 51.50, -0.12, "Lambeth", "London", "England", ["Lambeth", "Southwark", "Westminster"])


class FakeGeo:
    def geocode(self, text):
        return {"lambeth": (51.49, -0.11), "southwark": (51.50, -0.09), "london": (51.50, -0.12),
                "leeds": (53.80, -1.55)}.get((text or "").split(",")[0].strip().lower())


@pytest.fixture(autouse=True)
def _cache(tmp_path, monkeypatch):
    from recruit_scraper.candidates import cache as cache_mod
    monkeypatch.setattr(cache_mod, "CACHE_ROOT", tmp_path)
    monkeypatch.setattr(cache_mod.DiskCache.__init__, "__defaults__", (tmp_path,))
    import recruit_scraper.candidates.social as social
    monkeypatch.setattr(social.time, "sleep", lambda s: None)


def test_cross_links_and_own_domain_handles():
    assert cross_links("Also @jo@mastodon.social and jo.bsky.social, mail me@x.com") == [
        "https://mastodon.social/@jo", "https://bsky.app/profile/jo.bsky.social"]
    assert handle_site("janesmith.co.uk") == "https://janesmith.co.uk"
    assert handle_site("jo.dev") == "https://jo.dev"
    assert handle_site("jo.bsky.social") == "" and handle_site("jo.blog.example.com") == ""


def test_search_terms_skip_ambiguous_acronyms_and_add_nearby_areas():
    assert "rnld" not in RoleMatcher("locum nurse").search_terms(6)
    qs = BlueskyProvider().queries(RoleMatcher("electrician"), LONDON)
    assert "electrician Lambeth" in qs and "electrician Southwark" in qs and "electrician" in qs
    assert len(qs) <= BlueskyProvider.max_queries


def test_bluesky_profession_feed_authors_need_own_bio_match():
    nurse = SimpleNamespace(did="d1", handle="annie.co.uk", display_name="Annie", labels=[],
                            description="Staff nurse in Southwark, NHS")
    other = SimpleNamespace(did="d2", handle="news.bsky.social", display_name="News", labels=[],
                            description="Health news from Lambeth")
    post = lambda did: SimpleNamespace(post=SimpleNamespace(author=SimpleNamespace(did=did),
                                                            indexed_at="2026-11-01T10:00:00Z"))
    fg = SimpleNamespace(uri="at://feed/1", display_name="NurseSky", description="Posts from nurses")
    client = SimpleNamespace(app=SimpleNamespace(bsky=SimpleNamespace(
        actor=SimpleNamespace(search_actors=lambda p: SimpleNamespace(actors=[], cursor=None),
                              get_profiles=lambda p: SimpleNamespace(profiles=[nurse, other])),
        graph=SimpleNamespace(search_starter_packs=lambda p: SimpleNamespace(starter_packs=[])),
        unspecced=SimpleNamespace(get_popular_feed_generators=lambda p: SimpleNamespace(feeds=[fg])),
        feed=SimpleNamespace(get_feed=lambda p: SimpleNamespace(feed=[post("d1"), post("d2")], cursor=None)))))
    p = BlueskyProvider(fetch_websites=False, client=client)
    out = list(p.search("nurse", LONDON, 10, None, None, [], False, FakeGeo()))
    assert [c.name for c in out] == ["Annie"]
    c = out[0]
    assert c.website == "https://annie.co.uk" and c.last_active == "2026-11-01"
    assert "profession feed" in c.match_reason and p.stats["profession_feeds"] == 1


def test_mastodon_hashtag_authors_and_opt_in_rules():
    def acct(name, note, **kw):
        return {"acct": name, "url": f"https://m.example/@{name}", "display_name": name, "note": note,
                "discoverable": kw.get("disc", True), "bot": False, "noindex": kw.get("noindex", False),
                "fields": [{"name": "Location", "value": "Lambeth"}], "last_status_at": "2026-11-09"}

    class Api:
        def search_v2(self, q, **kw):
            return {"accounts": []}

        def timeline_hashtag(self, tag, **kw):
            assert tag == "electrician"
            return [{"id": "1", "account": acct("sparks", "<p>Electrician. Also @sparks@bsky.brid.gy</p>")},
                    {"id": "2", "account": acct("private", "<p>Electrician</p>", disc=False)},
                    {"id": "3", "account": acct("hidden", "<p>Electrician</p>", noindex=True)}]

    p = MastodonProvider(fetch_websites=False, servers=("m.example",), factory=lambda s: Api())
    p.HASHTAG_SERVERS = ("m.example",)
    out = list(p.search("electrician", LONDON, 10, None, None, [], False, FakeGeo()))
    assert [c.name for c in out] == ["sparks"]
    assert out[0].last_active == "2026-11-09" and "#electrician" in out[0].match_reason
    assert _acct_dict({"acct": "x", "discoverable": True, "bot": True}, "s") is None


def test_uk_servers_from_joinmastodon_directory(tmp_path):
    from recruit_scraper.candidates.cache import DiskCache
    rows = [{"domain": "mcr.wtf", "region": "europe", "total_users": 99, "description": "People in Manchester"},
            {"domain": "mstdn.dk", "region": "europe", "total_users": 400, "description": "Danish server"},
            {"domain": "mastodon.ie", "region": "europe", "total_users": 900, "description": "Irish Mastodon"},
            {"domain": "toot.wales", "region": "europe", "total_users": 500, "description": "Wales and the Welsh"}]
    http = SimpleNamespace(get=lambda url, params=None: SimpleNamespace(json=lambda: rows))
    assert uk_servers(http, DiskCache("t", 60, root=tmp_path)) == ["toot.wales", "mcr.wtf"]


def test_phone_numbers_validated_and_formatted():
    assert clean_phones(["0161 496 0000", "123"], "Call 07911 123456 - order ref 2026-11-10 99812345678") == [
        "+44 161 496 0000", "+44 7911 123456"]
    got = parse_page("<p>Mobile: 07911 123456</p>", "https://jo.example/")
    assert got["phones"] == ["+44 7911 123456"]


def test_merge_similar_same_name_same_area_only():
    a = Candidate(name="Dr Priya Shah (she/her)", provider="bluesky", profile_url="https://bsky.app/profile/p",
                  distance_miles=2.0, match_score=3)
    b = Candidate(name="Priya Shah", provider="mastodon", profile_url="https://m.example/@priya", distance_miles=3.1,
                  email="p@shah.example", match_score=2)
    c = Candidate(name="Priya Shah", provider="mastodon", profile_url="https://m.example/@p2", distance_miles=40)
    d = Candidate(name="Priya Shah", provider="bluesky", profile_url="https://bsky.app/profile/p3", distance_miles=2)
    out = merge_similar([a, b, c, d])
    assert len(out) == 3   # a+b merged; c is in another town; d is a second account on the same site as a
    assert out[0].provider == "bluesky + mastodon" and out[0].email == "p@shah.example"
    assert "https://m.example/@priya" in out[0].other_profiles
    # single names are never fuzzy-merged
    assert len(merge_similar([Candidate(name="Sam", provider="bluesky", distance_miles=1),
                              Candidate(name="Sam", provider="mastodon", distance_miles=1)])) == 2


def test_best_match_score_and_ranking():
    recent = (date.today() - timedelta(days=10)).isoformat()
    near = score_fit(Candidate(name="A", match_score=3, distance_miles=1, email="a@x.example", last_active=recent,
                               experience_years=5), 15, 3, None)
    far = score_fit(Candidate(name="B", match_score=3, distance_miles=14), 15, 3, None)
    weak = score_fit(Candidate(name="C", match_score=1.5, distance_miles=1, email="c@x.example"), 15)
    biz = score_fit(Candidate(name="Firm", kind="business", match_score=3, distance_miles=0.5, email="f@x.example",
                              phone="+44 161 496 0000"), 15)
    assert near.fit > far.fit and near.fit > weak.fit and 0 <= far.fit <= 100
    assert "has email" in near.fit_reason and "active recently" in near.fit_reason
    assert "job title in profile" in near.fit_reason
    ordered = sorted([biz, weak, far, near], key=rank_key)
    assert ordered[0].name == "A" and ordered[-1].name == "Firm"   # people before businesses
    temp = score_fit(Candidate(name="L", match_score=3, contract="temporary"), 15, want="temporary")
    assert "locum/temp" in temp.fit_reason


def test_engine_scores_and_sorts(monkeypatch):
    from recruit_scraper.candidates import engine as eng
    monkeypatch.setattr(eng, "lookup_postcode", lambda http, pc, r: LONDON)
    monkeypatch.setattr(eng, "PlaceGeocoder", lambda http: FakeGeo())

    class Prov:
        notes, stats = [], {}

        def __init__(self, name):
            self.name = name

        def search(self, *a, **kw):
            if self.name == "bluesky":
                yield Candidate(name="Jo Bloggs", provider="bluesky", profile_url="https://bsky.app/profile/jo",
                                distance_miles=2, match_score=3)
                yield Candidate(name="Far Away", provider="bluesky", profile_url="https://bsky.app/profile/f",
                                distance_miles=12, match_score=2)
            else:
                yield Candidate(name="Jo Bloggs", provider="mastodon", profile_url="https://m.example/@jo",
                                distance_miles=2.5, match_score=3, email="jo@bloggs.example")

    monkeypatch.setattr(eng, "BlueskyProvider", lambda **kw: Prov("bluesky"))
    monkeypatch.setattr(eng, "MastodonProvider", lambda **kw: Prov("mastodon"))
    res = eng.find_candidates("electrician", "SE1 7PB", 10, providers=["bluesky", "mastodon"], suppression_entries=[])
    assert [c.name for c in res.candidates] == ["Jo Bloggs", "Far Away"]
    top = res.candidates[0]
    assert top.provider == "bluesky + mastodon" and top.email and top.fit > res.candidates[1].fit


def test_uk_people_without_a_nearby_town_are_optional_and_labelled():
    class Api:
        def search_v2(self, q, **kw):
            mk = lambda name, note, srv="m.example": {
                "acct": f"{name}@{srv}", "url": f"https://{srv}/@{name}", "display_name": name, "note": note,
                "discoverable": True, "bot": False, "noindex": False, "fields": []}
            return {"accounts": [mk("nhs", "<p>Staff nurse, NHS</p>"),               # UK signal, no town
                                 mk("us", "<p>Nurse in Ohio, USA</p>"),               # not UK
                                 mk("mcr", "<p>Nurse</p>", "mcr.wtf"),                # Manchester's own server
                                 mk("plain", "<p>Nurse</p>")]}                        # no place, no UK signal
    geo = SimpleNamespace(geocode=lambda t: {"manchester": (53.48, -2.24)}.get((t or "").lower()))
    mcr = Place("M1 1AE", 53.48, -2.23, "Manchester", "North West", "England", ["Salford"])
    names = lambda prov: {c.name: c for c in prov.search("nurse", mcr, 10, None, None, [], False, geo)}
    off = names(MastodonProvider(fetch_websites=False, servers=("m.example",), factory=lambda s: Api(), hashtags=False))
    assert set(off) == {"mcr"} and off["mcr"].location == "Manchester" and off["mcr"].distance_miles is not None
    on = names(MastodonProvider(fetch_websites=False, servers=("m.example",), factory=lambda s: Api(), hashtags=False,
                                uk_unknown=True))
    assert set(on) == {"mcr", "nhs"} and on["nhs"].location == "UK - no nearby town mentioned"
    assert on["nhs"].distance_miles is None
    assert score_fit(on["mcr"], 10).fit > score_fit(on["nhs"], 10).fit


def test_far_uk_towns_and_business_accounts_are_handled():
    from recruit_scraper.candidates.geo import far_uk_place
    from recruit_scraper.candidates.matching import looks_like_business
    geo = SimpleNamespace(geocode=lambda t: {"cardiff": (51.48, -3.18), "salford": (53.49, -2.29),
                                             "preston": (53.76, -2.70)}.get((t or "").lower()))
    mcr = Place("M1 1AE", 53.48, -2.23, "Manchester", "North West", "England", ["Salford"])
    assert far_uk_place("Care worker in Ely, Cardiff, Cymru", mcr, 15, geo) in ("Cardiff", "Wales")
    assert far_uk_place("Preston-based accountant", mcr, 15, geo) == "Preston"
    assert far_uk_place("NHS nurse, Salford born", mcr, 15, geo) is None
    assert far_uk_place("Nurse. Love Yorkshire tea", mcr, 15, geo) is None      # counties are not judged
    assert far_uk_place("Health care worker, New England raised", mcr, 15, geo) is None
    assert looks_like_business("Beckenham Electricians", "Beckenham Electricians provides electrical services")
    assert looks_like_business("Sparks", "We provide domestic rewires. Call us for a free quote")
    assert not looks_like_business("Kat", "Proud registered nurse for 35 years in our amazing NHS")
    assert not looks_like_business("Miss Jones - Primary School Teacher", "I teach Year 4")


def test_business_bio_accounts_become_business_rows():
    class Api:
        def search_v2(self, q, **kw):
            mk = lambda name, note: {"acct": name, "url": f"https://m.example/@{name}", "display_name": name,
                                     "note": note, "discoverable": True, "bot": False, "noindex": False,
                                     "fields": [{"name": "Location", "value": "Lambeth"}]}
            return {"accounts": [mk("Lambeth Electricians Ltd", "<p>Electrician services, free quote</p>"),
                                 mk("Sam", "<p>I'm an electrician in Lambeth</p>")]}
    p = MastodonProvider(fetch_websites=False, servers=("m.example",), factory=lambda s: Api(), hashtags=False)
    kinds = {c.name: c.kind for c in p.search("electrician", LONDON, 10, None, None, [], False, FakeGeo())}
    assert kinds == {"Lambeth Electricians Ltd": "business", "Sam": "candidate"}


def test_uk_no_town_quality_rules():
    from recruit_scraper.candidates.matching import RoleMatcher as RM
    assert RM("care worker").match("Former ALS/MND carer. Researcher").score == 0

    class Api:
        def search_v2(self, q, **kw):
            mk = lambda name, note: {"acct": name.split()[0].lower(), "url": f"https://m.example/@{name.split()[0]}",
                                     "display_name": name, "note": note, "discoverable": True, "bot": False,
                                     "noindex": False, "fields": []}
            return {"accounts": [mk("Peter Cook", "<p>Campaigner for Britain</p>"),          # surname, not a job
                                 mk("Sam | Cook", "<p>Proud to work in the UK</p>"),           # title in name
                                 mk("Flex", "<p>Chef. Immigrant in Andalucía 🇪🇸 since 2012, UK born</p>"),
                                 mk("Uni", "<p>Chef. Project page http://x.ac.uk</p>")]}      # .uk only in a URL
    geo = SimpleNamespace(geocode=lambda t: None)
    p = MastodonProvider(fetch_websites=False, servers=("m.example",), factory=lambda s: Api(), hashtags=False,
                         uk_unknown=True)
    assert [c.name for c in p.search("chef", LONDON, 10, None, None, [], False, geo)] == ["Sam | Cook"]
