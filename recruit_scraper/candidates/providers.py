"""Candidate providers - lawful, no-key public sources only.

GitHubProvider
    Public GitHub user search (unauthenticated; an optional token only raises rate limits). Many query variants
    (nearby areas x skills), paginated up to GitHub's 1000-results-per-query cap, profiles cached on disk (7 days).
    Emails are NEVER taken from GitHub (GitHub Acceptable Use Policies s.7 forbid using GitHub data for unsolicited
    email to users, naming recruiters). If a person links their OWN website, that site is fetched politely
    (robots.txt + noindex honoured) and contact details the person self-published there are recorded with the
    source URL ("self-published on personal website").
JobAdContactsProvider
    Recruiting contacts employers published themselves: in job ads (descriptions, ATS application mailboxes) and
    role addresses (careers@, jobs@, talent@ …) on the company's own careers/contact pages.
"""
from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterator, Optional

import requests

from ..http import HttpClient
from .cache import DiskCache
from .contacts import SiteContactFinder
from .geo import Place, PlaceGeocoder, haversine_miles
from .matching import RoleMatcher, is_tech_role
from .models import Candidate

log = logging.getLogger(__name__)

# job words -> GitHub `language:` qualifiers (most specific first)
LANGUAGE_MAP = [
    (r"\bpython|django|flask|fastapi|pandas\b", ["Python"]),
    (r"\btypescript|angular|next\.?js\b", ["TypeScript", "JavaScript"]),
    (r"\bjavascript|node(\.?js)?|react|vue|front[- ]?end|frontend|full[- ]?stack\b", ["JavaScript", "TypeScript"]),
    (r"\bjava\b|spring", ["Java", "Kotlin"]),
    (r"\bkotlin|android\b", ["Kotlin", "Java"]),
    (r"c#|\.net|dotnet|asp\.net", ["C#"]),
    (r"\bgo(lang)?\b", ["Go"]),
    (r"\bruby|rails\b", ["Ruby"]),
    (r"\bphp|laravel|wordpress|symfony\b", ["PHP"]),
    (r"\brust\b", ["Rust"]),
    (r"\bswift|ios\b", ["Swift"]),
    (r"\bscala|spark\b", ["Scala", "Python"]),
    (r"\bc\+\+|embedded|unreal\b", ["C++", "C"]),
    (r"\bdata (scien|engineer|analyst)|machine learning|\bml\b|\bai\b|analytics", ["Python", "Jupyter Notebook"]),
    (r"\bdevops|sre|platform engineer|terraform|infrastructure|cloud engineer\b", ["HCL", "Shell", "Go"]),
    (r"\bshell|bash|linux\b", ["Shell"]),
    (r"\b(r developer|r programmer|rstats|statistician)\b", ["R"]),
]
# extra languages that usually come with a skill (title synonyms -> related stacks)
RELATED = {"Python": ["Jupyter Notebook"], "JavaScript": ["TypeScript"], "TypeScript": ["JavaScript"],
           "Java": ["Kotlin"], "C#": [], "Go": [], "Ruby": [], "PHP": []}
# metro / county names people often put in their GitHub location instead of the district name
AREA_SYNONYMS = {
    "manchester": ["Greater Manchester"], "salford": ["Greater Manchester"], "trafford": ["Greater Manchester"],
    "london": ["Greater London"], "westminster": ["London"], "city of london": ["London"], "camden": ["London"],
    "hackney": ["London"], "islington": ["London"], "southwark": ["London"], "tower hamlets": ["London"],
    "birmingham": ["West Midlands"], "leeds": ["West Yorkshire"], "bradford": ["West Yorkshire"],
    "liverpool": ["Merseyside"], "newcastle upon tyne": ["Newcastle", "Tyne and Wear"], "sheffield": ["South Yorkshire"],
    "glasgow city": ["Glasgow"], "city of edinburgh": ["Edinburgh"], "bristol, city of": ["Bristol"],
    "brighton and hove": ["Brighton"], "cardiff": ["Cardiff"], "cambridge": ["Cambridge"], "oxford": ["Oxford"],
}
_SOCIAL_ONLY = re.compile(r"linkedin\.com|twitter\.com|x\.com|facebook\.com|instagram\.com", re.I)


def languages_for(title: str) -> list[str]:
    out: list[str] = []
    for pat, langs in LANGUAGE_MAP:
        if re.search(pat, title or "", re.I):
            for lang in langs:
                if lang not in out:
                    out.append(lang)
    if out and len(out) == 1:
        out += [l for l in RELATED.get(out[0], []) if l not in out]
    return out


def location_variants(place: Place, max_areas: int) -> list[str]:
    """Town, nearby districts and common synonyms ('Greater Manchester'), de-duplicated, nearest first."""
    out: list[str] = []

    def add(x: str) -> None:
        x = (x or "").strip()
        if x and x.lower() not in [o.lower() for o in out]:
            out.append(x)

    for town in (place.nearby_towns or [place.town])[:max_areas]:
        add(re.sub(r",.*$", "", town))
        for syn in AREA_SYNONYMS.get(town.lower(), []):
            add(syn)
    return out


class ProviderStop(Exception):
    """Raised to stop a provider early (e.g. rate limit) with a friendly message."""


class GitHubProvider:
    name = "github"
    label = "GitHub public profiles"
    API = "https://api.github.com"

    def __init__(self, http: HttpClient, token: Optional[str] = None, max_towns: int = 6,
                 max_profiles: int = 200, hireable_only: bool = False, max_results: int = 1000,
                 include_unread: bool = True, fetch_websites: bool = True, max_wait_minutes: float = 0,
                 site_http: Optional[HttpClient] = None):
        self.http = http
        self.token = token or None
        self.max_towns = max_towns
        self.max_profiles = max_profiles
        self.hireable_only = hireable_only
        self.max_results = max_results
        self.include_unread = include_unread
        self.fetch_websites = fetch_websites
        self.max_wait = max_wait_minutes * 60
        self.notes: list[str] = []
        self.stats = {"search_calls": 0, "accounts_found": 0, "profiles_read": 0, "profiles_cached": 0,
                      "profiles_unread": 0, "websites_checked": 0, "website_emails": 0}
        self.user_cache = DiskCache("github_users", 7 * 86400)
        self.search_cache = DiskCache("github_search", 86400)
        self.sites = SiteContactFinder(site_http or HttpClient(min_interval=1.0, timeout=12, retries=1))
        self._last_search = 0.0
        self.search_interval = 2.2 if self.token else 6.5  # 30/min with token, 10/min without

    def _headers(self) -> dict:
        h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    @staticmethod
    def _reset_wait(r: requests.Response) -> float:
        reset = r.headers.get("X-RateLimit-Reset")
        if reset and reset.isdigit():
            return max(1.0, int(reset) - datetime.now(timezone.utc).timestamp() + 2)
        ra = r.headers.get("Retry-After")
        return float(ra) if ra and ra.isdigit() else 60.0

    def _get(self, url: str, params=None, kind: str = "core", progress: Callable[[str], None] = lambda m: None):
        while True:
            if kind == "search":
                wait = self._last_search + self.search_interval - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                self._last_search = time.monotonic()
            try:
                return self.http.get_json(url, params=params, headers=self._headers())
            except requests.HTTPError as e:
                r = e.response
                limited = r is not None and r.status_code in (403, 429) and (
                    r.headers.get("X-RateLimit-Remaining") == "0" or "rate limit" in r.text.lower())
                if r is not None and r.status_code == 401:
                    raise ProviderStop("GitHub token was rejected - remove it or create a new one.") from e
                if r is not None and r.status_code == 422:
                    return {"items": [], "total_count": 0}  # beyond the 1000-result cap / invalid query
                if not limited:
                    raise
                wait = self._reset_wait(r)
                # search limits reset every minute -> always wait; core (profiles) only up to max_wait
                if kind == "search" and wait <= 90 or wait <= self.max_wait:
                    progress(f"GitHub rate limit reached - waiting {int(wait)}s, then continuing…")
                    time.sleep(wait)
                    continue
                at = (datetime.now() + timedelta(seconds=wait)).strftime("%H:%M")
                raise ProviderStop(f"GitHub's free hourly limit for reading profiles was reached; more can be read "
                                   f"after about {at} (cached profiles are reused, so just search again then), "
                                   f"or add a free GitHub token under Advanced.") from e

    def build_queries(self, title: str, place: Place, min_exp: Optional[float], max_exp: Optional[float]) -> list[tuple[str, str]]:
        """(area, query) pairs: every nearby area x every skill language, with account-age qualifiers."""
        langs = languages_for(title)[:3]
        quals = []
        today = datetime.now(timezone.utc).date()
        if min_exp:
            quals.append(f"created:<={today - timedelta(days=int(min_exp * 365.25))}")
        if max_exp:
            quals.append(f"created:>={today - timedelta(days=int(max_exp * 365.25))}")
        quals.append("type:user")
        if not langs:
            if not is_tech_role(title):
                self.notes.append(f"Skipped: '{title}' isn't a software/data job, and GitHub only lists people "
                                  "who write code.")
                return []
            self.notes.append("No programming language recognised in the job title, so only people whose GitHub "
                              "bio mentions the job are kept.")
        out = []
        for area in location_variants(place, self.max_towns):
            loc = f'location:"{area}"' if " " in area else f"location:{area}"
            for lang in langs or [None]:
                lq = (f'language:"{lang}"' if " " in lang else f"language:{lang}") if lang else None
                out.append((area, " ".join([loc] + ([lq] if lq else []) + quals)))
        return out

    def _collect_logins(self, queries, progress) -> dict[str, str]:
        """Breadth-first over query variants (page 1 of every variant, then page 2 …) so all areas/skills
        contribute; each query is capped by GitHub at 1000 results (10 pages of 100)."""
        logins: dict[str, str] = {}  # login -> matched area
        active = list(queries)
        for page in range(1, 11):
            still = []
            for area, q in active:
                key = f"{q}|{page}"
                data = self.search_cache.get(key)
                if data is None:
                    progress(f"GitHub search: {q} (page {page})")
                    data = self._get(f"{self.API}/search/users",
                                     {"q": q, "per_page": 100, "page": page, "sort": "repositories"}, "search", progress)
                    self.stats["search_calls"] += 1
                    self.search_cache.set(key, {"items": [{"login": i["login"]} for i in data.get("items", [])],
                                                "total_count": data.get("total_count", 0)})
                items = data.get("items", [])
                for it in items:
                    logins.setdefault(it["login"], area)
                if len(logins) >= self.max_results:
                    return logins
                if len(items) == 100 and page * 100 < min(1000, data.get("total_count", 0)):
                    still.append((area, q))
            active = still
            if not active:
                break
        return logins

    def search(self, title: str, place: Place, radius_miles: float, min_exp: Optional[float],
               max_exp: Optional[float], keywords: list[str], include_unknown_distance: bool,
               geocoder: PlaceGeocoder, progress: Callable[[str], None] = lambda m: None) -> Iterator[Candidate]:
        matcher = RoleMatcher(title, keywords)
        langs = languages_for(title)[:3]
        queries = self.build_queries(title, place, min_exp, max_exp)
        if not queries:
            return
        progress(f"GitHub: {len(queries)} search variants (areas × skills)")
        logins = self._collect_logins(queries, progress)
        self.stats["accounts_found"] = len(logins)
        if not logins:
            return
        progress(f"Found {len(logins)} matching GitHub accounts; reading profiles (cached ones are free)…")
        kws = [k.lower() for k in keywords if k]
        now = datetime.now(timezone.utc)
        area_dist: dict[str, Optional[float]] = {}
        stopped: Optional[str] = None
        read: list[dict] = []
        unread: list[str] = []
        for i, login in enumerate(logins):
            u = self.user_cache.get(login)
            if u is not None:
                self.stats["profiles_cached"] += 1
            elif stopped or self.stats["profiles_read"] >= self.max_profiles:
                unread.append(login)
                continue
            else:
                try:
                    u = self._get(f"{self.API}/users/{login}", progress=progress)
                except ProviderStop as e:
                    stopped = str(e)
                    unread.append(login)
                    continue
                self.user_cache.set(login, {k: u.get(k) for k in (
                    "login", "name", "bio", "company", "location", "blog", "twitter_username", "hireable",
                    "created_at", "public_repos", "followers", "html_url", "type")})
                self.stats["profiles_read"] += 1
                if self.stats["profiles_read"] % 10 == 0:
                    progress(f"Read {self.stats['profiles_read']} profiles ({self.stats['profiles_cached']} from cache)…")
            read.append(u)
        if stopped:
            self.notes.append(stopped)
        elif unread:
            self.notes.append(f"Read the max of {self.max_profiles} new profiles; {len(unread)} more accounts are "
                              "listed without profile details (raise 'Max profiles' or search again later - "
                              "profiles already read are cached for 7 days).")

        cands: list[Candidate] = []
        for u in read:
            if (u.get("type") or "User") != "User":
                continue
            if self.hireable_only and not u.get("hireable"):
                continue
            text = " ".join(str(u.get(k) or "") for k in ("bio", "company", "name", "blog")).lower()
            if kws and not any(all(w in text for w in k.split()) for k in kws):
                continue
            m = matcher.match(u.get("bio"), u.get("company"), u.get("name"), u.get("blog"))
            if not m.score:
                if not langs:
                    continue  # no skill signal at all -> their bio has to mention the job
                m.score, m.reason = 1.0, f"has public code in {' / '.join(langs[:2])} (GitHub language search)"
            dist = None
            coords = geocoder.geocode(u.get("location") or "")
            if coords:
                dist = round(haversine_miles(place.lat, place.lon, *coords), 1)
                if dist > radius_miles:
                    continue
            elif not include_unknown_distance:
                continue
            yrs = None
            if u.get("created_at"):
                yrs = round((now - datetime.fromisoformat(u["created_at"].replace("Z", "+00:00"))).days / 365.25, 1)
                if (min_exp and yrs < min_exp) or (max_exp and yrs > max_exp):
                    continue
            blog = (u.get("blog") or "").strip()
            c = Candidate(
                name=u.get("name") or u.get("login"),
                headline=(u.get("bio") or "").strip().replace("\r\n", " ")[:300],
                company=(u.get("company") or "").lstrip("@").strip(),
                location=u.get("location") or "", distance_miles=dist, experience_years=yrs,
                experience_source=(f"estimated: GitHub account age; {u.get('public_repos', 0)} public repos "
                                   "(rough signal, not verified experience)") if yrs is not None else "",
                email_source="", profile_url=u.get("html_url") or f"https://github.com/{u.get('login')}",
                website="" if _SOCIAL_ONLY.search(blog) else blog,
                linkedin_url=blog if "linkedin.com" in blog.lower() else "",
                twitter_url=f"https://x.com/{u['twitter_username']}" if u.get("twitter_username") else "",
                hireable=bool(u.get("hireable")) if u.get("hireable") is not None else None,
                kind="candidate", provider="github", profile_read=True,
                match_score=m.score, match_reason=m.reason)
            cands.append(c)

        if self.fetch_websites:
            with_site = [c for c in cands if c.website]
            if with_site:
                progress(f"Checking {len(with_site)} personal websites for self-published contact details…")
            with ThreadPoolExecutor(max_workers=8) as ex:
                for c, res in zip(with_site, ex.map(lambda c: self.sites.personal(c.website), with_site)):
                    self.stats["websites_checked"] += 1
                    if res["emails"]:
                        c.email = res["emails"][0]
                        c.email_source = f"self-published on personal website {res['source_url']}"
                        self.stats["website_emails"] += 1
                    if res["phones"]:
                        c.phone = res["phones"][0]
                    c.linkedin_url = c.linkedin_url or (res["linkedin"] or "")
                    c.twitter_url = c.twitter_url or (res["twitter"] or "")
        for c in cands:
            if not c.email:
                c.email_source = ("none self-published on their website" if c.website else
                                  "not collected from GitHub (GitHub policy)")
            yield c

        if self.include_unread and not self.hireable_only and not kws and langs:
            for login in unread:
                area = logins[login]
                if area not in area_dist:
                    co = geocoder.geocode(area)
                    area_dist[area] = round(haversine_miles(place.lat, place.lon, *co), 1) if co else None
                d = area_dist[area]
                if d is not None and d > radius_miles + 5:
                    continue
                self.stats["profiles_unread"] += 1
                yield Candidate(
                    name=login, headline="(profile not read yet - open the profile link)",
                    location=f"{area} (GitHub location search)", distance_miles=d,
                    experience_years=None,
                    experience_source=(f"account at least {min_exp:g} years old (search filter)" if min_exp else ""),
                    email_source="profile not read yet", profile_url=f"https://github.com/{login}",
                    kind="candidate", provider="github (quick list)", profile_read=False, match_score=0.5,
                    match_reason=f"GitHub language search ({' / '.join(langs[:2])}); profile not read yet")


class JobAdContactsProvider:
    """Recruiting contacts employers published themselves (job ads + company careers/contact pages)."""
    name = "job_ads"
    label = "Hiring contacts published by employers"

    def __init__(self, job_sources: list[str], companies: list[str] = (), delay: float = 1.0,
                 company_pages: bool = True, max_company_sites: int = 40):
        self.companies = list(companies)
        ats = [a for a in ("greenhouse", "lever", "ashby", "workable", "smartrecruiters", "recruitee")
               if any(c.startswith(a + ":") for c in self.companies)
               or (a in ("greenhouse", "lever", "ashby") and any(":" not in c for c in self.companies))]
        self.job_sources = list(job_sources) + [a for a in ats if a not in job_sources]
        self.delay = delay
        self.company_pages = company_pages
        self.max_company_sites = max_company_sites
        self.notes: list[str] = []
        self.stats = {"ads_checked": 0, "ad_emails": 0, "company_sites_checked": 0, "company_emails": 0}

    def search(self, title: str, place: Place, radius_miles: float, geocoder: PlaceGeocoder,
               progress: Callable[[str], None] = lambda m: None) -> Iterator[Candidate]:
        from ..core import search as job_search
        from ..presets import DOMAINS
        locations = [t for t in location_variants(place, 8) if t]
        if place.region and place.region not in locations:
            locations.append(place.region)
        progress(f"Searching job ads for '{title}' near {place.town} ({len(self.job_sources)} sources)…")
        res = job_search(self.job_sources, keywords=[title] if title else [], locations=locations,
                         companies=self.companies, max_results=0, delay=self.delay, remote_uk=False)
        for r in res.sources:
            if r.status != "ok" and r.message:
                self.notes.append(f"{r.name}: {r.message}")
        self.stats["ads_checked"] = len(res.jobs)
        seen: set[str] = set()
        company_domains: dict[str, tuple[str, str]] = {}  # domain -> (company, location)
        for job in res.jobs:
            coords = geocoder.geocode(job.location)
            dist = round(haversine_miles(place.lat, place.lon, *coords), 1) if coords else None
            if dist is not None and dist > radius_miles:
                continue
            src = job.source
            if src in DOMAINS:
                company_domains.setdefault(DOMAINS[src], (job.company, job.location))
            if job.contact_email and job.contact_email.lower() not in seen:
                seen.add(job.contact_email.lower())
                dom = job.contact_email.split("@")[-1]
                if not any(x in dom for x in ("gmail.", "hotmail.", "outlook.", "yahoo.", "recruitee.com")):
                    company_domains.setdefault(dom, (job.company, job.location))
                self.stats["ad_emails"] += 1
                yield Candidate(
                    name="", headline=f"Hiring contact for: {job.title}", company=job.company, location=job.location,
                    distance_miles=dist, email=job.contact_email, phone=job.contact_phone or "",
                    email_source=f"published by the employer in the job ad {job.url}",
                    profile_url=job.url, kind="hiring contact", provider=f"job ad ({job.source})")
        if self.company_pages and company_domains:
            finder = SiteContactFinder(HttpClient(min_interval=1.0, timeout=12, retries=1))
            doms = list(company_domains.items())[: self.max_company_sites]
            progress(f"Checking {len(doms)} company careers/contact pages for published recruiting emails…")
            with ThreadPoolExecutor(max_workers=8) as ex:
                results = list(ex.map(lambda d: finder.company(d[0]), doms))
            for (dom, (company, loc)), found in zip(doms, results):
                self.stats["company_sites_checked"] += 1
                for email, url in found:
                    if email in seen:
                        continue
                    seen.add(email)
                    self.stats["company_emails"] += 1
                    yield Candidate(
                        name="", headline="Recruiting contact (company website)", company=company, location=loc,
                        email=email, email_source=f"published on the company website {url}", profile_url=url,
                        kind="hiring contact", provider=f"company website ({dom})")
        if not self.stats["ad_emails"] and not self.stats["company_emails"]:
            self.notes.append(f"No published hiring emails found ({len(res.jobs)} ads checked).")
