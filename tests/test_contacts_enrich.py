"""Self-published contact details: bio text, links on the person's own profile, link-in-bio pages, one hop to a
contact page, 'name [at] domain' decoding, ownership rules and firm addresses routed to Businesses (offline)."""
from types import SimpleNamespace

import pytest

from recruit_scraper.candidates import engine
from recruit_scraper.candidates.enrich import (ContactEnricher, apply_contacts, deobfuscate_emails, enrich_candidates,
                                               parse_contact_page, skip_url, text_contacts)
from recruit_scraper.candidates.models import Candidate
from recruit_scraper.http import RobotsDisallowed


@pytest.fixture(autouse=True)
def _cache(tmp_path, monkeypatch):
    from recruit_scraper.candidates import cache as cache_mod
    monkeypatch.setattr(cache_mod, "CACHE_ROOT", tmp_path)
    monkeypatch.setattr(cache_mod.DiskCache.__init__, "__defaults__", (tmp_path,))


class FakeHttp:
    def __init__(self, pages, blocked=()):
        self.pages, self.blocked, self.calls = pages, set(blocked), []

    def get(self, url, check_robots=False, headers=None, **kw):
        assert check_robots, "every contact page fetch must check robots.txt"
        self.calls.append(url)
        if url in self.blocked:
            raise RobotsDisallowed(url)
        if url not in self.pages:
            import requests
            raise requests.HTTPError("404", response=SimpleNamespace(status_code=404))
        return SimpleNamespace(text=self.pages[url], content=self.pages[url].encode(), url=url,
                               headers={"Content-Type": "text/html"})


def html(body, title="", head=""):
    return f"<html><head><title>{title}</title>{head}</head><body>{body}</body></html>"


def test_deobfuscation_only_for_real_addresses():
    assert deobfuscate_emails("Email: jo [at] bloggs [dot] com") == ["jo@bloggs.com"]
    assert deobfuscate_emails("jo.smith(at)smithlaw.co.uk please") == ["jo.smith@smithlaw.co.uk"]
    assert deobfuscate_emails("write to jo at bloggs dot org") == ["jo@bloggs.org"]
    assert deobfuscate_emails("jo {at} nhs {dot} net") == ["jo@nhs.net"]
    assert deobfuscate_emails("Practice nurse at NHS. Mum of two") == []
    assert deobfuscate_emails("Solicitor at Smith Law.co with dogs") == []   # plain 'at' needs a written 'dot'


def test_bio_text_contacts():
    emails, phones = text_contacts("Practice nurse. Bookings: sam [at] nursesam [dot] co.uk | 07911 123456 | 2024-11-10")
    assert emails == ["sam@nursesam.co.uk"] and phones == ["+44 7911 123456"]
    assert text_contacts("jo [at] x [dot] com", decode_obfuscated=False)[0] == []


def test_parse_contact_page_mailto_tel_schema_and_obfuscated():
    ld = ('<script type="application/ld+json">{"@type":"Person","name":"Jo Bloggs","email":"mailto:jo@bloggs.example",'
          '"contactPoint":{"@type":"ContactPoint","telephone":"0161 496 0000"}}</script>')
    got = parse_contact_page(html('<a href="tel:07911123456">call</a> or write jo [at] bloggs [dot] co.uk '
                                  '<a href="/contact">Contact me</a>', "Jo Bloggs - Nurse", ld), "https://bloggs.example/")
    assert got["emails"] == ["jo@bloggs.example"] and got["obfuscated"] == ["jo@bloggs.co.uk"]
    assert "+44 7911 123456" in got["phones"] and "+44 161 496 0000" in got["phones"]
    assert ("https://bloggs.example/contact", "Contact me") in got["links"] and got["title"] == "Jo Bloggs - Nurse"


def test_link_in_bio_page_then_own_site_then_contact_hop():
    pages = {
        "https://bio.link/samnurse": html('<a href="https://samjones.co.uk">My site</a> <a href="https://x.com/s">X</a>'),
        "https://samjones.co.uk": html('<a href="https://samjones.co.uk/get-in-touch">Get in touch</a>', "Sam Jones"),
        "https://samjones.co.uk/get-in-touch": html('<a href="mailto:hello@samjones.co.uk">mail</a> 07911 123456'),
    }
    http = FakeHttp(pages)
    c = Candidate(name="Sam Jones", other_profiles="https://bio.link/samnurse https://bsky.app/profile/x", kind="candidate")
    r = ContactEnricher(http, max_pages=3).enrich(c)
    assert r.email == "hello@samjones.co.uk" and r.source == "https://samjones.co.uk/get-in-touch"
    assert r.phone == "+44 7911 123456"
    assert http.calls == list(pages)                       # x.com / bsky.app never fetched
    apply_contacts(c, r)
    assert c.contact_source == "https://samjones.co.uk/get-in-touch" and "linked from their own profile" in c.email_source


def test_page_cap_robots_and_never_fetched_hosts():
    http = FakeHttp({"https://a.example": html("nothing"), "https://a.example/contact": html("nothing"),
                     "https://b.example": html("nothing")}, blocked={"https://c.example"})
    c = Candidate(name="Al", website="https://c.example",
                  other_profiles="https://a.example https://b.example https://d.example https://linktr.ee/al")
    r = ContactEnricher(http, max_pages=3).enrich(c)
    assert len(r.pages) == 3 and not r.email and "robots.txt" in r.note
    assert skip_url("https://linktr.ee/al") and skip_url("https://www.linkedin.com/in/al")
    assert not any("linktr.ee" in u or "d.example" in u for u in http.calls)


def test_firm_addresses_go_to_businesses_and_staff_lists_need_the_name():
    firm = html("Contact our solicitors: info@bigfirm.co.uk", "Big Firm LLP - Family Law")
    staff = html("kay.dee@bigfirm.co.uk tom@bigfirm.co.uk ann@bigfirm.co.uk bob@bigfirm.co.uk", "Our people")
    http = FakeHttp({"https://bigfirm.co.uk": firm, "https://bigfirm.co.uk/contact": firm,
                     "https://bigfirm.co.uk/people": staff})
    c = Candidate(name="Kay Dee", kind="candidate", other_profiles="https://bigfirm.co.uk", location="Newport",
                  provider="bluesky", match_score=3)
    r = ContactEnricher(http, max_pages=2).enrich(c)
    assert r.email == "" and r.business["email"] == "info@bigfirm.co.uk"
    b = apply_contacts(c, r)
    assert c.email == "" and "message them via their profile" in c.email_source
    assert b.kind == "business" and b.email == "info@bigfirm.co.uk" and b.contact_source == "https://bigfirm.co.uk"
    c2 = Candidate(name="Kay Dee", kind="candidate", other_profiles="https://bigfirm.co.uk/people")
    r2 = ContactEnricher(http, max_pages=1).enrich(c2)
    assert r2.email == "kay.dee@bigfirm.co.uk"
    c3 = Candidate(name="Kim Lee", kind="candidate", other_profiles="https://bigfirm.co.uk/people")
    assert ContactEnricher(http, max_pages=1).enrich(c3).email == ""


def test_own_site_generic_address_is_theirs():
    http = FakeHttp({"https://kimlee.co.uk": html("Say hi: hello@kimlee.co.uk", "Kim Lee, practice nurse")})
    r = ContactEnricher(http).enrich(Candidate(name="Kim Lee", website="https://kimlee.co.uk"))
    assert r.email == "hello@kimlee.co.uk" and "their own site" in r.how


def test_obfuscated_addresses_can_be_switched_off():
    http = FakeHttp({"https://kimlee.co.uk": html("kim [at] kimlee [dot] co.uk", "Kim Lee")})
    assert ContactEnricher(http, decode_obfuscated=False, max_pages=1).enrich(
        Candidate(name="Kim Lee", website="https://kimlee.co.uk")).email == ""
    assert ContactEnricher(http, max_pages=1).enrich(
        Candidate(name="Kim Lee", website="https://kimlee.co.uk")).email == "kim@kimlee.co.uk"


def test_enrich_candidates_top_n_only_people_without_email():
    seen = []

    class Enr:
        def enrich(self, c, deadline):
            seen.append(c.name)
            from recruit_scraper.candidates.enrich import ContactResult
            return ContactResult(email=f"{c.name.lower()}@{c.name.lower()}.example", source="https://s.example",
                                 how="published on the page", pages=["https://s.example"])

    cands = [Candidate(name="A", website="https://a.example"), Candidate(name="B", email="b@x.example",
                                                                          website="https://b.example"),
             Candidate(name="C", kind="business", website="https://c.example"),
             Candidate(name="D"), Candidate(name="E", website="https://e.example"),
             Candidate(name="F", website="https://f.example")]
    extra, stats = enrich_candidates(cands, top_n=2, seconds=10, enricher=Enr())
    assert sorted(seen) == ["A", "E"] and stats["emails_found"] == 2 and not extra
    assert cands[0].email == "a@a.example" and cands[0].contact_source == "https://s.example"


def test_engine_reapplies_suppression_after_contact_check(monkeypatch):
    from recruit_scraper.candidates.geo import Place
    import recruit_scraper.candidates.enrich as enr
    monkeypatch.setattr(engine, "lookup_postcode",
                        lambda http, pc, r: Place("M1 1AE", 53.48, -2.23, "Manchester", "North West", "England"))
    monkeypatch.setattr(engine, "PlaceGeocoder", lambda http: None)

    class Prov:
        name, notes, stats = "bluesky", [], {}

        def search(self, *a, **kw):
            yield Candidate(name="Sam", provider="bluesky", profile_url="https://bsky.app/profile/sam",
                            website="https://sam.example", match_score=3, distance_miles=1)
            yield Candidate(name="Pat", provider="bluesky", profile_url="https://bsky.app/profile/pat",
                            website="https://pat.example", match_score=3, distance_miles=2)

    monkeypatch.setattr(engine, "BlueskyProvider", lambda **kw: Prov())

    def fake_enrich(cands, top, secs, **kw):
        for c in cands:
            c.email, c.contact_source = f"{c.name.lower()}@x.example", c.website
        return [], {"emails_found": 2}
    monkeypatch.setattr(enr, "enrich_candidates", fake_enrich)
    res = engine.find_candidates("practice nurse", "M1 1AE", 10, providers=["bluesky"],
                                 suppression_entries=["sam@x.example"])
    assert [c.name for c in res.candidates] == ["Pat"] and res.suppressed == 1
    assert "1 of 1 people published an email" in res.contact_summary
    assert any(r.name == "contacts" and r.found == 2 for r in res.reports)


def test_firm_accounts_keep_their_generic_address_and_dead_links_are_cached():
    http = FakeHttp({"https://lawfirm.example": html("Call 01983 123456 or info@lawfirm.co.uk", "Law Firm LLP")})
    c = Candidate(name="Island Family Law", kind="business", provider="bluesky", website="https://lawfirm.example")
    extra, stats = enrich_candidates([c], top_n=5, seconds=10, enricher=ContactEnricher(http, max_pages=2))
    assert c.email == "info@lawfirm.co.uk" and c.contact_source == "https://lawfirm.example" and not extra
    assert c.phone.startswith("+44 1983")
    n = len(http.calls)
    ContactEnricher(http, max_pages=2).enrich(Candidate(name="X", kind="business", website="https://lawfirm.example"))
    assert len(http.calls) == n          # both pages (incl. the 404 /contact) came from the cache


def test_fediverse_handles_are_not_emails_and_profiles_not_fetched():
    assert text_contacts("Follow me @nearlylegal@mas.to or mail jo@bloggs.co.uk")[0] == ["jo@bloggs.co.uk"]
    got = parse_contact_page(html("Find me at @sarah@c.im"), "https://sarah.example/")
    assert got["emails"] == []
    assert skip_url("https://mas.to/@nearlylegal") and skip_url("https://pixelfed.social/users/jo")
