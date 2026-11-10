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
from .enrich import text_contacts
from .geo import Place, PlaceGeocoder, far_uk_place, haversine_miles, looks_non_uk
from .matching import RoleMatcher, contract_type, find_place, looks_like_business, place_terms, years_from_text
from .models import Candidate

log = logging.getLogger(__name__)
_URL = re.compile(r"https?://[^\s<>\"')]+", re.I)
_SOCIAL = re.compile(r"linkedin\.com|twitter\.com|(^|//|\.)x\.com|facebook\.com|instagram\.com|bsky\.app|"
                     r"tiktok\.com|youtube\.com|threads\.net|linktr\.ee|ko-fi\.com|patreon\.com|gravatar\.com|"
                     r"/@[A-Za-z0-9_]+$", re.I)


def strip_html(s: str) -> str:
    s = re.sub(r"<br\s*/?>|</p>", " \n", s or "", flags=re.I)
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


_FEDI = re.compile(r"(?<![\w/@.])@([A-Za-z0-9_]{1,30})@((?:[a-z0-9-]+\.)+[a-z]{2,})\b", re.I)
_BSKY_HANDLE = re.compile(r"(?<![\w/@.])@?([a-z0-9-]{2,40}\.bsky\.social)\b", re.I)
_UK_SLD = {"co", "org", "me", "ltd", "plc", "net", "ac", "gov", "nhs", "sch"}


# clear signs a profile is UK-based even when it names no town (used only for the optional 'UK, town not stated' rows)
UK_SIGNAL = re.compile(r"\b(uk|u\.k\.|united kingdom|britain|british|england|scotland|wales|cymru|northern ireland|"
                       r"nhs|nmc|hcpc|cqc|sra|cilex|niceic|gas safe|cscs|acca|icaew|cima|aat|ofsted|pgce|qts)\b|🇬🇧|"
                       r"🏴", re.I)
# signs someone with UK ties actually lives abroad (then they are not shown as 'UK, no nearby town')
ABROAD = re.compile(r"\b(exile|expat|ex-pat|immigrant|emigrated|living abroad|usa|u\.s\.a|"
                    r"america|canada|australia|new zealand|(?<!northern )ireland|france|paris|germany|berlin|spain|"
                    r"andaluc\w*|madrid|barcelona|portugal|lisbon|italy|netherlands|amsterdam|belgium|cyprus|greece|"
                    r"sweden|norway|denmark|india|nigeria|ghana|kenya|south africa|dubai|uae|qatar|singapore|"
                    r"hong kong|japan|china|brazil|mexico)\b|🇺🇸|🇨🇦|🇦🇺|🇳🇿|🇮🇪|🇫🇷|🇩🇪|🇪🇸|🇵🇹|🇮🇹|🇳🇱|🇧🇪|🇨🇾|🇬🇷|🇮🇳|🇳🇬", re.I)
# explicit places outside the UK that a UK town name elsewhere in the bio must not override
# ('A native of Meath … Leeds United fan', 'apprentice electrician in Portland Oregon')
NON_UK_PLACE = re.compile(r"\b(meath|westmeath|dublin|cork|galway|limerick|wexford|wicklow|kildare|louth|donegal|mayo|"
                          r"sligo|tipperary|waterford|kilkenny|offaly|laois|carlow|longford|cavan|monaghan|roscommon|"
                          r"leitrim|republic of ireland|oregon|portland,? (or|oregon|maine)|seattle|chicago|boston,? ma|"
                          r"philadelphia|florida|arizona|colorado|michigan|virginia|carolina|minnesota|wisconsin|"
                          r"pennsylvania|massachusetts|new jersey|toronto|ontario|vancouver|quebec|montreal|sydney|"
                          r"melbourne|brisbane|perth,? (wa|western australia)|auckland|wellington,? nz|dominican|"
                          r"\bdr\b(?=\W*🇩🇴))\b", re.I)
_UK_FLAG = "🇬🇧"
# '<Town> United fan' says nothing about where someone lives
_CLUB = re.compile(r"\b[A-Z][a-z]+(?:\s[A-Z][a-z]+)?\s+((united|utd|fc|afc|rovers|rhinos|wanderers|athletic|albion|"
                   r"hotspur|celtic|rangers|city fc)\b(\s+(fan|fanatic|supporter|season ticket holder)s?)?|"
                   r"(fan|fanatic|supporter|season ticket holder)s?\b)", re.I)


def abroad_signal(text: str) -> bool:
    t = text or ""
    lives_flag = re.search(r"(live|living|based|reside|residing) in\W{0,3}([🇦-🇿]{2})", t)
    if lives_flag and lives_flag.group(2) != _UK_FLAG:
        return True
    return bool(NON_UK_PLACE.search(t))


# Mastodon servers for one city/area: an account there with no stated town is taken to be from that area
SERVER_TOWN = {"mcr.wtf": "Manchester", "glasgow.social": "Glasgow", "newforest.social": "New Forest",
               "mastodon.iow.social": "Isle of Wight"}
UK_SERVERS = {"mastodonapp.uk", "mastodon.scot", "toot.wales", "mastodon.me.uk", "mcr.wtf", "glasgow.social",
              "newforest.social", "mastodon.iow.social"}


def cross_links(text: str) -> list[str]:
    """Profile links a person wrote in their own bio: '@jo@mastodon.social' and 'jo.bsky.social' handles.
    Used only to merge the same person's rows across sites (and to show their other profiles)."""
    out = [f"https://{inst.lower()}/@{user}" for user, inst in _FEDI.findall(text or "")]
    out += [f"https://bsky.app/profile/{h.lower()}" for h in _BSKY_HANDLE.findall(text or "")]
    return list(dict.fromkeys(out))


def handle_site(handle: str) -> str:
    """A Bluesky handle that is a person's own apex domain (e.g. 'janesmith.co.uk') is a website they control
    (Bluesky verifies it via DNS/HTTPS), so it can be checked for contact details they published there."""
    h = (handle or "").lower().strip(".")
    labels = h.split(".")
    if len(labels) < 2 or h.endswith((".bsky.social", ".brid.gy", ".bsky.team")) or "bsky" in labels:
        return ""
    if len(labels) == 2 or (len(labels) == 3 and labels[-1] == "uk" and labels[1] in _UK_SLD):
        return f"https://{h}"
    return ""


def own_site(urls: list[str]) -> str:
    for u in urls:
        u = u.rstrip(".,;")
        if not _SOCIAL.search(u) and "github.com" not in u.lower():
            return u
    return ""


_FAMILIES = ("nurse", "carer", "teacher", "solicitor", "accountant", "chef", "electrician", "plumber", "driver")


def _family(core: str) -> str:
    """The one-word job family of a longer title: 'practice nurse' -> 'nurse' ('' when the title is the family)."""
    words = (core or "").split()
    return next((f for f in _FAMILIES if len(words) > 1 and words[-1] in (f, f + "s")), "")


class _BioProvider:
    name = "bio"
    label = ""
    pages = 2

    max_queries = 8

    def __init__(self, fetch_websites: bool = True, site_http: Optional[HttpClient] = None,
                 max_queries: Optional[int] = None, uk_unknown: bool = False, decode_obfuscated: bool = True):
        self.fetch_websites = fetch_websites
        self.decode_obfuscated = decode_obfuscated
        self.uk_unknown = uk_unknown
        if max_queries is not None:
            self.max_queries = max_queries
        self.notes: list[str] = []
        self.stats = {"searches": 0, "profiles_seen": 0, "job_matches": 0, "in_area": 0, "uk_town_not_stated": 0,
                      "websites_checked": 0, "website_emails": 0}
        self.cache = DiskCache(f"{self.name}_search", 86400)
        self._site_http = site_http

    def extra_people(self, matcher: RoleMatcher, progress) -> dict[str, dict]:
        return {}

    # subclasses return dicts: {id, name, handle, url, bio, location, links[]}
    def _search(self, q: str, page: int, state: dict) -> list[dict]:
        raise NotImplementedError

    n_terms, n_areas = 4, 1

    def queries(self, matcher: RoleMatcher, place: Place) -> list[str]:
        """'<job> <town>' for the main town and a few nearby areas, then the job (and synonyms) on its own."""
        terms = matcher.search_terms(max(4, self.n_terms))
        areas = [place.town] + (["London"] if (place.region or "").lower() == "london" else [])
        areas += list(place.nearby_towns or [])[: max(0, self.n_areas - 1)]
        areas = [re.sub(r",.*$", "", a) for a in dict.fromkeys(a for a in areas if a)][: max(1, self.n_areas + 1)]
        if getattr(place, "nationwide", False):
            areas = ["UK"]   # a whole-UK search: the job + 'UK', then the job on its own (UK location checked later)
        qs = [f"{t} {a}" for t in terms[:2] for a in areas[:2]] + terms[:self.n_terms]
        qs += [f"{t} {a}" for a in areas[2:] for t in terms[:1]] + [f"{t} {a}" for t in terms[2:3] for a in areas[:1]]
        # the wider job family near them ('nurse Leeds' finds the practice nurses who just write 'nurse, Leeds')
        fam = _family(matcher.core)
        if fam:
            qs.insert(min(len(qs), 3), f"{fam} {areas[0]}")
        if matcher.contract == "temporary":
            # locum / bank work is how temporary staff describe themselves: search those words first
            lq = [f"locum {matcher.core}", f"bank {fam or matcher.core}", f"locum {fam or matcher.core} {areas[0]}"]
            qs = lq + qs
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
            m = matcher.match(_URL.sub(" ", p.get("bio") or ""), p.get("location"))  # not words inside links
            if not m.score:
                # the job only in the display name counts when written as a title ('Jo Smith | Nurse', 'Sam (chef)'),
                # not as a surname ('Peter Cook', 'Ann Mason')
                parts = re.split(r"\s*[|•·,(/\-–—:]\s*", p.get("name") or "")
                m = matcher.match(" ".join(parts[1:])) if len(parts) > 1 else m
            if not m.score:
                continue
            self.stats["job_matches"] += 1
            loc = (p.get("location") or "").strip()
            # where they live comes from their location field and bio - never from words in their display name
            # ('Guthrie Glasgow'), except a title-style tail ('Jo Smith | Nurse | Leeds'); football clubs don't count
            name_parts = re.split(r"\s*[|•·,(/\-–—:]\s*", p.get("name") or "")
            bio_place = _CLUB.sub(" ", _URL.sub(" ", " \n".join([" ".join(name_parts[1:]), p.get("bio") or ""])))
            area = find_place(_CLUB.sub(" ", loc), pterms)
            from_bio = False
            if not area:
                area = find_place(bio_place, pterms)
                from_bio = bool(area)
            if looks_non_uk(loc or text) and not re.search(r"\b(uk|united kingdom|england|scotland|wales)\b", text, re.I):
                continue
            if abroad_signal(loc) or (not (area and not from_bio) and abroad_signal(p.get("bio") or "")):
                continue   # an explicit non-UK home (Meath, Portland Oregon, 'I live in 🇩🇴') beats a UK word in the bio
            dist = None
            uk_only = False
            if not area and p.get("home_town"):
                co = geocoder.geocode(p["home_town"])   # e.g. an account on mcr.wtf (Manchester's server)
                if co and haversine_miles(place.lat, place.lon, *co) <= radius_miles + 3:
                    area = p["home_town"]
            if area:
                co = geocoder.geocode(area)
                if co:
                    dist = round(haversine_miles(place.lat, place.lon, *co), 1)
                # area names are district-level, so allow a little slack beyond the radius
                if dist is not None and dist > radius_miles + 3:
                    continue
                if getattr(place, "nationwide", False):
                    dist = None       # a whole-UK search: distance from the middle of the country means nothing
            elif getattr(place, "nationwide", False) and (p.get("uk_hint") or UK_SIGNAL.search(
                    _URL.sub(" ", re.sub(r"(?i)new england", "", text)))) and not ABROAD.search(_URL.sub(" ", text)):
                area = "UK"           # whole-UK search: 'UK-based' is exactly what was asked for
            elif self.uk_unknown and (p.get("uk_hint") or UK_SIGNAL.search(_URL.sub(" ", re.sub(r"(?i)new england",
                                                                                                "", text)))):
                if far_uk_place(text, place, radius_miles, geocoder) or ABROAD.search(_URL.sub(" ", text)):
                    continue     # says they're in another UK town/nation (e.g. 'Cardiff', 'Preston-based') or abroad
                uk_only = True   # clearly UK-based but names no town near the postcode: kept, ranked lower
            elif not include_unknown_distance:
                continue
            yrs = years_from_text(p.get("bio") or "")
            if yrs is not None and ((min_exp and yrs < min_exp) or (max_exp and yrs > max_exp)):
                continue
            self.stats["uk_town_not_stated" if uk_only else "in_area"] += 1
            site = own_site(p.get("links") or [])
            biz = looks_like_business(p.get("name") or "", p.get("bio") or "")
            if biz:
                self.stats["business_accounts"] = self.stats.get("business_accounts", 0) + 1
            cands.append(Candidate(
                name=p.get("name") or p.get("handle") or "", headline=re.sub(r"\s+", " ", p.get("bio") or "")[:300],
                location=(loc or (area or "")) if not uk_only else (f"{loc} (UK - no nearby town mentioned)" if loc
                                                                     else "UK - no nearby town mentioned"),
                distance_miles=dist, experience_years=yrs,
                experience_source="stated in their bio" if yrs is not None else "",
                profile_url=p["url"], website=site,
                linkedin_url=next((u for u in p.get("links") or [] if "linkedin.com/in/" in u.lower()), ""),
                kind="business" if biz else "candidate", provider=self.name, profile_read=True,
                match_score=m.score, match_reason=m.reason + (f" (found via {p['via']})" if p.get("via") else ""),
                contract=contract_type(p.get("bio") or ""), last_active=p.get("last_active") or "",
                other_profiles=" ".join(u for u in (p.get("links") or []) if u != site)[:500]))
            # contact details they wrote in their OWN bio / profile fields (plain or 'name [at] domain' style)
            emails, phones = text_contacts(_URL.sub(" ", p.get("bio") or ""), self.decode_obfuscated)
            if emails:
                cands[-1].email, cands[-1].contact_source = emails[0], p["url"]
                cands[-1].email_source = f"published in their own {self.label} profile {p['url']}"
                self.stats["bio_emails"] = self.stats.get("bio_emails", 0) + 1
            if phones:
                cands[-1].phone = phones[0]
                cands[-1].contact_source = cands[-1].contact_source or p["url"]
        if self.fetch_websites:
            with_site = [c for c in cands if c.website and not c.email]
            if with_site:
                progress(f"{self.label}: checking {len(with_site)} personal websites for self-published contacts…")
                sites = SiteContactFinder(self._site_http or HttpClient(min_interval=1.0, timeout=12, retries=1))
                with ThreadPoolExecutor(max_workers=6) as ex:
                    for c, res in zip(with_site, ex.map(lambda c: sites.personal(c.website), with_site)):
                        self.stats["websites_checked"] += 1
                        if res["emails"] and not c.email:
                            c.email, c.contact_source = res["emails"][0], res["source_url"] or c.website
                            c.email_source = f"self-published on personal website {res['source_url']}"
                            self.stats["website_emails"] += 1
                        if res["phones"] and not c.phone:
                            c.phone = res["phones"][0]
                        c.linkedin_url = c.linkedin_url or (res["linkedin"] or "")
                        c.twitter_url = c.twitter_url or (res["twitter"] or "")
        for c in cands:
            if not c.email:
                c.email_source = ("none self-published on their website" if c.website and self.fetch_websites else
                                  f"none in their {self.label} bio - contact them via their profile")
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
        links = [u.rstrip(".,;") for u in _URL.findall(bio)] + cross_links(bio)
        if site := handle_site(a.handle):
            links.append(site)  # their own verified domain, after any site they linked in the bio
        return {"id": a.did, "name": a.display_name or a.handle, "handle": a.handle,
                "url": f"https://bsky.app/profile/{a.handle}", "bio": bio, "location": "",
                "links": list(dict.fromkeys(links)), "uk_hint": a.handle.lower().endswith(".uk")}

    max_feeds = 3
    n_terms, n_areas, max_queries = 5, 4, 14

    def extra_people(self, matcher: RoleMatcher, progress) -> dict[str, dict]:
        people = self.starter_pack_people(matcher, progress)
        for k, v in self.feed_people(matcher, progress).items():
            people.setdefault(k, v)
        return people

    def feed_people(self, matcher: RoleMatcher, progress) -> dict[str, dict]:
        """Recent posters in public Bluesky *custom feeds* for this profession (e.g. 'NurseSky', 'Legal Sky').
        Their profiles are then read and, like everyone else, kept only if their OWN bio mentions the job."""
        key = f"feeds|{matcher.core}"
        hit = self.cache.get(key)
        if hit is not None:
            return hit
        people: dict[str, dict] = {}
        try:
            c = self.client()
            feeds, seen = [], set()
            for term in matcher.search_terms(2):
                progress(f"Bluesky: looking for '{term}' custom feeds…")
                time.sleep(0.3)
                res = c.app.bsky.unspecced.get_popular_feed_generators({"query": term, "limit": 15})
                for fg in res.feeds or []:
                    text = f"{fg.display_name or ''} {getattr(fg, 'description', '') or ''}"
                    if fg.uri not in seen and matcher.match(text).score:
                        seen.add(fg.uri)
                        feeds.append(fg)
            feeds = feeds[: self.max_feeds]
            authors: dict[str, str] = {}
            for fg in feeds:
                cursor = None
                for _ in range(2):
                    time.sleep(0.3)
                    params = {"feed": fg.uri, "limit": 100}
                    if cursor:
                        params["cursor"] = cursor
                    page = c.app.bsky.feed.get_feed(params)
                    for item in page.feed or []:
                        post = item.post
                        when = (getattr(post, "indexed_at", "") or "")[:10]
                        authors[post.author.did] = max(authors.get(post.author.did, ""), when)
                    cursor = getattr(page, "cursor", None)
                    if not cursor:
                        break
            dids = list(authors)[:400]
            progress(f"Bluesky: reading {len(dids)} profiles from {len(feeds)} profession feeds…")
            for i in range(0, len(dids), 25):
                time.sleep(0.3)
                res = c.app.bsky.actor.get_profiles({"actors": dids[i:i + 25]})
                for prof in res.profiles or []:
                    d = self._person(prof)
                    if d:
                        d["last_active"] = authors.get(prof.did, "")
                        d["via"] = "a Bluesky profession feed"
                        people.setdefault(d["id"], d)
            self.stats["profession_feeds"] = len(feeds)
        except Exception as e:  # noqa: BLE001
            log.warning("bluesky feeds failed: %s", e)
            self.notes.append(f"profession feeds: {str(e)[:100]}")
            return people
        self.cache.set(key, people)
        return people

    def starter_pack_people(self, matcher: RoleMatcher, progress) -> dict[str, dict]:
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
                            d["via"] = "a Bluesky starter pack"
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


UK_HINT = re.compile(r"\b(uk|united kingdom|britain|british|england|scotland|scottish|wales|welsh|cymru|"
                     r"northern ireland|london|manchester|glasgow|edinburgh|liverpool|leeds|birmingham|bristol|"
                     r"newcastle|sheffield|nottingham|yorkshire|lancashire|kent|sussex|devon|cornwall|isle of wight|"
                     r"new forest|cardiff|belfast)\b|\.uk$", re.I)


def uk_servers(http: Optional[HttpClient] = None, cache: Optional[DiskCache] = None, limit: int = 10) -> list[str]:
    """UK regional Mastodon servers listed on joinmastodon.org's public server directory (api.joinmastodon.org,
    the same keyless list the joinmastodon.org website uses). Cached for a week; [] on any error."""
    cache = cache or DiskCache("mastodon_servers", 7 * 86400)
    hit = cache.get("uk")
    if hit is not None:
        return hit[:limit]
    try:
        http = http or HttpClient(min_interval=1.0, timeout=20, retries=1)
        rows = http.get("https://api.joinmastodon.org/servers", params={"category": "regional"}).json()
    except Exception as e:  # noqa: BLE001
        log.info("joinmastodon server list failed: %s", e)
        return []
    out = [r["domain"] for r in sorted(rows, key=lambda r: -(r.get("total_users") or 0))
           if r.get("domain") and (r.get("region") in ("europe", "", None))
           and UK_HINT.search(f"{r.get('domain')} {r.get('description') or ''}")
           and not re.search(r"\b(ireland|irish)\b", r.get("description") or "", re.I)]
    cache.set("uk", out)
    return out[:limit]


def _acct_dict(a: dict, server: str) -> Optional[dict]:
    """A Mastodon account -> our profile dict, or None if they didn't opt in to discovery (or are a bot/group)."""
    if not a.get("discoverable") or a.get("bot") or a.get("noindex") or a.get("group"):
        return None
    fields = a.get("fields") or []
    loc = next((strip_html(f.get("value", "")) for f in fields
                if re.search(r"location|based|where|city|town", f.get("name", ""), re.I)), "")
    links = []
    for f in sorted(fields, key=lambda f: not f.get("verified_at")):
        links += re.findall(r'href="([^"]+)"', f.get("value", ""))
    links += re.findall(r'href="([^"]+)"', a.get("note") or "")
    links = [u for u in dict.fromkeys(links) if "/tags/" not in u and not re.search(r"/@\w+$", u)
             or u.startswith("https://bsky.app")]
    acct = a.get("acct") or ""
    if "@" not in acct:
        acct = f"{acct}@{server}"
    extra = " ".join(strip_html(f.get("value", "")) for f in fields)
    bio = strip_html(a.get("note") or "") + (" | " + extra if extra else "")
    links += [u for u in cross_links(bio) if u.split("/@")[0] not in (a.get("url") or "")]
    last = a.get("last_status_at") or ""
    home = acct.rsplit("@", 1)[-1].lower()
    return {"id": acct.lower(), "name": a.get("display_name") or acct, "handle": acct, "url": a.get("url") or "",
            "bio": bio, "location": loc, "links": list(dict.fromkeys(links)),
            "last_active": str(last)[:10] if last else "",
            "home_town": SERVER_TOWN.get(home, ""), "uk_hint": home in UK_SERVERS or home.endswith(".uk")}


class MastodonProvider(_BioProvider):
    name = "mastodon"
    label = "Mastodon"
    pages = 1
    n_terms, n_areas, max_queries = 4, 2, 8
    # big general servers + UK-focused ones (all allow unauthenticated account search; robots.txt allows /api/v2).
    # Each server's search also covers the remote accounts it knows, so more servers = more of the fediverse.
    DEFAULT_SERVERS = ("mastodon.social", "mastodonapp.uk", "mastodon.scot", "toot.wales", "mstdn.social",
                       "mastodon.online", "mastodon.world", "mas.to", "universeodon.com", "mastodon.me.uk",
                       "mcr.wtf", "glasgow.social")
    HASHTAG_SERVERS = ("mastodon.social", "mastodonapp.uk", "mastodon.online")

    def __init__(self, *a, servers: Optional[tuple[str, ...]] = None, factory=None, discover_servers: bool = True,
                 hashtags: bool = True, **kw):
        super().__init__(*a, **kw)
        self.servers = list(servers or self.DEFAULT_SERVERS)
        self._discover = discover_servers and servers is None and factory is None
        self.hashtags = hashtags
        self._factory = factory
        self._apis: dict = {}

    def search(self, *a, **kw):
        if self._discover:
            extra = [d for d in uk_servers() if d not in self.servers]
            self.servers += extra
            self.stats["servers"] = len(self.servers)
            self._discover = False
        yield from super().search(*a, **kw)

    def api(self, server: str):
        if server not in self._apis:
            if self._factory:
                self._apis[server] = self._factory(server)
            else:
                from mastodon import Mastodon  # https://github.com/halcy/Mastodon.py
                self._apis[server] = Mastodon(api_base_url=f"https://{server}", request_timeout=20,
                                              version_check_mode="none")
        return self._apis[server]

    def _search_server(self, server: str, q: str) -> tuple[list[dict], bool, str]:
        try:
            # unauthenticated search can't paginate, so one page (top results) per query and server
            res = self.api(server).search_v2(q, result_type="accounts", resolve=False)
        except Exception as e:  # noqa: BLE001
            log.info("mastodon %s search failed: %s", server, e)
            return [], False, f"{server}: {str(e)[:100]}"
        accts = res.get("accounts") or []
        return [d for d in (_acct_dict(a, server) for a in accts) if d], len(accts) >= 20, ""

    def _search(self, q: str, page: int, state: dict) -> list[dict]:
        out: list[dict] = []
        more = False
        # one request per server, in parallel (different hosts), so many servers stay quick and polite
        with ThreadPoolExecutor(max_workers=6) as ex:
            for rows, m, err in ex.map(lambda sv: self._search_server(sv, q), self.servers):
                out += rows
                more = more or m
                if err:
                    state.setdefault("errors", []).append(err)
        state["more"] = more
        return out

    def extra_people(self, matcher: RoleMatcher, progress) -> dict[str, dict]:
        """Authors of recent public posts under the job's hashtag (e.g. #nurse, #electrician, #solicitor) on a few
        big servers. Same opt-in rules (discoverable, not noindex, not a bot) and the bio must still match."""
        if not self.hashtags:
            return {}
        tags = list(dict.fromkeys(re.sub(r"[^a-z0-9]", "", t.lower()) for t in matcher.search_terms(3)))[:3]
        key = f"tags|{'|'.join(tags)}"
        hit = self.cache.get(key)
        if hit is not None:
            return hit
        people: dict[str, dict] = {}
        for server in self.HASHTAG_SERVERS:
            for tag in tags:
                if not tag:
                    continue
                progress(f"Mastodon: reading #{tag} posts on {server}…")
                max_id = None
                for _ in range(2):
                    time.sleep(0.3)
                    try:
                        kw = {"limit": 40}
                        if max_id:
                            kw["max_id"] = max_id
                        posts = self.api(server).timeline_hashtag(tag, **kw)
                    except Exception as e:  # noqa: BLE001
                        log.info("mastodon hashtag %s on %s failed: %s", tag, server, e)
                        break
                    for st in posts or []:
                        d = _acct_dict(st.get("account") or {}, server)
                        if d:
                            d["via"] = f"#{tag} posts"
                            people.setdefault(d["id"], d)
                    if not posts or len(posts) < 40:
                        break
                    max_id = posts[-1].get("id")
        self.stats["hashtag_authors"] = len(people)
        self.cache.set(key, people)
        return people
