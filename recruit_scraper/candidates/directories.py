"""Business / professional listings that were published to be found (non-tech jobs: trades, accountants, lawyers…).

OpenStreetMapProvider
    Businesses and sole traders mapped in OpenStreetMap with a trade/office tag (craft=electrician, craft=plumber,
    office=accountant, office=lawyer …) near the postcode, with the contact details tagged on them (email, phone,
    website). Data © OpenStreetMap contributors, ODbL (attribution shown). Read through the public Overpass API with a
    unique User-Agent, cached for a day, one query per search - well inside the Overpass usage policy (<10,000
    queries/day; no parallel scripts). These rows are labelled kind="business": firms / sole traders, NOT job seekers.
TeamPageProvider
    'Our people' / 'meet the team' pages YOU paste (law firms, chambers, practices, agencies). Reads schema.org
    Person data the employer published (JSON-LD, Microdata, RDFa via scrapinghub/extruct), robots.txt + noindex
    honoured. Only people whose published job title matches the search are kept; contact details are the business
    ones the employer published on that page.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Callable, Iterator, Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from ..http import HttpClient, RobotsDisallowed
from ..utils import find_emails
from .cache import DiskCache
from .contacts import SiteContactFinder, _noindex
from .geo import Place, PlaceGeocoder, haversine_miles
from .matching import RoleMatcher, contract_type
from .models import Candidate

log = logging.getLogger(__name__)

# job words -> OpenStreetMap tags for businesses that do that job
OSM_TAGS: dict[str, list[tuple[str, str]]] = {
    "electrician": [("craft", "electrician")],
    "plumber": [("craft", "plumber"), ("craft", "hvac"), ("craft", "heating_engineer")],
    "heating engineer": [("craft", "heating_engineer"), ("craft", "hvac"), ("craft", "plumber")],
    "gas engineer": [("craft", "heating_engineer"), ("craft", "plumber")],
    "carpenter": [("craft", "carpenter"), ("craft", "joiner")], "joiner": [("craft", "joiner"), ("craft", "carpenter")],
    "builder": [("craft", "builder")], "roofer": [("craft", "roofer")], "plasterer": [("craft", "plasterer")],
    "painter": [("craft", "painter")], "decorator": [("craft", "painter")], "tiler": [("craft", "tiler")],
    "locksmith": [("craft", "locksmith"), ("shop", "locksmith")], "glazier": [("craft", "glaziery")],
    "gardener": [("craft", "gardener")], "landscaper": [("craft", "gardener")], "stonemason": [("craft", "stonemason")],
    "upholsterer": [("craft", "upholsterer")], "tailor": [("craft", "tailor")], "photographer": [("craft", "photographer")],
    "mechanic": [("shop", "car_repair")], "vehicle technician": [("shop", "car_repair")],
    "cleaner": [("craft", "cleaning")], "chef": [("craft", "caterer")], "caterer": [("craft", "caterer")],
    "accountant": [("office", "accountant"), ("office", "tax_advisor")], "bookkeeper": [("office", "accountant")],
    "tax adviser": [("office", "tax_advisor")], "tax advisor": [("office", "tax_advisor")],
    "lawyer": [("office", "lawyer"), ("office", "notary")], "solicitor": [("office", "lawyer")],
    "barrister": [("office", "lawyer")], "conveyancer": [("office", "lawyer")], "paralegal": [("office", "lawyer")],
    "legal executive": [("office", "lawyer")], "legal secretary": [("office", "lawyer")],
    "estate agent": [("office", "estate_agent")], "financial adviser": [("office", "financial_advisor")],
    "financial advisor": [("office", "financial_advisor")], "insurance": [("office", "insurance")],
    "architect": [("office", "architect")], "surveyor": [("office", "surveyor")],
    "hairdresser": [("shop", "hairdresser")], "barber": [("shop", "hairdresser")], "beautician": [("shop", "beauty")],
    "physiotherapist": [("healthcare", "physiotherapist")], "optician": [("shop", "optician")],
    "dentist": [("amenity", "dentist")], "vet": [("amenity", "veterinary")],
}
OVERPASS = ("https://overpass-api.de/api/interpreter", "https://overpass.private.coffee/api/interpreter")


def osm_tags_for(matcher: RoleMatcher) -> list[tuple[str, str]]:
    names = [matcher.core] + matcher.synonyms + matcher.related
    out: list[tuple[str, str]] = []
    for key, tags in OSM_TAGS.items():
        if any(n == key or re.search(rf"\b{re.escape(key)}\b", n) for n in names[:1]) or key in names:
            out += [t for t in tags if t not in out]
    return out


class OpenStreetMapProvider:
    name = "openstreetmap"
    label = "OpenStreetMap businesses"

    def __init__(self, http: Optional[HttpClient] = None, fetch_websites: bool = True, max_sites: int = 25,
                 site_http: Optional[HttpClient] = None):
        self.http = http or HttpClient(min_interval=2.0, timeout=60, retries=3, backoff=3)
        self.fetch_websites = fetch_websites
        self.max_sites = max_sites
        self.site_http = site_http
        self.notes: list[str] = []
        self.stats = {"listings": 0, "with_email": 0, "with_phone": 0, "websites_checked": 0, "website_emails": 0}
        self.cache = DiskCache("osm", 86400)

    def query(self, tags: list[tuple[str, str]], place: Place, radius_miles: float) -> str:
        r = int(min(max(radius_miles, 1), 25) * 1609.344)
        parts = "".join(f'nwr["{k}"="{v}"](around:{r},{place.lat:.5f},{place.lon:.5f});' for k, v in tags)
        return f"[out:json][timeout:80];({parts});out center tags 400;"

    def _run(self, q: str) -> list[dict]:
        hit = self.cache.get(q)
        if hit is not None:
            return hit
        last: Optional[Exception] = None
        for url in OVERPASS:
            try:
                els = self.http.get_json(url, params={"data": q}).get("elements") or []
                self.cache.set(q, els)
                return els
            except (requests.RequestException, ValueError) as e:
                last = e
                log.info("overpass %s failed: %s", url, e)
        log.info("overpass unavailable: %s", last)
        raise RuntimeError("OpenStreetMap's free Overpass servers are busy right now - search again in a minute "
                           "(results are cached for a day once they load)")

    def search(self, title: str, place: Place, radius_miles: float, min_exp, max_exp, keywords: list[str],
               include_unknown_distance: bool, geocoder: PlaceGeocoder,
               progress: Callable[[str], None] = lambda m: None) -> Iterator[Candidate]:
        matcher = RoleMatcher(title, keywords)
        tags = osm_tags_for(matcher)
        if not tags:
            self.notes.append(f"No OpenStreetMap business category for '{title}' (covers trades, accountants, "
                              "lawyers, estate agents, hairdressers …).")
            return
        progress(f"OpenStreetMap: {', '.join(f'{k}={v}' for k, v in tags)} businesses near {place.town}…")
        els = self._run(self.query(tags, place, radius_miles))
        out: list[Candidate] = []
        for e in els:
            t = e.get("tags") or {}
            name = t.get("name") or t.get("brand") or ""
            if not name:
                continue
            lat = e.get("lat") or (e.get("center") or {}).get("lat")
            lon = e.get("lon") or (e.get("center") or {}).get("lon")
            dist = round(haversine_miles(place.lat, place.lon, lat, lon), 1) if lat and lon else None
            if dist is not None and dist > radius_miles:
                continue
            what = next((f"{k}={t[k]}" for k, _ in tags if k in t), "")
            email = next((x for x in find_emails(t.get("email") or t.get("contact:email") or "")), "")
            phone = t.get("phone") or t.get("contact:phone") or t.get("contact:mobile") or ""
            site = t.get("website") or t.get("contact:website") or t.get("url") or ""
            addr = ", ".join(filter(None, [" ".join(filter(None, [t.get("addr:housenumber"), t.get("addr:street")])),
                                           t.get("addr:city"), t.get("addr:postcode")]))
            osm_url = f"https://www.openstreetmap.org/{e.get('type', 'node')}/{e.get('id')}"
            self.stats["listings"] += 1
            out.append(Candidate(
                name=name, headline=f"Business listing ({what.replace('=', ': ')}) - a firm or sole trader, not a job seeker",
                company=name, location=addr or t.get("addr:city") or "", distance_miles=dist, email=email,
                email_source=f"published for this business in OpenStreetMap {osm_url} (© OpenStreetMap contributors, ODbL)"
                if email else "", phone=phone.split(";")[0].strip(), profile_url=osm_url, website=site,
                kind="business", provider="openstreetmap", match_score=2.0,
                match_reason=f"OpenStreetMap lists it as {what}", contract=contract_type(t.get("description", ""))))
        if self.fetch_websites:
            need = [c for c in out if c.website and not c.email][: self.max_sites]
            if need:
                progress(f"OpenStreetMap: checking {len(need)} business websites for published contact emails…")
                from concurrent.futures import ThreadPoolExecutor
                sites = SiteContactFinder(self.site_http or HttpClient(min_interval=1.0, timeout=12, retries=1))
                with ThreadPoolExecutor(max_workers=6) as ex:
                    for c, res in zip(need, ex.map(lambda c: sites.personal(c.website), need)):
                        self.stats["websites_checked"] += 1
                        if res["emails"]:
                            c.email = res["emails"][0]
                            c.email_source = f"published on the business's own website {res['source_url']}"
                            self.stats["website_emails"] += 1
                        if res["phones"] and not c.phone:
                            c.phone = res["phones"][0]
        for c in out:
            self.stats["with_email"] += bool(c.email)
            self.stats["with_phone"] += bool(c.phone)
            if not c.email:
                c.email_source = "none published"
            yield c
        if not out:
            self.notes.append(f"No {', '.join(v for _, v in tags)} businesses mapped within {radius_miles:g} miles.")


def _people_from_html(html: str, url: str) -> list[dict]:
    """schema.org Person records on a page (JSON-LD, Microdata, RDFa)."""
    found: list[dict] = []

    def walk(x):
        if isinstance(x, list):
            for i in x:
                walk(i)
        elif isinstance(x, dict):
            typ = x.get("@type") or x.get("type") or ""
            typ = " ".join(typ) if isinstance(typ, list) else str(typ)
            if typ.split("/")[-1] == "Person" or typ.endswith("Person"):
                found.append(x)
            for v in x.values():
                if isinstance(v, (list, dict)):
                    walk(v)

    try:
        import extruct  # https://github.com/scrapinghub/extruct
        data = extruct.extract(html, base_url=url, syntaxes=["json-ld", "microdata", "rdfa"], uniform=True,
                               errors="ignore")
        walk(list(data.values()))
    except Exception:  # noqa: BLE001 - fall back to plain JSON-LD
        soup = BeautifulSoup(html, "lxml")
        for tag in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
            try:
                walk(json.loads(tag.string or ""))
            except Exception:  # noqa: BLE001
                continue
    return found


def _val(x) -> str:
    if isinstance(x, list):
        x = x[0] if x else ""
    if isinstance(x, dict):
        x = x.get("name") or x.get("@value") or x.get("url") or ""
    return str(x or "").strip()


class TeamPageProvider:
    name = "team_pages"
    label = "Team / 'our people' pages"

    def __init__(self, urls: list[str], http: Optional[HttpClient] = None):
        self.urls = [u for u in urls if u.startswith("http")]
        self.http = http or HttpClient(min_interval=1.5, timeout=20, retries=1)
        self.notes: list[str] = []
        self.stats = {"pages": 0, "people_on_pages": 0, "job_matches": 0}

    def search(self, title: str, place: Place, radius_miles: float, min_exp, max_exp, keywords: list[str],
               include_unknown_distance: bool, geocoder: PlaceGeocoder,
               progress: Callable[[str], None] = lambda m: None) -> Iterator[Candidate]:
        if not self.urls:
            return
        matcher = RoleMatcher(title, keywords)
        for url in self.urls[:30]:
            progress(f"Reading team page {url}…")
            try:
                r = self.http.get(url, check_robots=True, headers={"Accept": "text/html"})
            except RobotsDisallowed:
                self.notes.append(f"{urlparse(url).netloc}: robots.txt disallows it - skipped")
                continue
            except requests.RequestException as e:
                self.notes.append(f"{urlparse(url).netloc}: {str(e)[:80]}")
                continue
            soup = BeautifulSoup(r.text, "lxml")
            if _noindex(r, soup):
                self.notes.append(f"{urlparse(url).netloc}: page asks not to be indexed - skipped")
                continue
            self.stats["pages"] += 1
            people = _people_from_html(r.text, r.url)
            self.stats["people_on_pages"] += len(people)
            org = _val((soup.find("meta", property="og:site_name") or {}).get("content") if soup.find("meta", property="og:site_name") else "") or urlparse(r.url).netloc
            for p in people:
                name, job = _val(p.get("name")), _val(p.get("jobTitle"))
                m = matcher.match(job, _val(p.get("description")))
                if not name or not m.score:
                    continue
                self.stats["job_matches"] += 1
                email = next(iter(find_emails(_val(p.get("email")).replace("mailto:", ""))), "")
                loc = _val(p.get("workLocation")) or _val(p.get("address"))
                co = geocoder.geocode(loc) if loc else None
                dist = round(haversine_miles(place.lat, place.lon, *co), 1) if co else None
                if dist is not None and dist > radius_miles + 3:
                    continue
                yield Candidate(
                    name=name, headline=job, company=_val(p.get("worksFor")) or org, location=loc,
                    distance_miles=dist, email=email,
                    email_source=f"published by their employer on {r.url}" if email else "none published on the page",
                    phone=_val(p.get("telephone")), profile_url=_val(p.get("url")) or r.url,
                    kind="candidate", provider=f"team page ({urlparse(r.url).netloc})", match_score=m.score,
                    match_reason=f"job title on employer's page: {job}"[:120], contract=contract_type(job))
        if self.stats["pages"] and not self.stats["people_on_pages"]:
            self.notes.append("No schema.org Person data on those pages (many sites don't publish it).")
