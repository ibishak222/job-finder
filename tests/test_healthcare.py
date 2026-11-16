"""Europe PMC clinical researchers, Bluesky/Mastodon message links and 'Employers to contact' (offline)."""
from datetime import date
from types import SimpleNamespace

import pytest

from recruit_scraper.candidates import employers as emp
from recruit_scraper.candidates import engine
from recruit_scraper.candidates.geo import Place
from recruit_scraper.candidates.matching import RoleMatcher
from recruit_scraper.candidates.models import Candidate
from recruit_scraper.candidates.research import (API, EuropePMCProvider, area_towns, build_query, is_health_role,
                                                 owns_email)

MAN = Place("M1 1AE", 53.48, -2.23, "Manchester", "North West", "England")
MAN.nearby_towns = ["Manchester", "Salford", "Trafford"]


@pytest.fixture(autouse=True)
def _cache(tmp_path, monkeypatch):
    from recruit_scraper.candidates import cache as cache_mod
    monkeypatch.setattr(cache_mod, "CACHE_ROOT", tmp_path)
    monkeypatch.setattr(cache_mod.DiskCache.__init__, "__defaults__", (tmp_path,))


def author(first, last, aff):
    return {"firstName": first, "lastName": last, "fullName": f"{last} {first[0]}",
            "authorAffiliationDetailsList": {"authorAffiliation": [{"affiliation": aff}]}}


ARTICLE = {"id": "41000001", "source": "MED", "pmid": "41000001", "title": "Practice nurse workload in general practice.",
           "pubYear": "2025", "firstPublicationDate": "2025-03-04", "authorList": {"author": [
               author("Gemma", "Beardmore", "Division of Nursing, University of Manchester, Manchester, UK. "
                                            "gemma.beardmore@nhs.net"),
               author("Lee", "Smith", "University of Manchester, Manchester, UK. g.beardmore@manchester.ac.uk"),
               author("Ann", "Jones", "School of Healthcare, University of Leeds, Leeds, UK. a.jones@leeds.ac.uk"),
               author("Rob", "Taylor", "Salford Royal, Salford, UK. info@srft.nhs.uk"),
               author("Kate", "Brown", "Salford Royal NHS Foundation Trust, Salford, UK. k.brown@nhs.net"),
           ]}}


class FakeHttp:
    def __init__(self, payload):
        self.payload, self.calls = payload, []

    def get_json(self, url, params=None, **kw):
        self.calls.append((url, params))
        return self.payload


def test_health_roles_and_query():
    assert is_health_role("practice nurse") and is_health_role("care worker") and is_health_role("HCA")
    assert not is_health_role("family lawyer") and not is_health_role("electrician")
    q = build_query(RoleMatcher("practice nurse"), ["Manchester", "Salford"], 5, today=date(2026, 11, 10))
    assert 'TITLE_ABS:"practice nurse"' in q and 'TITLE_ABS:"general practice nurse"' in q
    assert 'TITLE_ABS:"nurse"' not in q and 'AFF:"Manchester" OR AFF:"Salford"' in q and "PUB_YEAR:[2021 TO 2026]" in q
    lon = Place("SE1 7PB", 51.5, -0.1, "Southwark", "London", "England")
    lon.nearby_towns = ["Southwark", "Lambeth"]
    assert area_towns(lon)[0] == "London"


def test_email_must_belong_to_that_author():
    assert owns_email("gemma.beardmore@nhs.net", "Gemma", "Beardmore")
    assert owns_email("kbrown2@nhs.net", "Kate", "Brown")
    assert not owns_email("g.beardmore@manchester.ac.uk", "Lee", "Smith")   # a co-author's address
    assert not owns_email("info@srft.nhs.uk", "Rob", "Taylor")
    assert owns_email("j.o-connor@x.ac.uk", "Jo", "O'Connor")


def test_parse_keeps_only_own_local_addresses_with_article_source():
    prov = EuropePMCProvider(http=FakeHttp({"hitCount": 1, "resultList": {"result": [ARTICLE]}}))
    got = list(prov.search("practice nurse", MAN, 10, None, None, [], False, None))
    assert [c.email for c in got] == ["gemma.beardmore@nhs.net", "k.brown@nhs.net"]
    g = got[0]
    url = "https://europepmc.org/article/MED/41000001"
    assert g.name == "Gemma Beardmore" and g.contact_source == url and g.profile_url == url
    assert g.provider == "europepmc" and g.headline.startswith("Clinical researcher (Europe PMC)")
    assert "research correspondence" in g.email_source and "PECR" in g.email_source
    assert g.location == "Manchester" and g.match_score >= 1.5
    assert got[1].location == "Salford"
    assert prov.stats["not_their_email"] == 2 and prov.stats["not_near"] == 1
    assert any("PECR" in n for n in prov.notes)


def test_few_polite_requests_and_cache():
    http = FakeHttp({"hitCount": 1, "resultList": {"result": [ARTICLE]}, "nextCursorMark": "AoE"})
    list(EuropePMCProvider(http=http).search("practice nurse", MAN, 10, None, None, [], False, None))
    assert len(http.calls) == 1 and http.calls[0][0] == API and http.calls[0][1]["resultType"] == "core"
    list(EuropePMCProvider(http=http).search("practice nurse", MAN, 10, None, None, [], False, None))
    assert len(http.calls) == 1                                   # second run came from the cache
    assert EuropePMCProvider().http.min_interval >= 10            # robots.txt crawl delay


def test_non_health_roles_make_no_request():
    http = FakeHttp({})
    prov = EuropePMCProvider(http=http)
    assert list(prov.search("family lawyer", MAN, 10, None, None, [], False, None)) == [] and not http.calls
    assert "skipped" in prov.notes[0]


def test_coauthors_not_merged_and_dedupe_with_bluesky():
    prov = EuropePMCProvider(http=FakeHttp({"hitCount": 1, "resultList": {"result": [ARTICLE]}}))
    rows = list(prov.parse([ARTICLE], RoleMatcher("practice nurse"), ["Manchester", "Salford"], MAN, 10, None))
    for r in rows:
        r.distance_miles = 1.0
    bsky = Candidate(name="Kate Brown RGN", provider="bluesky", profile_url="https://bsky.app/profile/kateb",
                     location="Salford", distance_miles=1.5, match_score=3)
    out = engine.merge_similar(engine.merge_duplicates(rows + [bsky]))
    assert len(out) == 2                                          # two co-authors stay two people
    kate = next(c for c in out if "Brown" in c.name)
    assert "bluesky" in kate.provider and "europepmc" in kate.provider and kate.email == "k.brown@nhs.net"


def test_engine_toggle_suppression_and_note(monkeypatch):
    monkeypatch.setattr(engine, "lookup_postcode", lambda http, pc, r: MAN)
    monkeypatch.setattr(engine, "PlaceGeocoder", lambda http: None)
    made = []

    def fake_provider():
        p = EuropePMCProvider(http=FakeHttp({"hitCount": 1, "resultList": {"result": [ARTICLE]}}))
        made.append(p)
        return p
    monkeypatch.setattr(engine, "EuropePMCProvider", fake_provider)
    res = engine.find_candidates("practice nurse", "M1 1AE", 10, providers=[], suppression_entries=[], enrich_top=0)
    assert not made and not res.candidates                       # off by default
    res = engine.find_candidates("practice nurse", "M1 1AE", 10, providers=[], europepmc=True, enrich_top=0,
                                 suppression_entries=["k.brown@nhs.net"])
    assert [c.email for c in res.candidates] == ["gemma.beardmore@nhs.net"] and res.suppressed == 1
    assert "PECR" in res.research_note
    assert any(r.name == "europepmc" and r.found == 2 for r in res.reports)


def test_message_links():
    cs = [Candidate(name="A", provider="bluesky", profile_url="https://bsky.app/profile/a.bsky.social"),
          Candidate(name="B", provider="mastodon", profile_url="https://mastodon.social/@b"),
          Candidate(name="C", provider="mastodon + bluesky", profile_url="https://mas.to/@c",
                    other_profiles="https://bsky.app/profile/c.uk"),
          Candidate(name="D", provider="bluesky", profile_url="https://bsky.app/profile/d", email="d@x.example"),
          Candidate(name="E", provider="github", profile_url="https://github.com/e"),
          Candidate(name="F", kind="business", provider="bluesky", profile_url="https://bsky.app/profile/f")]
    assert engine.add_message_links(cs) == 3
    assert (cs[0].message_via, cs[1].message_via, cs[2].message_via) == ("Bluesky", "Mastodon", "Bluesky")
    assert cs[2].message_url == "https://bsky.app/profile/c.uk"
    assert not cs[3].message_url and not cs[4].message_url and not cs[5].message_url
    assert "message_url" in Candidate.field_names()


def test_employer_kinds():
    assert emp.employer_kinds("practice nurse") == ["gp", "independent"]
    assert emp.employer_kinds("care worker") == ["care_home", "home_care"]
    assert emp.employer_kinds("Senior carer") == ["care_home", "home_care"]
    assert emp.employer_kinds("HCA") == ["gp", "care_home", "hospital", "independent"]
    assert emp.employer_kinds("dental nurse") == ["dental"]
    assert "gp" in emp.employer_kinds("staff nurse")
    assert emp.employer_kinds("electrician") == ["trades"] and emp.employer_kinds("accountant") == ["accountancy"] and emp.employer_kinds("software engineer") == []
    assert emp.employer_kinds("family lawyer") == ["barristers"] and emp.employer_kinds("chef") == ["food"]


class FakeOds:
    """postcodes.io outcodes + bulk postcodes, NHS ODS search + organisation records."""

    def __init__(self):
        self.calls = []
        self.session = self

    def _throttle(self, host):
        pass

    def get_json(self, url, params=None, **kw):
        self.calls.append(url)
        if url.endswith("/outcodes"):
            return {"result": [{"outcode": "M13", "latitude": 53.46, "longitude": -2.22},
                               {"outcode": "M1", "latitude": 53.48, "longitude": -2.23}]}
        if url == emp.ODS:
            assert params["PostCode"] == "M1"           # one prefix request covers M1 and M13
            return {"Organisations": [{"Name": "CITY CENTRE MEDICAL PRACTICE", "OrgId": "P1", "PostCode": "M1 2AB"},
                                      {"Name": "WRONG DISTRICT SURGERY", "OrgId": "P2", "PostCode": "M11 3BB"},
                                      {"Name": "VALLANCE MEDICAL", "OrgId": "P3", "PostCode": "M13 9UJ"}]}
        if url.endswith("/P1"):
            return {"Organisation": {"GeoLoc": {"Location": {"AddrLn1": "1 HIGH STREET", "Town": "MANCHESTER"}},
                                     "Contacts": {"Contact": [{"type": "tel", "value": "0161 000 0001"}]}}}
        if url.endswith("/P3"):
            return {"Organisation": {"GeoLoc": {"Location": {"AddrLn1": "2 OXFORD ROAD", "Town": "MANCHESTER"}}}}
        raise AssertionError(url)

    def post(self, url, json=None, timeout=None):
        self.calls.append(url)
        coords = {"M12AB": (53.481, -2.231), "M139UJ": (53.46, -2.22), "M113BB": (53.48, -2.18)}
        res = [{"query": p, "result": {"latitude": coords[p][0], "longitude": coords[p][1]} if p in coords else None}
               for p in json["postcodes"]]
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"result": res})


class FakeOverpass:
    def __init__(self, els):
        self.els, self.calls = els, 0

    def get_json(self, url, params=None):
        self.calls += 1
        assert '"amenity"="doctors"' in params["data"]
        return {"elements": self.els}


def test_employer_leads_ods_osm_merge_and_suppression():
    osm = [{"type": "node", "id": 1, "lat": 53.4811, "lon": -2.2311, "tags": {
                "amenity": "doctors", "name": "City Centre Medical Practice", "addr:postcode": "M1 2AB",
                "website": "https://citycentremp.nhs.uk", "phone": "0161 000 0001"}},
           {"type": "way", "id": 2, "center": {"lat": 53.47, "lon": -2.24}, "tags": {
                "amenity": "doctors", "name": "Hulme Surgery", "email": "hulme.surgery@nhs.net"}},
           {"type": "node", "id": 3, "lat": 54.5, "lon": -2.2, "tags": {"amenity": "doctors", "name": "Far Away"}}]
    ods, ov = FakeOds(), FakeOverpass(osm)
    f = emp.EmployerLeadsFinder(http=ods, osm_http=ov, registers=False)
    leads, n = f.find("practice nurse", MAN, 5, entries=[])
    names = [l.name for l in leads]
    assert names[0] == "City Centre Medical Practice" and "Vallance Medical" in names and "Hulme Surgery" in names
    assert "Wrong District Surgery" not in names and "Far Away" not in names   # M11 not a nearby district; too far
    city = leads[0]
    assert city.source == "NHS ODS + OpenStreetMap" and city.phone == "0161 000 0001"
    assert city.website == "https://citycentremp.nhs.uk" and city.address.startswith("1 High Street")
    assert city.employer_type == "GP practice" and city.note.startswith("Employer")
    assert len(names) == 3 and n == 0 and ov.calls == 1
    assert sum(1 for c in ods.calls if c == emp.ODS) == 1
    leads2, n2 = emp.EmployerLeadsFinder(http=ods, osm_http=ov, registers=False).find(
        "practice nurse", MAN, 5, entries=["@citycentremp.nhs.uk", "hulme.surgery@nhs.net"])
    assert n2 == 2 and [l.name for l in leads2] == ["Vallance Medical"]
    assert ov.calls == 1                                            # Overpass result cached


def test_employers_only_for_care_roles_and_in_engine(monkeypatch):
    monkeypatch.setattr(engine, "lookup_postcode", lambda http, pc, r: MAN)
    monkeypatch.setattr(engine, "PlaceGeocoder", lambda http: None)
    calls = []

    class F:
        notes, stats = [], {"with_phone": 1}

        def find(self, title, place, radius, entries, progress):
            calls.append(title)
            return [emp.EmployerLead(name="Oak Care Home", employer_type="Care home / nursing home")], 0
    monkeypatch.setattr(engine, "EmployerLeadsFinder", lambda *a, **kw: F())
    res = engine.find_candidates("care worker", "M1 1AE", 10, providers=[], suppression_entries=[], enrich_top=0)
    assert [e.name for e in res.employers] == ["Oak Care Home"] and "not candidates" in res.employers_note
    assert not res.candidates                                     # employers never mixed into people rows
    res = engine.find_candidates("electrician", "M1 1AE", 10, providers=[], suppression_entries=[], enrich_top=0)
    assert calls == ["care worker", "electrician"]
    res = engine.find_candidates("care worker", "M1 1AE", 10, providers=[], suppression_entries=[], enrich_top=0,
                                 employers=False)
    assert not res.employers


def test_employer_csv_export(tmp_path):
    p = engine.export_employers([emp.EmployerLead(name="Oak", phone="0161", source="NHS ODS")], tmp_path / "cands")
    txt = p.read_text(encoding="utf-8-sig")
    assert p.name == "cands_employers.csv" and "Employer - not a candidate" in txt and "lat" not in txt.split("\n")[0]
