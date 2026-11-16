from datetime import datetime, timedelta, timezone

import pytest

from recruit_scraper.candidates import suppression
from recruit_scraper.candidates.engine import filter_experience, purge_old_exports
from recruit_scraper.candidates.geo import (PlaceGeocoder, PostcodeError, haversine_miles, looks_non_uk,
                                            lookup_postcode)
from recruit_scraper.candidates.models import Candidate
from recruit_scraper.candidates.providers import GitHubProvider, languages_for


class FakeHttp:
    """Stands in for HttpClient; maps URL substrings to JSON responses."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get_json(self, url, params=None, headers=None, **kw):
        self.calls.append((url, params))
        for frag, data in self.routes.items():
            if frag in url:
                return data(params) if callable(data) else data
        import requests
        resp = requests.Response()
        resp.status_code = 404
        raise requests.HTTPError(response=resp)


PC = {"result": {"postcode": "M1 1AE", "latitude": 53.4835, "longitude": -2.2312, "admin_district": "Manchester",
                 "region": "North West", "country": "England"}}
OUT = {"result": [{"admin_district": ["Manchester"]}, {"admin_district": ["Salford", "Manchester"]},
                  {"admin_district": ["Stockport"]}, {"admin_district": ["Salford"]}]}


def test_lookup_postcode_mocked():
    http = FakeHttp({"/postcodes/M11AE": PC, "/outcodes": OUT})
    place = lookup_postcode(http, " m1 1ae ", radius_miles=20)
    assert (place.postcode, place.town, place.region) == ("M1 1AE", "Manchester", "North West")
    assert place.lat == pytest.approx(53.4835)
    assert place.nearby_towns == ["Manchester", "Salford", "Stockport"]
    radius = [p for u, p in http.calls if "/outcodes" in u][0]["radius"]
    assert radius == 25000  # 20 miles is capped at postcodes.io's 25 km maximum


def test_lookup_postcode_errors():
    with pytest.raises(PostcodeError):
        lookup_postcode(FakeHttp({}), "not a postcode")
    with pytest.raises(PostcodeError):
        lookup_postcode(FakeHttp({}), "ZZ1 1ZZ")  # 404 -> friendly error


def test_geocoder_and_non_uk():
    http = FakeHttp({"/places": {"result": [{"name_1": "Stockport", "local_type": "Town",
                                              "latitude": 53.408, "longitude": -2.159}]}})
    g = PlaceGeocoder(http)
    assert g.geocode("Stockport, UK") == (53.408, -2.159)
    assert g.geocode("Manchester, NH") is None
    assert looks_non_uk("Manchester, USA") and not looks_non_uk("Manchester, UK")
    assert 5.5 < haversine_miles(53.4835, -2.2312, 53.408, -2.159) < 6.5  # Manchester -> Stockport ~6 miles


def test_filter_experience():
    cs = [Candidate(name="a", experience_years=2), Candidate(name="b", experience_years=5),
          Candidate(name="c", experience_years=12), Candidate(name="d", experience_years=None)]
    assert [c.name for c in filter_experience(cs, 3, 10)] == ["b", "d"]
    assert [c.name for c in filter_experience(cs, 3, None, keep_unknown=False)] == ["b", "c"]
    assert [c.name for c in filter_experience(cs, None, 4)] == ["a", "d"]


def test_github_query_uses_experience_and_language():
    from recruit_scraper.candidates.geo import Place
    gh = GitHubProvider(http=None, site_http=object())
    place = Place("M1 1AE", 53.48, -2.23, "Manchester", "North West", "England",
                  ["Manchester", "Cheshire East"])
    qs = [q for _, q in gh.build_queries("Senior Python developer", place, 3, 10)]
    today = datetime.now(timezone.utc).date()
    assert qs[0].startswith("location:Manchester language:Python created:<=")
    assert qs[1].startswith('location:Manchester language:"Jupyter Notebook"')  # related skill variant
    assert f"created:<={today - timedelta(days=int(3 * 365.25))}" in qs[0]
    assert f"created:>={today - timedelta(days=int(10 * 365.25))}" in qs[0]
    assert any(q.startswith('location:"Greater Manchester"') for q in qs)      # area synonym
    assert any(q.startswith('location:"Cheshire East"') for q in qs)
    assert languages_for("React front-end engineer") == ["JavaScript", "TypeScript"]


def test_suppression_list(tmp_path):
    path = tmp_path / "suppression.txt"
    suppression.save(["Jane@Example.com", "@optout.co.uk", "https://github.com/someuser", "  "], path)
    entries = suppression.load(path)
    assert "jane@example.com" in entries and "@optout.co.uk" in entries
    cs = [Candidate(email="jane@example.com"), Candidate(email="bob@optout.co.uk"),
          Candidate(profile_url="https://github.com/SomeUser"), Candidate(email="ok@fine.com"),
          Candidate(profile_url="https://github.com/other")]
    kept, removed = suppression.apply(cs, entries)
    assert removed == 3
    assert [c.email or c.profile_url for c in kept] == ["ok@fine.com", "https://github.com/other"]
    assert suppression.parse("# comment\nfoo@bar.com\n") == ["foo@bar.com"]


def test_purge_old_exports(tmp_path):
    import os
    import time
    old, new = tmp_path / "old.csv", tmp_path / "new.csv"
    old.write_text("x")
    new.write_text("y")
    os.utime(old, (time.time() - 40 * 86400,) * 2)
    assert purge_old_exports(30, tmp_path) == 1
    assert not old.exists() and new.exists()


def test_github_breadth_first_pagination(tmp_path, monkeypatch):
    from recruit_scraper.candidates import cache as cache_mod
    monkeypatch.setattr(cache_mod, "CACHE_ROOT", tmp_path)
    gh = GitHubProvider(http=None, site_http=object(), max_results=10_000)
    gh.user_cache = cache_mod.DiskCache("u", 60, root=tmp_path)
    gh.search_cache = cache_mod.DiskCache("s", 60, root=tmp_path)
    gh.search_interval = 0
    calls = []

    def fake_get(url, params=None, kind="core", progress=None):
        calls.append((params["q"], params["page"]))
        total = 250 if "A" in params["q"] else 50
        start = (params["page"] - 1) * 100
        n = max(0, min(100, total - start))
        return {"total_count": total, "items": [{"login": f"{params['q'][:1]}{start + i}"} for i in range(n)]}

    gh._get = fake_get
    logins = gh._collect_logins([("A", "A q"), ("B", "B q")], lambda m: None)
    assert len(logins) == 300
    assert calls == [("A q", 1), ("B q", 1), ("A q", 2), ("A q", 3)]  # page 1 of every variant first
    gh._collect_logins([("A", "A q"), ("B", "B q")], lambda m: None)
    assert len(calls) == 4  # second run fully served from the disk cache


def test_personal_site_parsing_rules():
    from recruit_scraper.candidates.contacts import is_no_fetch, parse_page
    html = """<html><head><script type="application/ld+json">
      {"@type":"Person","name":"A","email":"mailto:jo@jo.dev","telephone":"+44 7700 900123",
       "sameAs":["https://www.linkedin.com/in/jo-example"]}</script></head>
      <body><a href="mailto:Hello%40jo.dev?subject=hi">mail</a> <a href="https://x.com/jo_dev">x</a>
      contact: jo [at] example [dot] org, noreply@jo.dev</body></html>"""
    got = parse_page(html, "https://jo.dev/")
    assert got["emails"] == ["hello@jo.dev", "jo@jo.dev"]       # obfuscated + no-reply not harvested
    assert got["phones"] == ["+44 7700 900123"]
    assert got["linkedin"] == "https://www.linkedin.com/in/jo-example"
    assert got["twitter"] == "https://x.com/jo_dev"
    assert is_no_fetch("https://www.linkedin.com/in/x") and is_no_fetch("https://github.com/x")
    assert not is_no_fetch("https://jo.github.io")


def test_personal_site_respects_noindex_and_robots(tmp_path, monkeypatch):
    import requests
    from recruit_scraper.candidates import cache as cache_mod
    from recruit_scraper.candidates.contacts import SiteContactFinder
    from recruit_scraper.http import RobotsDisallowed
    monkeypatch.setattr(cache_mod, "CACHE_ROOT", tmp_path)

    class Http:
        def get(self, url, check_robots=False, headers=None):
            assert check_robots is True
            if "blocked" in url:
                raise RobotsDisallowed(url)
            r = requests.Response()
            r.status_code, r.url = 200, url
            r.headers["Content-Type"] = "text/html"
            body = ('<meta name="robots" content="noindex"><a href="mailto:a@hidden.dev">x</a>' if "hidden" in url
                    else '<a href="mailto:me@open.dev">x</a>')
            r._content = body.encode()
            return r

    f = SiteContactFinder(Http())
    assert f.personal("open.dev")["emails"] == ["me@open.dev"]
    assert f.personal("https://hidden.dev")["emails"] == []
    assert f.personal("https://blocked.dev")["emails"] == []
    assert f.personal("https://linkedin.com/in/someone")["linkedin"] == "https://linkedin.com/in/someone"


def test_placeholder_emails_ignored():
    from recruit_scraper.utils import find_emails
    txt = "Write to firstname.surname@uni.ac.uk or your.name@x.com; me@realsite.dev; jobs@acme.co.uk"
    assert find_emails(txt) == ["me@realsite.dev", "jobs@acme.co.uk"]
