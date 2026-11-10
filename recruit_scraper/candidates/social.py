"""People who describe their own job in a public social-network bio (works for non-tech jobs too).

BlueskyProvider
    Bluesky's public AppView (public.api.bsky.app) via the open-source `atproto` SDK
    (https://github.com/MarshalX/atproto). No login or key. Bluesky's robots.txt for that host says "Crawling the
    public parts of the API is allowed"; the Developer Guidelines only forbid spam/automated interactions, which this
    never does (read-only). Accounts that opted out of being shown to logged-out visitors
    (`!no-unauthenticated` label) are skipped.
MastodonProvider
    Mastodon's public account search (unauthenticated, no remote resolving) via the open-source `Mastodon.py`
    library (https://github.com/halcy/Mastodon.py). Only accounts that opted in to discovery (`discoverable`) and
    did not opt out of search-engine indexing (`noindex`) are used; bots are skipped. robots.txt allows /api/v2.

For both: only people whose OWN bio/profile fields mention the job (or a common synonym) AND a place within the
radius are kept. Emails are not lifted from bios; if a person links their own website, the existing polite
website check (robots.txt + noindex honoured) records contact details they self-published there.
"""
from __future__ import annotations

import html
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterator, Optional

from ..http import HttpClient
from .cache import DiskCache
from .contacts import SiteContactFinder
from .geo import Place, PlaceGeocoder, haversine_miles, looks_non_uk
from .matching import RoleMatcher, contract_type, find_place, place_terms, years_from_text
from .models import Candidate

log = logging.getLogger(__name__)
_URL = re.compile(r"https?://[^\s<>\"')]+", re.I)
_SOCIAL = re.compile(r"linkedin\.com|twitter\.com|(^|//|\.)x\.com|facebook\.com|instagram\.com|bsky\.app|"
                     r"tiktok\.com|youtube\.com|threads\.net|linktr\.ee|ko-fi\.com|patreon\.com|gravatar\.com|"
                     r"/@[A-Za-z0-9_]+$", re.I)


def strip_html(s: str) -> str:
    s = re.sub(r"<br\s*/?>|</p>", " \n", s or "", flags=re.I)
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def own_site(urls: list[str]) -> str:
    for u in urls:
        u = u.rstrip(".,;")
        if not _SOCIAL.search(u) and "github.com" not in u.lower():
            return u
    return ""


class _BioProvider:
    name = "bio"
    label = ""
    pages = 2

    def __init__(self, fetch_websites: bool = True, site_http: Optional[HttpClient] = None, max_queries: int = 8):
        self.fetch_websites = fetch_websites
        self.max_queries = max_queries
        self.notes: list[str] = []
        self.stats = {"searches": 0, "profiles_seen": 0, "job_matches": 0, "in_area": 0, "websites_checked": 0,
                      "website_emails": 0}
        self.cache = DiskCache(f"{self.name}_search", 86400)
        self._site_http = site_http

    def extra_people(self, matcher: RoleMatcher, progress) -> dict[str, dict]:
        return {}

    # subclasses return dicts: {id, name, handle, url, bio, location, links[]}
    def _search(self, q: str, page: int, state: dict) -> list[dict]:
        raise NotImplementedError

    def queries(self, matcher: RoleMatcher, place: Place) -> list[str]:
        terms = matcher.search_terms(4)
        areas = [place.town] + (["London"] if (place.region or "").lower() == "london" else [])
        areas = [re.sub(r",.*$", "", a) for a in dict.fromkeys(a for a in areas if a)]
        qs = [f"{t} {a}" for t in terms[:2] for a in areas] + terms
        return list(dict.fromkeys(qs))[: self.max_queries]

    def _collect(self, qs: list[str], progress) -> dict[str, dict]:
        people: dict[str, dict] = {}
        for q in qs:
            state: dict = {}
            for page in range(1, self.pages + 1):
                key = f"{q}|{page}"
                hit = self.cache.get(key)
                if hit is None:
                    progress(f"{self.label}: searching '{q}'" + (f" (page {page})" if page > 1 else "") + "…")
                    try:
                        rows = self._search(q, page, state)
                    except Exception as e:  # noqa: BLE001
                        log.warning("%s search failed: %s", self.name, e)
                        self.notes.append(f"search '{q}' failed: {str(e)[:120]}")
                        break
                    self.stats["searches"] += 1
                    self.cache.set(key, {"rows": rows, "more": bool(state.get("more")),
                                         "cursor": state.get("cursor")})
                else:
                    rows = hit.get("rows") or []
                    state["more"], state["cursor"] = hit.get("more"), hit.get("cursor")
                for r in rows:
                    people.setdefault(r["id"], r)
                if not rows or not state.get("more"):
                    break
        return people

    def search(self, title: str, place: Place, radius_miles: float, min_exp: Optional[float],
               max_exp: Optional[float], keywords: list[str], include_unknown_distance: bool,
               geocoder: PlaceGeocoder, progress: Callable[[str], None] = lambda m: None) -> Iterator[Candidate]:
        matcher = RoleMatcher(title, keywords)
        people = self._collect(self.queries(matcher, place), progress)
        for k, v in self.extra_people(matcher, progress).items():
            people.setdefault(k, v)
        self.stats["profiles_seen"] = len(people)
        pterms = place_terms(place)
        cands: list[Candidate] = []
        for p in people.values():
            text = " \n".join([p.get("name") or "", p.get("bio") or "", p.get("location") or ""])
            m = matcher.match(p.get("bio"), p.get("location"), p.get("name"))
            if not m.score:
                continue
            self.stats["job_matches"] += 1
            loc = (p.get("location") or "").strip()
            area = find_place(loc, pterms) or find_place(text, pterms)
            if looks_non_uk(loc or text) and not re.search(r"\b(uk|united kingdom|england|scotland|wales)\b", text, re.I):
                continue
            dist = None
            if area:
                co = geocoder.geocode(area)
                if co:
                    dist = round(haversine_miles(place.lat, place.lon, *co), 1)
                # area names are district-level, so allow a little slack beyond the radius
                if dist is not None and dist > radius_miles + 3:
                    continue
            elif not include_unknown_distance:
                continue
            yrs = years_from_text(p.get("bio") or "")
            if yrs is not None and ((min_exp and yrs < min_exp) or (max_exp and yrs > max_exp)):
                continue
            self.stats["in_area"] += 1
            site = own_site(p.get("links") or [])
            cands.append(Candidate(
                name=p.get("name") or p.get("handle") or "", headline=re.sub(r"\s+", " ", p.get("bio") or "")[:300],
                location=loc or (area or ""), distance_miles=dist, experience_years=yrs,
                experience_source="stated in their bio" if yrs is not None else "",
                profile_url=p["url"], website=site,
                linkedin_url=next((u for u in p.get("links") or [] if "linkedin.com/in/" in u.lower()), ""),
                kind="candidate", provider=self.name, profile_read=True,
                match_score=m.score, match_reason=m.reason, contract=contract_type(p.get("bio") or ""),
                other_profiles=" ".join(u for u in (p.get("links") or []) if u != site)[:500]))
        if self.fetch_websites:
            with_site = [c for c in cands if c.website]
            if with_site:
                progress(f"{self.label}: checking {len(with_site)} personal websites for self-published contacts…")
                sites = SiteContactFinder(self._site_http or HttpClient(min_interval=1.0, timeout=12, retries=1))
                with ThreadPoolExecutor(max_workers=6) as ex:
                    for c, res in zip(with_site, ex.map(lambda c: sites.personal(c.website), with_site)):
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
                c.email_source = ("none self-published on their website" if c.website and self.fetch_websites else
                                  f"not collected from {self.label} bios - contact them via their profile")
            yield c
        if people and not cands:
            self.notes.append(f"{len(people)} profiles checked; none both mention the job and an area near "
                              f"{place.town}.")


class BlueskyProvider(_BioProvider):
    name = "bluesky"
    label = "Bluesky"
    BASE = "https://public.api.bsky.app/xrpc"

    max_packs = 4

    def __init__(self, *a, client=None, **kw):
        super().__init__(*a, **kw)
        self._client = client

    def client(self):
        if self._client is None:
            from atproto import Client  # https://github.com/MarshalX/atproto
            self._client = Client(base_url=self.BASE)
        return self._client

    def _search(self, q: str, page: int, state: dict) -> list[dict]:
        if page > 1 and not state.get("cursor"):
            return []
        params = {"q": q, "limit": 100}
        if state.get("cursor"):
            params["cursor"] = state["cursor"]
        time.sleep(0.3)  # gentle on the public AppView
        res = self.client().app.bsky.actor.search_actors(params)
        state["cursor"] = getattr(res, "cursor", None)
        state["more"] = bool(state["cursor"])
        return [d for d in (self._person(a) for a in res.actors or []) if d]

    @staticmethod
    def _person(a) -> Optional[dict]:
        labels = [getattr(lb, "val", "") for lb in (getattr(a, "labels", None) or [])]
        if "!no-unauthenticated" in labels:
            return None  # they asked not to be shown to logged-out viewers
        bio = getattr(a, "description", None) or ""
        return {"id": a.did, "name": a.display_name or a.handle, "handle": a.handle,
                "url": f"https://bsky.app/profile/{a.handle}", "bio": bio, "location": "",
                "links": list(dict.fromkeys(u.rstrip(".,;") for u in _URL.findall(bio)))}

    def extra_people(self, matcher: RoleMatcher, progress) -> dict[str, dict]:
        """Members of public Bluesky *starter packs* made for this profession (e.g. 'UK nurses', 'Legal Bluesky')."""
        key = f"packs|{matcher.core}"
        hit = self.cache.get(key)
        if hit is not None:
            return hit
        people: dict[str, dict] = {}
        try:
            c = self.client()
            packs = []
            for term in matcher.search_terms(2):
                progress(f"Bluesky: looking for '{term}' starter packs…")
                time.sleep(0.3)
                res = c.app.bsky.graph.search_starter_packs({"q": term, "limit": 25})
                for sp in res.starter_packs or []:
                    rec = sp.record
                    name = f"{getattr(rec, 'name', '')} {getattr(rec, 'description', '') or ''}"
                    if matcher.match(name).score and sp.uri not in [p.uri for _, p in packs]:
                        uk = 1 if re.search(r"\b(uk|britain|british|england|scotland|wales|nhs|london)\b", name, re.I) else 0
                        packs.append((uk, sp))
            packs = [sp for _, sp in sorted(packs, key=lambda x: -x[0])][: self.max_packs]
            for sp in packs:
                time.sleep(0.3)
                full = c.app.bsky.graph.get_starter_pack({"starter_pack": sp.uri}).starter_pack
                lst = getattr(full, "list", None)
                if not lst:
                    continue
                cursor = None
                for _ in range(2):
                    time.sleep(0.3)
                    params = {"list": lst.uri, "limit": 100}
                    if cursor:
                        params["cursor"] = cursor
                    page = c.app.bsky.graph.get_list(params)
                    for it in page.items or []:
                        d = self._person(it.subject)
                        if d:
                            people.setdefault(d["id"], d)
                    cursor = getattr(page, "cursor", None)
                    if not cursor:
                        break
            self.stats["starter_packs"] = len(packs)
        except Exception as e:  # noqa: BLE001
            log.warning("bluesky starter packs failed: %s", e)
            self.notes.append(f"starter packs: {str(e)[:100]}")
            return people
        self.cache.set(key, people)
        return people


class MastodonProvider(_BioProvider):
    name = "mastodon"
    label = "Mastodon"
    pages = 1
    # big general servers + UK-focused ones (all allow unauthenticated account search; robots.txt allows /api/v2)
    DEFAULT_SERVERS = ("mastodon.social", "mastodonapp.uk", "mastodon.scot", "toot.wales", "mstdn.social")

    def __init__(self, *a, servers: tuple[str, ...] = DEFAULT_SERVERS, factory=None, **kw):
        super().__init__(*a, **kw)
        self.servers = list(servers)
        self._factory = factory
        self._apis: dict = {}

    def api(self, server: str):
        if server not in self._apis:
            if self._factory:
                self._apis[server] = self._factory(server)
            else:
                from mastodon import Mastodon  # https://github.com/halcy/Mastodon.py
                self._apis[server] = Mastodon(api_base_url=f"https://{server}", request_timeout=20,
                                              version_check_mode="none")
        return self._apis[server]

    def _search(self, q: str, page: int, state: dict) -> list[dict]:
        out: list[dict] = []
        more = False
        for server in self.servers:
            time.sleep(0.3)
            try:
                # unauthenticated search can't paginate, so one page (top results) per query and server
                res = self.api(server).search_v2(q, result_type="accounts", resolve=False)
            except Exception as e:  # noqa: BLE001
                log.info("mastodon %s search failed: %s", server, e)
                state.setdefault("errors", []).append(f"{server}: {str(e)[:100]}")
                continue
            accts = res.get("accounts") or []
            more = more or len(accts) >= 20
            for a in accts:
                if not a.get("discoverable") or a.get("bot") or a.get("noindex") or a.get("group"):
                    continue
                fields = a.get("fields") or []
                loc = next((strip_html(f.get("value", "")) for f in fields
                            if re.search(r"location|based|where|city|town", f.get("name", ""), re.I)), "")
                links = []
                for f in sorted(fields, key=lambda f: not f.get("verified_at")):
                    links += re.findall(r'href="([^"]+)"', f.get("value", ""))
                links += re.findall(r'href="([^"]+)"', a.get("note") or "")
                links = [u for u in dict.fromkeys(links) if "/tags/" not in u and not re.search(r"/@\w+$", u)
                         or u.startswith("https://bsky.app")]
                url = a.get("url") or ""
                acct = a.get("acct") or ""
                if "@" not in acct:
                    acct = f"{acct}@{server}"
                extra = " ".join(strip_html(f.get("value", "")) for f in fields)
                out.append({"id": acct.lower(), "name": a.get("display_name") or acct, "handle": acct, "url": url,
                            "bio": strip_html(a.get("note") or "") + (" | " + extra if extra else ""),
                            "location": loc, "links": links})
        state["more"] = more
        return out
