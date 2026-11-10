"""Find contact details people PUBLISHED THEMSELVES, by following the links on their own public profile.

For each candidate (top N by best-match score, within a time budget) we read up to `max_pages` pages:
their website / own-domain handle, link-in-bio pages (bio.link, beacons, lnk.bio, solo.to, carrd, about.me …) and
the personal sites those pages link to, plus one hop to a /contact or /about page on the same site.

Rules (UK GDPR / PECR / site terms):
* robots.txt is honoured for every page and pages marked noindex are skipped (Linktree's robots.txt forbids
  crawling, so Linktree pages are never read). LinkedIn, X, Facebook, Instagram, GitHub etc. are never fetched.
* Nothing is guessed or generated: an address must appear on the page (mailto:, page text, schema.org
  Person/ContactPoint, or written out as 'name [at] domain [dot] com', which is decoded the way a reader would).
* An address is only attached to the person when it is clearly theirs: found on their own site / link page, or
  its local part contains their name. Generic firm addresses (info@, enquiries@, office@ …) on an employer's site
  become a separate 'business' row instead. Every contact keeps the URL it came from (`contact_source`).
"""
from __future__ import annotations

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ..http import HttpClient, RobotsDisallowed
from ..utils import find_emails
from .cache import DiskCache
from .contacts import _noindex, clean_phones, is_no_fetch, normalise_url
from .models import Candidate

log = logging.getLogger(__name__)

# link-in-bio services whose public profile pages robots.txt allows (checked again live for every fetch)
AGGREGATORS = ("bio.link", "beacons.ai", "lnk.bio", "solo.to", "taplink.cc", "msha.ke", "allmylinks.com",
               "carrd.co", "crd.co", "about.me", "campsite.bio", "linkin.bio", "linkbio.co", "bio.site", "hoo.be",
               "flow.page", "znap.link", "linkr.bio", "contactin.bio", "biolinky.co", "koji.to", "linktr.ee")
# never fetched: social networks, code hosts, link shorteners, job boards (their terms forbid it or it's pointless)
SKIP_HOSTS = ("bsky.app", "bsky.social", "mastodon.", "threads.net", "tiktok.com", "youtube.com", "youtu.be",
              "spotify.com", "twitch.tv", "patreon.com", "ko-fi.com", "buymeacoffee.com", "paypal.", "amazon.",
              "goodreads.com", "wikipedia.org", "google.com", "apple.com", "substack.com", "medium.com",
              "glassdoor.", "indeed.", "reed.co.uk", "totaljobs.com", "eventbrite.", "gofundme.com", "justgiving.com",
              "orcid.org", "doi.org", "researchgate.net", "scholar.google", "t.me", "wa.me", "discord.gg",
              "discord.com", "flickr.com", "pinterest.", "reddit.com", "tumblr.com", "gravatar.com", "brid.gy")
GENERIC_LOCALS = {"info", "enquiries", "enquiry", "inquiries", "office", "admin", "reception", "contact", "contactus",
                  "team", "sales", "support", "mail", "general", "accounts", "bookings", "booking", "hr", "jobs",
                  "careers", "recruitment", "press", "media", "marketing", "studio", "help", "service", "services",
                  "clinic", "practice", "law", "legal", "nurses", "care", "hello", "hi", "post", "webmaster",
                  "membership", "members", "events", "news", "newsletter", "editor", "editorial", "submissions"}
FREE_MAIL = {"gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "hotmail.co.uk", "live.com", "live.co.uk",
             "yahoo.com", "yahoo.co.uk", "icloud.com", "me.com", "proton.me", "protonmail.com", "pm.me", "aol.com",
             "btinternet.com", "sky.com", "virginmedia.com", "fastmail.com", "zoho.com", "gmx.com", "tutanota.com"}

_AT = r"(?:\s*[\[\(\{<]\s*at\s*[\]\)\}>]\s*|\s+at\s+|\s*\(@\)\s*|\s*\[@\]\s*)"
_DOT = r"(?:\s*[\[\(\{<]\s*dot\s*[\]\)\}>]\s*|\s+dot\s+)"
_OBF = re.compile(rf"\b([a-z0-9][a-z0-9._%+\-]{{0,63}}){_AT}([a-z0-9\-]+(?:(?:{_DOT}|\.)[a-z0-9\-]+)+)\b", re.I)
_TLD = re.compile(r"\.(com|net|org|co\.uk|org\.uk|ac\.uk|nhs\.uk|uk|me|io|dev|info|biz|scot|wales|cymru|eu|law|"
                  r"legal|email|online|site|xyz|ie|london)$", re.I)


_HANDLE = re.compile(r"(?<![\w.])@[\w.\-]+@[\w\-]+(\.[\w\-]+)+")   # fediverse handles: @jo@mastodon.social


def strip_handles(text: str) -> str:
    """Remove '@user@server' fediverse handles so they are not mistaken for email addresses."""
    return _HANDLE.sub(" ", text or "")


def deobfuscate_emails(text: str) -> list[str]:
    """Addresses a person wrote out to avoid spam bots, e.g. 'jo [at] example [dot] com', 'jo(at)example.co.uk',
    'jo at example dot com'. Only decoded when the result is a well-formed address with a real-looking domain;
    'nurse at NHS' (no dot part) is not an email."""
    out: list[str] = []
    for m in _OBF.finditer(text or ""):
        raw_at = m.group(0)[len(m.group(1)):]
        plain_words = not re.search(r"[\[\(\{<@]", raw_at)
        domain = re.sub(_DOT, ".", m.group(2), flags=re.I).lower()
        if plain_words and not re.search(_DOT, m.group(2), re.I):
            continue  # 'jo at example.com' in prose is too ambiguous unless 'dot' is written out too
        if "." not in domain or not _TLD.search(domain):
            continue
        for e in find_emails(f"{m.group(1).lower()}@{domain}"):
            if e not in out:
                out.append(e)
    return out


def text_contacts(text: str, decode_obfuscated: bool = True) -> tuple[list[str], list[str]]:
    """Emails and (fully valid) phone numbers written in a bio or profile field."""
    text = strip_handles(text or "")
    emails = find_emails(text)
    if decode_obfuscated:
        emails += [e for e in deobfuscate_emails(text) if e not in emails]
    return emails, clean_phones([], text, max_from_text=1)


def host_of(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def is_aggregator(url: str) -> bool:
    h = host_of(url)
    return any(h == a or h.endswith("." + a) for a in AGGREGATORS)


def skip_url(url: str) -> bool:
    h = host_of(url)
    if not h or is_no_fetch(url):
        return True
    if any(s in h for s in SKIP_HOSTS):
        return True
    if re.search(r"/@[\w.\-]+/?$|/users/[\w.\-]+/?$", urlparse(url).path or ""):
        return True   # another fediverse profile page (Mastodon, Pixelfed …), not a personal site
    return bool(re.search(r"\.(pdf|jpe?g|png|gif|mp[34]|zip|docx?)($|\?)", url, re.I))


def parse_contact_page(html: str, base_url: str) -> dict:
    """Everything we may use from one page: emails (+ how they were written), phones, schema.org Person /
    ContactPoint details (via extruct when installed), outbound links and the page title."""
    soup = BeautifulSoup(html, "lxml")
    emails: list[str] = []
    obf: list[str] = []
    tel: list[str] = []
    links: list[tuple[str, str]] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        low = href.lower()
        if low.startswith("mailto:"):
            for e in find_emails(requests.utils.unquote(href[7:].split("?")[0])):
                if e not in emails:
                    emails.append(e)
        elif low.startswith("tel:"):
            tel.append(requests.utils.unquote(href[4:]).strip())
        elif low.startswith(("http://", "https://", "/")):
            full = urljoin(base_url, href)
            links.append((full, a.get_text(" ", strip=True)[:60]))
    people: list[dict] = []
    for item in _structured(html, base_url):
        for ent in _walk(item):
            typ = str(ent.get("@type") or ent.get("type") or "").split("/")[-1]
            props = ent.get("properties") if isinstance(ent.get("properties"), dict) else ent
            if typ not in ("Person", "ContactPoint", "Organization", "LocalBusiness", "ProfilePage"):
                continue
            e = [x for x in find_emails(str(props.get("email") or "").replace("mailto:", ""))]
            t = props.get("telephone")
            for x in e:
                if x not in emails:
                    emails.append(x)
            if t:
                tel.append(str(t))
            if typ == "Person":
                people.append({"name": str(props.get("name") or ""), "emails": e})
    for s in soup(["script", "style", "noscript", "template"]):
        s.decompose()
    text = strip_handles(soup.get_text(" "))
    for e in find_emails(text):
        if e not in emails:
            emails.append(e)
    for e in deobfuscate_emails(text):
        if e not in emails and e not in obf:
            obf.append(e)
    title = (soup.title.get_text(" ", strip=True) if soup.title else "")[:120]
    return {"emails": emails, "obfuscated": obf, "phones": clean_phones(tel, text, max_from_text=1),
            "links": links[:200], "title": title, "people": people[:50]}


def _structured(html: str, base_url: str) -> list:
    try:
        import extruct  # https://github.com/scrapinghub/extruct
        data = extruct.extract(html, base_url=base_url, syntaxes=["json-ld", "microdata"], uniform=True,
                               errors="ignore")
        return (data.get("json-ld") or []) + (data.get("microdata") or [])
    except Exception:  # noqa: BLE001 - fall back to plain JSON-LD
        out = []
        for m in re.finditer(r'<script[^>]+ld\+json[^>]*>(.*?)</script>', html, re.S | re.I):
            try:
                out.append(json.loads(m.group(1)))
            except ValueError:
                continue
        return out


def _walk(obj):
    if isinstance(obj, list):
        for o in obj:
            yield from _walk(o)
    elif isinstance(obj, dict):
        yield obj
        for k in ("@graph", "mainEntity", "contactPoint", "author", "employee", "member", "founder", "properties"):
            if k in obj:
                yield from _walk(obj[k])


def name_tokens(name: str) -> list[str]:
    n = re.sub(r"[^a-z\s'\-]", " ", (name or "").lower())
    return [w for w in re.split(r"[\s'\-]+", n) if len(w) >= 3 and w not in {"the", "and", "nurse", "mrs", "miss"}]


@dataclass
class ContactResult:
    email: str = ""
    phone: str = ""
    source: str = ""            # URL the email (or phone) was found on
    how: str = ""               # e.g. "mailto link", "written as 'name [at] domain'"
    pages: list[str] = field(default_factory=list)
    business: Optional[dict] = None   # a firm address found instead (routed to Businesses)
    note: str = ""


class ContactEnricher:
    def __init__(self, http: Optional[HttpClient] = None, max_pages: int = 3, decode_obfuscated: bool = True,
                 cache_days: float = 7):
        self.http = http or HttpClient(min_interval=1.0, timeout=10, retries=1)
        self.max_pages = max_pages
        self.decode = decode_obfuscated
        self.cache = DiskCache("contact_pages", cache_days * 86400)

    def fetch(self, url: str) -> Optional[dict]:
        hit = self.cache.get(url)
        if hit is not None:
            return None if (not hit or hit.get("missing")) else hit
        out: dict = {}
        try:
            r = self.http.get(url, check_robots=True, headers={"Accept": "text/html,application/xhtml+xml"})
            ctype = r.headers.get("Content-Type") or "html"
            if "html" in ctype and len(r.content) < 3_000_000:
                soup = BeautifulSoup(r.text, "lxml")
                if _noindex(r, soup):
                    out = {"skipped": "noindex"}
                else:
                    out = parse_contact_page(r.text, r.url)
                    out["url"] = r.url
        except RobotsDisallowed:
            out = {"skipped": "robots.txt"}
        except requests.HTTPError as e:
            code = getattr(e.response, "status_code", None)
            if code in (404, 410):
                self.cache.set(url, {"missing": True})   # remember dead links (e.g. no /contact page)
            log.debug("contact page fetch failed %s: %s", url, e)
            return None
        except requests.RequestException as e:
            log.debug("contact page fetch failed %s: %s", url, e)
            return None
        self.cache.set(url, out)
        return out or None

    # ------------------------------------------------------------------ choosing
    @staticmethod
    def own_site(url: str, title: str, toks: list[str], own_hosts: set[str]) -> bool:
        h = host_of(url)
        if is_aggregator(url) or h in own_hosts:
            return True
        hn = re.sub(r"[^a-z]", "", h.split(".")[0])
        return any(t in hn for t in toks) or any(t in (title or "").lower() for t in toks)

    def pick(self, page: dict, url: str, toks: list[str], own_hosts: set[str]) -> tuple[str, str, Optional[str]]:
        """(email, how, firm_email) for one page under the ownership rules."""
        own = self.own_site(url, page.get("title", ""), toks, own_hosts)
        cands = [(e, "published on the page") for e in page.get("emails", [])]
        if self.decode:
            cands += [(e, "written out as 'name [at] domain' on the page") for e in page.get("obfuscated", [])]
        many = len({e for e, _ in cands}) > 3
        firm = None
        for e, how in cands:
            local, _, dom = e.partition("@")
            named = any(t in local for t in toks)
            for p in page.get("people", []):   # schema.org Person with this person's name and email
                if e in p.get("emails", []) and any(t in p.get("name", "").lower() for t in toks):
                    named = True
            generic = re.sub(r"[^a-z]", "", local) in GENERIC_LOCALS
            if named:
                return e, how, None
            if many:
                continue            # a staff list: only an address with their name in it is theirs
            if generic:
                if own and (dom in FREE_MAIL or host_of(url).endswith(dom) or dom in own_hosts):
                    return e, how + " (their own site)", None
                firm = firm or e
                continue
            if own or dom in FREE_MAIL:
                return e, how, None
        return "", "", firm

    def enrich(self, c: Candidate, deadline: float = float("inf")) -> ContactResult:
        res = ContactResult()
        toks = name_tokens(c.name)
        urls = [c.website] + (c.other_profiles or "").split()
        own_hosts = {host_of(c.website)} if c.website else set()
        queue: list[str] = []
        for u in urls:
            u = normalise_url((u or "").rstrip(".,;)"))
            if u and not skip_url(u) and u not in queue:
                queue.append(u)
        queue.sort(key=lambda u: (not is_aggregator(u), u != normalise_url(c.website or "")))
        seen: set[str] = set()
        hopped: set[str] = set()
        while queue and len(res.pages) < self.max_pages and time.monotonic() < deadline:
            url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            page = self.fetch(url)
            res.pages.append(url)
            if not page or page.get("skipped"):
                res.note = res.note or (f"skipped {host_of(url)} ({page['skipped']})" if page else "")
                continue
            src = page.get("url") or url
            if not res.phone and page.get("phones") and (c.kind == "business" or
                                                          self.own_site(src, page.get("title", ""), toks, own_hosts)):
                res.phone, res.source = page["phones"][0], res.source or src
            email, how, firm = self.pick(page, src, toks, own_hosts)
            if email:
                res.email, res.how, res.source = email, how, src
                break
            if firm and not res.business:
                res.business = {"email": firm, "url": src, "title": page.get("title") or host_of(src)}
            links = page.get("links") or []
            if is_aggregator(src):   # a link-in-bio page: follow the person's own sites listed there next
                own_hosts.add(host_of(src))
                nxt = [u for u, _ in links if not skip_url(u) and not is_aggregator(u) and host_of(u) != host_of(src)]
                for u in list(dict.fromkeys(nxt))[:2][::-1]:
                    own_hosts.add(host_of(u))
                    queue.insert(0, u)
            elif host_of(src) not in hopped:   # one hop to the same site's contact / about page
                hopped.add(host_of(src))
                same = [u for u, t in links if host_of(u) == host_of(src) and re.search(
                    r"contact|about|get-in-touch|hire|work-with-me", f"{u} {t}", re.I) and u.rstrip("/") != src.rstrip("/")]
                same.sort(key=lambda u: "contact" not in u.lower())
                if not same:
                    base = f"{urlparse(src).scheme}://{urlparse(src).netloc}"
                    same = [urljoin(base, "/contact")]
                queue.insert(0, same[0])
        return res


def apply_contacts(c: Candidate, r: ContactResult) -> Optional[Candidate]:
    """Write a ContactResult onto the candidate; returns a business row for a firm address, if any."""
    if c.kind == "business":   # a firm's own account: its generic address is exactly what we want
        email = r.email or (r.business or {}).get("email", "")
        if email:
            c.email, c.contact_source = email, r.source or (r.business or {}).get("url", "")
            c.email_source = f"published on {c.contact_source} (linked from the organisation's own profile)"
        if r.phone and not c.phone:
            c.phone, c.contact_source = r.phone, c.contact_source or r.source
        return None
    if r.email:
        c.email, c.contact_source = r.email, r.source
        c.email_source = f"{r.how} {r.source} (linked from their own profile)"
    if r.phone and not c.phone:
        c.phone = r.phone
        c.contact_source = c.contact_source or r.source
    if not c.email and r.pages:
        c.email_source = (f"none published on {len(r.pages)} page(s) linked from their profile - message them via "
                          "their profile")
    if r.business and not r.email:
        b = r.business
        return Candidate(name=b["title"][:80], kind="business", headline=f"Organisation linked from {c.name}'s profile",
                         location=c.location, distance_miles=c.distance_miles, email=b["email"],
                         email_source=f"published on {b['url']} (a firm/organisation address, not personal)",
                         contact_source=b["url"], profile_url=b["url"], provider=c.provider,
                         match_score=c.match_score, match_reason=f"linked from {c.name}'s profile ({c.match_reason})")
    return None


def enrich_candidates(cands: list[Candidate], top_n: int = 60, seconds: float = 120, max_pages: int = 3,
                      decode_obfuscated: bool = True, workers: int = 6, enricher: Optional[ContactEnricher] = None,
                      progress: Callable[[str], None] = lambda m: None) -> tuple[list[Candidate], dict]:
    """Check the top-N people (by current order) that have no email yet, within `seconds`. Returns new business
    rows (firm addresses) and stats."""
    def has_links(c: Candidate) -> bool:
        return bool(c.website) or any(not skip_url(u) for u in (c.other_profiles or "").split())
    todo = [c for c in cands if c.kind == "candidate" and not c.email and has_links(c)][:top_n]
    # firm accounts found on Bluesky/Mastodon (OpenStreetMap rows have their own website check)
    todo += [c for c in cands if c.kind == "business" and not c.email and c.provider in ("bluesky", "mastodon")
             and has_links(c)][:max(10, top_n // 3)]
    stats = {"people_checked": 0, "pages_read": 0, "emails_found": 0, "phones_found": 0, "firm_addresses": 0}
    if not todo:
        return [], stats
    enr = enricher or ContactEnricher(max_pages=max_pages, decode_obfuscated=decode_obfuscated)
    deadline = time.monotonic() + seconds
    progress(f"Checking the links on {len(todo)} people's profiles for contact details they published "
             f"(up to {max_pages} pages each, max {int(seconds)}s)…")
    extra: list[Candidate] = []
    ex = ThreadPoolExecutor(max_workers=workers)
    futs = {ex.submit(enr.enrich, c, deadline): c for c in todo}
    pending = set(futs)
    try:
        while pending and time.monotonic() < deadline:
            done, pending = wait(pending, timeout=max(0.1, deadline - time.monotonic()), return_when=FIRST_COMPLETED)
            for f in done:
                c = futs[f]
                try:
                    r = f.result()
                except Exception as e:  # noqa: BLE001
                    log.info("contact check failed for %s: %s", c.profile_url, e)
                    continue
                stats["people_checked"] += 1
                stats["pages_read"] += len(r.pages)
                had_phone = bool(c.phone)
                b = apply_contacts(c, r)
                stats["emails_found"] += bool(r.email)
                stats["phones_found"] += bool(c.phone) and not had_phone
                if b:
                    extra.append(b)
                    stats["firm_addresses"] += 1
                if stats["people_checked"] % 10 == 0:
                    progress(f"Contact check: {stats['people_checked']}/{len(todo)} people, "
                             f"{stats['emails_found']} emails so far…")
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    if pending:
        stats["not_checked_time_limit"] = len(pending)
    return extra, stats
