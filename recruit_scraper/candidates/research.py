"""Europe PMC 'Clinical researchers' source (OFF by default).

Healthcare people who co-authored recent research and published a correspondence email in their OWN author
affiliation (e.g. "School of Nursing, University of Manchester, UK. jo.bloggs@manchester.ac.uk"). Read through the
free Europe PMC REST API (https://europepmc.org/RestfulWebService; no key; EMBL-EBI terms of use), one or two
search requests per run, at least 10 s apart (the crawl delay in https://www.ebi.ac.uk/robots.txt), cached a day.

Rules:
* the email must be written in THAT author's own affiliation text - nothing is guessed or built from a pattern;
* the email must belong to that author (their surname or first name is in the address) - otherwise it is skipped,
  because a shared affiliation line can carry a co-author's address;
* the affiliation must name a town near the postcode;
* the source shown for every email is the article page.
These addresses were published for research correspondence, not for recruitment: UK GDPR (purpose / legitimate
interests) and PECR (consent or soft opt-in for unsolicited email) apply before contacting anyone.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from datetime import date
from typing import Callable, Iterator, Optional

import requests

from ..http import HttpClient
from ..utils import find_emails
from .cache import DiskCache
from .geo import Place, PlaceGeocoder, haversine_miles
from .matching import RoleMatcher
from .models import Candidate

log = logging.getLogger(__name__)

API = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
LABEL = "Clinical researcher (Europe PMC)"
RESEARCH_NOTE = ("Clinical researchers (Europe PMC): these emails were published in research articles for research "
                 "correspondence, not to be recruited. Before contacting anyone, check you have a lawful basis "
                 "(legitimate interests assessment) and PECR consent or soft opt-in for unsolicited email, tell them "
                 "where you found their address (the article link is shown) and honour opt-outs.")
_HEALTH = re.compile(r"\b(nurs\w*|midwi\w*|carer|care|caring|hca|health\w*|clinic\w*|medic\w*|doctor|gp|physician|"
                     r"physio\w*|therap\w*|pharmac\w*|dental|dentist|paramedic|radiograph\w*|sonograph\w*|"
                     r"psycholog\w*|psychiatr\w*|dietit\w*|occupational|social work\w*|support worker|surgeon|"
                     r"anaesthet\w*|podiatr\w*|optometr\w*|audiolog\w*|speech|counsell\w*|hospital|patient)\b", re.I)
_GENERIC = {"info", "enquiries", "enquiry", "office", "admin", "contact", "research", "editor", "editorial",
            "journal", "support", "help", "secretary", "general", "team"}


def is_health_role(title: str) -> bool:
    return bool(_HEALTH.search(title or ""))


def _ascii(s: str) -> str:
    return re.sub(r"[^a-z]", "", unicodedata.normalize("NFKD", (s or "").lower()).encode("ascii", "ignore").decode())


def owns_email(email: str, first: str, last: str) -> bool:
    """The address belongs to this author: their surname (or first name) appears in the local part."""
    local = email.split("@", 1)[0].lower()
    flat = _ascii(local)
    if not flat or flat in _GENERIC:
        return False
    surnames = [_ascii(p) for p in re.split(r"[\s\-']+", last or "") if len(_ascii(p)) >= 3]
    fn = _ascii((first or "").split()[0]) if first else ""
    return any(s in flat for s in surnames) or (len(fn) >= 3 and fn in flat)


def role_terms(matcher: RoleMatcher, n: int = 4) -> list[str]:
    """The job title and its exact synonyms (broader 'related' words only if there are none: a 'practice nurse'
    search must not pull every paper that mentions a nurse)."""
    from .matching import _ACRONYM
    out = [matcher.core]
    for s in matcher.synonyms + (matcher.related if not matcher.synonyms else []):
        if len(out) >= n:
            break
        if len(s) > 3 and s not in out and not _ACRONYM.match(s):
            out.append(s)
    return [o for o in out if o]


def build_query(matcher: RoleMatcher, towns: list[str], years: int = 5, today: Optional[date] = None) -> str:
    """The job must be in the article's title or abstract, an author affiliation must name a nearby town."""
    y = (today or date.today()).year
    words = [w for t in role_terms(matcher) for w in (t, t if t.endswith("s") else t + "s")]   # + plurals
    terms = " OR ".join(f'TITLE_ABS:"{t}"' for t in words)
    affs = " OR ".join(f'AFF:"{t}"' for t in towns)
    return f"({terms}) AND ({affs}) AND PUB_YEAR:[{y - years} TO {y}]"


def area_towns(place: Place, n: int = 5) -> list[str]:
    towns = [t for t in [place.town] + list(place.nearby_towns or []) if t]
    if (place.region or "").lower() == "london" or "london" in " ".join(towns).lower():
        towns.insert(0, "London")
    out: list[str] = []
    for t in towns:
        t = re.sub(r",.*", "", t).strip()
        if t and t.lower() not in (o.lower() for o in out):
            out.append(t)
    return out[:n]


class EuropePMCProvider:
    name = "europepmc"
    label = LABEL

    def __init__(self, http: Optional[HttpClient] = None, years: int = 5, page_size: int = 200, max_pages: int = 2):
        self.http = http or HttpClient(min_interval=10.0, timeout=60, retries=2, backoff=5)
        self.years, self.page_size, self.max_pages = years, page_size, max_pages
        self.notes: list[str] = []
        self.stats = {"requests": 0, "articles": 0, "authors_with_email": 0, "kept": 0, "not_their_email": 0,
                      "not_near": 0}
        self.cache = DiskCache("europepmc", 86400)

    def _page(self, query: str, cursor: str) -> dict:
        key = f"{query}|{cursor}|{self.page_size}"
        hit = self.cache.get(key)
        if hit is not None:
            return hit
        self.stats["requests"] += 1
        data = self.http.get_json(API, params={"query": query, "resultType": "core", "format": "json",
                                               "pageSize": self.page_size, "cursorMark": cursor})
        self.cache.set(key, data)
        return data

    def search(self, title: str, place: Place, radius_miles: float, min_exp, max_exp, keywords: list[str],
               include_unknown_distance: bool, geocoder: PlaceGeocoder,
               progress: Callable[[str], None] = lambda m: None) -> Iterator[Candidate]:
        if not is_health_role(title):
            self.notes.append(f"Europe PMC only covers healthcare research; skipped for '{title}'.")
            return
        matcher = RoleMatcher(title, keywords)
        towns = area_towns(place)
        query = build_query(matcher, towns, self.years)
        progress(f"Europe PMC: recent research on '{matcher.core}' with authors in {', '.join(towns)}…")
        results, cursor = [], "*"
        try:
            for _ in range(self.max_pages):
                data = self._page(query, cursor)
                rows = (data.get("resultList") or {}).get("result") or []
                results += rows
                nxt = data.get("nextCursorMark")
                if not rows or not nxt or nxt == cursor or len(results) >= int(data.get("hitCount") or 0):
                    break
                cursor = nxt
        except (requests.RequestException, ValueError) as e:
            log.info("europe pmc failed: %s", e)
            if not results:
                raise RuntimeError(f"Europe PMC search failed ({e}) - try again later") from e
        self.stats["articles"] = len(results)
        yield from self.parse(results, matcher, towns, place, radius_miles, geocoder, include_unknown_distance)
        if self.stats["kept"]:
            self.notes.append("emails were published for research correspondence - PECR/consent applies")

    def parse(self, results: list[dict], matcher: RoleMatcher, towns: list[str], place: Place, radius_miles: float,
              geocoder: Optional[PlaceGeocoder], include_unknown_distance: bool = False) -> Iterator[Candidate]:
        seen: set[str] = set()
        town_res = [(t, re.compile(rf"\b{re.escape(t)}\b", re.I)) for t in towns]
        for art in results:
            src, aid = art.get("source") or "MED", art.get("id") or art.get("pmid") or ""
            url = f"https://europepmc.org/article/{src}/{aid}" if aid else (
                f"https://doi.org/{art['doi']}" if art.get("doi") else "")
            if not url:
                continue
            atitle = (art.get("title") or "").strip().rstrip(".")
            for a in (art.get("authorList") or {}).get("author") or []:
                first, last = a.get("firstName") or "", a.get("lastName") or ""
                for aff in ((a.get("authorAffiliationDetailsList") or {}).get("authorAffiliation") or []):
                    text = aff.get("affiliation") or ""
                    emails = find_emails(text)
                    if not emails:
                        continue
                    self.stats["authors_with_email"] += 1
                    mine = [e for e in emails if owns_email(e, first, last)]
                    if not mine:
                        self.stats["not_their_email"] += 1
                        continue
                    email = mine[0]
                    town = next((t for t, rx in town_res if rx.search(text)), "")
                    if not town:
                        self.stats["not_near"] += 1
                        continue
                    dist = None
                    co = geocoder.geocode(town) if geocoder else None
                    if co:
                        dist = round(haversine_miles(place.lat, place.lon, *co), 1)
                        if dist > radius_miles + 3:
                            self.stats["not_near"] += 1
                            continue
                    if email in seen:
                        continue
                    seen.add(email)
                    s = matcher.match(text).score   # e.g. 'School of Nursing' for a nurse search
                    name = " ".join(filter(None, [first, last])) or a.get("fullName") or ""
                    org = re.sub(r"\s*[\w.+-]+@[\w.-]+", "", text).strip(" .;,")
                    self.stats["kept"] += 1
                    yield Candidate(
                        name=name, headline=f"{LABEL} - {org[:220]}", company=org.split(",")[0][:120] if org else "",
                        location=town, distance_miles=dist, email=email,
                        email_source=("published as the research correspondence address in their own author "
                                      f"affiliation, Europe PMC article {url} - research contact, PECR/consent applies"),
                        contact_source=url, profile_url=url, kind="candidate", provider="europepmc",
                        match_score=max(1.5, float(s or 0)),
                        match_reason=(f"co-author of '{atitle[:120]}' ({art.get('pubYear', '')}), research on "
                                      f"'{matcher.core}'"),
                        last_active=(art.get("firstPublicationDate") or "")[:10])
                    break
