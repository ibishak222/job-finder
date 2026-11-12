"""Extract contact details that people/companies publish on their OWN websites.

Rules: robots.txt is honoured (HttpClient check_robots), pages marked noindex (meta robots or X-Robots-Tag) are
skipped, obfuscated addresses ("name [at] domain") are NOT decoded (that is a clear sign the owner doesn't want
harvesting), no guessing or permutation, LinkedIn/Twitter/Facebook are never fetched (only links are recorded).
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import asdict
from typing import Optional
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ..crawl import classify_links, discover_feeds, matching_vacancies, parse_feed, vacancy_items
from ..http import HttpClient, RobotsDisallowed
from ..models import Job
from ..utils import find_emails
from .cache import DiskCache

log = logging.getLogger(__name__)

NO_FETCH_HOSTS = ("linkedin.com", "twitter.com", "x.com", "facebook.com", "instagram.com", "github.com",
                  "youtube.com", "tiktok.com", "t.co", "bit.ly", "linktr.ee", "medium.com", "indeed.com")
ROLE_LOCALS = {"careers", "career", "jobs", "job", "recruitment", "recruiting", "recruit", "talent", "hiring", "hr",
               "people", "join", "joinus", "work", "workwithus", "talentacquisition", "resourcing", "vacancies"}
_LINKEDIN = re.compile(r"https?://([a-z]{2,3}\.)?linkedin\.com/in/[A-Za-z0-9_%\-]+/?", re.I)
_TWITTER = re.compile(r"https?://(www\.)?(twitter|x)\.com/(?!share|intent|home|search)[A-Za-z0-9_]{1,15}/?$", re.I)


def normalise_url(url: str) -> Optional[str]:
    url = (url or "").strip()
    if not url:
        return None
    if not re.match(r"https?://", url, re.I):
        url = "https://" + url
    url = re.sub(r"^http://", "https://", url, flags=re.I)  # this network only allows HTTPS out; most sites support it
    p = urlparse(url)
    if not p.netloc or "." not in p.netloc:
        return None
    return url


_TEMP_SEARCH = re.compile(r"\b(locum|bank|sessional|agency|ad[- ]?hoc|temp(orary)?|relief|flexi|per diem)\b", re.I)


def vacancy_type(job: Job) -> str:
    """'locum / bank / temporary' when the ad (title, start of the text, contract type) says so, else 'permanent / other'."""
    from .matching import CONTRACT_TEMP
    hay = f"{job.title}\n{(job.description or '')[:800]}\n{job.employment_type or ''}".lower()
    return "locum / bank / temporary" if CONTRACT_TEMP.search(hay) else "permanent / other"


def _same_host(a: str, b: str) -> bool:
    ha, hb = _host(a), _host(b)
    return ha == hb or ha.endswith("." + hb) or hb.endswith("." + ha)


def _host(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def is_no_fetch(url: str) -> bool:
    h = _host(url)
    return any(h == d or h.endswith("." + d) for d in NO_FETCH_HOSTS)


def _noindex(resp: requests.Response, soup: BeautifulSoup) -> bool:
    if "noindex" in (resp.headers.get("X-Robots-Tag") or "").lower():
        return True
    for m in soup.find_all("meta", attrs={"name": re.compile(r"^(robots|googlebot)$", re.I)}):
        if "noindex" in (m.get("content") or "").lower():
            return True
    return False


def parse_page(html: str, base_url: str) -> dict:
    """Emails / phones / social links published on one page."""
    soup = BeautifulSoup(html, "lxml")
    emails: list[str] = []
    phones: list[str] = []
    linkedin = twitter = None
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        low = href.lower()
        if low.startswith("mailto:"):
            addr = unquote(href[7:].split("?")[0]).strip().lower()
            for e in find_emails(addr):
                if e not in emails:
                    emails.append(e)
        elif low.startswith("tel:"):
            num = unquote(href[4:]).strip()
            if len(re.sub(r"\D", "", num)) >= 10 and num not in phones:
                phones.append(num)
        else:
            full = urljoin(base_url, href)
            if not linkedin and _LINKEDIN.match(full):
                linkedin = _LINKEDIN.match(full).group(0)
            elif not twitter and _TWITTER.match(full):
                twitter = full
    for ent in _schema_entities(html, base_url, soup):
        for e in find_emails(str(ent.get("email") or "").replace("mailto:", "")):
            if e not in emails:
                emails.append(e)
        tel = ent.get("telephone")
        if tel and str(tel) not in phones:
            phones.append(str(tel))
        for cp in ent.get("contactPoint") or []:
            if isinstance(cp, dict):
                for e in find_emails(str(cp.get("email") or "").replace("mailto:", "")):
                    if e not in emails:
                        emails.append(e)
                if cp.get("telephone") and str(cp["telephone"]) not in phones:
                    phones.append(str(cp["telephone"]))
        for same in ent.get("sameAs") or []:
            if not linkedin and _LINKEDIN.match(str(same)):
                linkedin = str(same)
    for s in soup(["script", "style", "noscript"]):
        s.decompose()
    text = soup.get_text(" ")
    for e in find_emails(text):
        if e not in emails:
            emails.append(e)
    out = {"emails": emails, "phones": clean_phones(phones, text), "linkedin": linkedin, "twitter": twitter,
           "managers": practice_managers(text)}
    out.update({f"{k}_links": v for k, v in classify_links(html, base_url).items()})
    return out


# schema.org types that describe a thing rather than the organisation / person publishing the page
_NOT_ENTITY = {"jobposting", "event", "product", "offer", "website", "webpage", "breadcrumblist", "article",
               "blogposting", "newsarticle", "imageobject", "searchaction", "listitem", "itemlist", "review",
               "aggregaterating", "question", "answer", "faqpage", "postaladdress", "place", "geocoordinates"}


def _schema_entities(html: str, base_url: str, soup: BeautifulSoup) -> list[dict]:
    """schema.org entities with a public phone / email: any Organization or LocalBusiness subtype (MedicalClinic,
    Electrician, AccountingService, Restaurant, ...) or Person, from JSON-LD (lenient: raw newlines / trailing commas) and
    microdata (extruct). Page-level `mainEntity` of a ProfilePage is unwrapped."""
    from ..sources.jsonld import _loads_lenient, _walk
    found: list[dict] = []
    for tag in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        try:
            data = _loads_lenient(tag.string or tag.get_text() or "")
        except Exception:  # noqa: BLE001
            continue
        found.extend(d for d in _walk(data))
    try:
        import extruct
        md = extruct.extract(html, base_url=base_url, syntaxes=["microdata"], uniform=True, errors="ignore")
        for item in md.get("microdata") or []:
            found.extend(_walk(item))
    except Exception as e:  # noqa: BLE001 - extruct is optional and must never break a page read
        log.debug("extruct microdata failed: %s", e)
    out: list[dict] = []
    for d in found:
        t = d.get("@type")
        types = [str(x).split("/")[-1].lower() for x in (t if isinstance(t, list) else [t]) if x]
        if types and all(x in _NOT_ENTITY for x in types):
            continue
        ent = d.get("mainEntity") if isinstance(d.get("mainEntity"), dict) else d
        cps = ent.get("contactPoint")
        ent = {**ent, "contactPoint": cps if isinstance(cps, list) else [cps] if cps else []}
        sa = ent.get("sameAs")
        ent["sameAs"] = sa if isinstance(sa, list) else [sa] if sa else []
        if ent.get("email") or ent.get("telephone") or ent["contactPoint"] or ent["sameAs"]:
            out.append(ent)
    return out


_NAME = r"((?:Mrs|Mr|Ms|Miss|Dr)\.?\s+)?([A-Z][a-z'’\-]+(?:\s+[A-Z][a-z'’\-]+){1,2})"
_PM_ROLE = r"(Practice Manager|Practice Business Manager|Business Manager|Managing Partner|Lead Nurse|Nurse Manager|" \
           r"Senior Practice Nurse|Lead Practice Nurse)"
_PM_AFTER = re.compile(_PM_ROLE + r"\s*(?:is\s+)?[:\-–—,]?\s+" + _NAME)
_PM_BEFORE = re.compile(_NAME + r"\s*(?:[,\-–—(|]\s*|\s+is\s+(?:our|the)\s+|\s+)" + _PM_ROLE)
_NOT_NAME = re.compile(r"\b(Our|The|Your|Contact|Meet|Team|Practice|Surgery|Medical|Centre|Health|Nurse|Manager|Partner|"
                       r"Email|Phone|Telephone|Opening|Hours|Patient|Patients|Services|Appointments|Reception|Home|"
                       r"About|News|Staff|Clinical|Doctors?|Gp|Nhs|Lead|Senior|Business|Managing|Admin|Address|Who|"
                       r"We|Us|Please|If|For|And|Or|To|Of|In|At|On|By|With)\b")


def practice_managers(text: str, limit: int = 3) -> list[tuple[str, str]]:
    """(name, role) for the practice manager / lead nurse a surgery names on its OWN website ('Practice Manager:
    Jane Smith', 'Jane Smith - Practice Manager'). Business contacts the organisation chose to publish."""
    t = re.sub(r"\s+", " ", text or "")[:300_000]
    out: list[tuple[str, str]] = []
    for rx, ni, ri in ((_PM_AFTER, 3, 1), (_PM_BEFORE, 2, 3)):
        for m in rx.finditer(t):
            name, role = m.group(ni).strip(), m.group(ri)
            if _NOT_NAME.search(name) or len(name) > 40:
                continue
            title = (m.group(ni - 1) or "").strip()
            full = f"{title} {name}".strip()
            if full not in [o[0] for o in out]:
                out.append((full, role))
            if len(out) >= limit:
                return out
    return out


_NHS_MAIL = re.compile(r"@(nhs\.net|[\w.-]*nhs\.(uk|scot)|hscni\.net|[\w.-]*\.hscni\.net|wales\.nhs\.uk)$", re.I)


def clean_phones(raw: list[str], text: str = "", max_from_text: int = 2) -> list[str]:
    """Validate and format phone numbers the owner published (tel: links, schema.org telephone, and numbers written
    in the page text) with Google's libphonenumber rules (https://github.com/daviddrysdale/python-phonenumbers).
    UK by default; invalid numbers (dates, IDs, order numbers) are dropped. Falls back to the raw list if the
    library is missing."""
    try:
        import phonenumbers
    except ImportError:  # pragma: no cover
        return list(dict.fromkeys(raw))
    out: list[str] = []

    def add(num, strict: bool = True) -> None:
        # numbers the owner explicitly marked as a phone (tel: link / schema.org) only need to be possible;
        # numbers spotted in page text must be fully valid so dates and reference numbers are not picked up
        if phonenumbers.is_valid_number(num) or (not strict and phonenumbers.is_possible_number(num)):
            f = phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.INTERNATIONAL)
            if f not in out:
                out.append(f)

    for r in raw:
        try:
            add(phonenumbers.parse(r, "GB"), strict=False)
        except phonenumbers.NumberParseException:
            continue
    if text:
        for i, m in enumerate(phonenumbers.PhoneNumberMatcher(text[:200_000], "GB",
                                                              leniency=phonenumbers.Leniency.VALID)):
            if i >= max_from_text:
                break
            add(m.number)
    return out


class SiteContactFinder:
    def __init__(self, http: HttpClient, cache_ttl_days: float = 7):
        self.http = http
        self.cache = DiskCache("sites", cache_ttl_days * 86400)

    deadline: Optional[float] = None    # time.monotonic() after which no more pages are fetched (search time budget)

    def _fetch(self, url: str, careers: bool = False) -> Optional[dict]:
        key = url + ("#careers" if careers else "")
        cached = self.cache.get(key)
        if cached is not None:
            return cached or None
        if self.deadline is not None and time.monotonic() > self.deadline:
            return None                 # out of search time: no more page requests
        out: dict = {}
        try:
            r = self.http.get(url, check_robots=True, headers={"Accept": "text/html,application/xhtml+xml"})
            if "html" in (r.headers.get("Content-Type") or "html") and len(r.content) < 3_000_000:
                soup = BeautifulSoup(r.text, "lxml")
                if _noindex(r, soup):
                    out = {"skipped": "noindex"}
                else:
                    out = parse_page(r.text, r.url)
                    out["url"] = r.url
                    if careers:
                        out["vacancies"] = [asdict(j) for j in vacancy_items(r.text, r.url)]
                        out["feeds"] = discover_feeds(r.text, r.url)[:2]
        except RobotsDisallowed:
            out = {"skipped": "robots.txt"}
        except requests.RequestException as e:
            log.debug("site fetch failed %s: %s", url, e)
            return None  # don't cache network failures
        self.cache.set(key, out)
        return out or None

    def personal(self, site_url: str) -> dict:
        """Contacts a person publishes on their own site: homepage, then /contact and /about if needed."""
        url = normalise_url(site_url)
        res = {"emails": [], "phones": [], "linkedin": None, "twitter": None, "source_url": None, "note": ""}
        if not url:
            return res
        if is_no_fetch(url):
            if _LINKEDIN.match(url):
                res["linkedin"] = url
            elif re.search(r"(twitter|x)\.com/", url):
                res["twitter"] = url
            res["note"] = "social profile link only (not fetched)"
            return res
        base = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
        for page in (url, urljoin(base, "/contact"), urljoin(base, "/about")):
            got = self._fetch(page)
            if not got:
                continue
            if got.get("skipped"):
                res["note"] = f"skipped ({got['skipped']})"
                if page == url:
                    return res
                continue
            for e in got.get("emails", []):
                if not find_emails(e):  # re-check cached pages against the latest placeholder/no-reply filters
                    continue
                if e not in res["emails"]:
                    res["emails"].append(e)
                    res["source_url"] = res["source_url"] or got.get("url") or page
            res["phones"] += [p for p in got.get("phones", []) if p not in res["phones"]]
            res["linkedin"] = res["linkedin"] or got.get("linkedin")
            res["twitter"] = res["twitter"] or got.get("twitter")
            if res["emails"]:
                break
        return res

    def practice(self, site_url: str) -> dict:
        """What a GP practice / clinic publishes on its OWN website: its contact email(s) and the practice manager's
        name. Only emails on the practice's own domain or an NHS / HSC address are kept (never third-party ones);
        robots.txt and noindex are respected, pages are cached."""
        url = normalise_url(site_url)
        res = {"emails": [], "managers": [], "source_url": None, "note": ""}
        if not url or is_no_fetch(url):
            return res
        host = _host(url).removeprefix("www.")
        base = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
        for page in (url, urljoin(base, "/contact-us"), urljoin(base, "/contact"), urljoin(base, "/practice-staff")):
            got = self._fetch(page)
            if not got:
                continue
            if got.get("skipped"):
                res["note"] = f"skipped ({got['skipped']})"
                if page == url:
                    return res
                continue
            for e in got.get("emails", []):
                dom = e.rsplit("@", 1)[-1].lower()
                own = dom == host or dom.endswith("." + host) or host.endswith("." + dom)
                if find_emails(e) and (own or _NHS_MAIL.search(e)) and e not in res["emails"]:
                    res["emails"].append(e)
                    res["source_url"] = res["source_url"] or got.get("url") or page
            for name, role in got.get("managers") or []:
                if name not in [m[0] for m in res["managers"]]:
                    res["managers"].append((name, role))
                    res["source_url"] = res["source_url"] or got.get("url") or page
            if res["emails"] and res["managers"]:
                break
        return res

    def employer(self, site_url: str, keyword: Optional[str] = None, max_careers: int = 2) -> dict:
        """What an employer publishes on its OWN website (any trade or profession): contact email(s) on its own domain (or an
        NHS / HSC address), phone, the manager it names, the careers / vacancies page linked from its site, and any open
        vacancy there whose title fits `keyword`. Contact pages are found from the links on the homepage ('Contact us',
        'Get in touch'), then the usual paths; robots.txt and noindex are respected and every page is cached."""
        url = normalise_url(site_url)
        res = {"emails": [], "phones": [], "managers": [], "source_url": None, "note": "", "careers_url": "",
               "vacancies": [], "pages": 0, "bank_url": ""}
        if not url or is_no_fetch(url):
            return res
        host = _host(url)
        base = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
        home = self._fetch(url)
        if not home:
            return res
        if home.get("skipped"):
            res["note"] = f"skipped ({home['skipped']})"
            return res
        contact_links = list(home.get("contact_links") or [])
        careers_links = list(home.get("careers_links") or [])
        pages = [(url, home)]
        self._collect(res, home, url, host)
        fallback = [urljoin(base, p) for p in ("/contact-us", "/contact")] if not contact_links else []
        for page in contact_links[:2] + fallback + [urljoin(base, "/practice-staff")]:
            if len(pages) >= 4 or (res["emails"] and res["managers"] and page != url):
                break
            if any(page.rstrip("/") == p.rstrip("/") for p, _ in pages):
                continue
            if page.endswith("/practice-staff") and res["managers"]:
                continue
            got = self._fetch(page)
            if got and not got.get("skipped"):
                pages.append((page, got))
                self._collect(res, got, page, host)
        res["pages"] = len(pages)
        bank_links = list(home.get("bank_links") or [])
        temporary = _TEMP_SEARCH.search(keyword or "") is not None
        # locum / bank searches look at the 'join our bank' pages first, others at the careers pages first
        links = (bank_links + careers_links) if temporary else (careers_links + bank_links)
        for link in links:
            if link in bank_links and not res["bank_url"]:
                res["bank_url"] = link
            if link in careers_links and not res["careers_url"]:
                res["careers_url"] = link
            if len(res["vacancies"]) >= 5 or max_careers <= 0:
                if res["careers_url"] and (res["bank_url"] or not bank_links):
                    break
                continue
            if not _same_host(link, url):
                continue            # a job platform the employer links to: recorded, but not crawled from here
            got = self._fetch(link, careers=True)
            max_careers -= 1
            if not got or got.get("skipped"):
                continue
            res["pages"] += 1
            self._collect(res, got, link, host)
            items = [Job(**{k: v for k, v in j.items() if k in Job.field_names()}) for j in got.get("vacancies") or []]
            for b in got.get("bank_links") or []:
                if not res["bank_url"]:
                    res["bank_url"] = b
            found = matching_vacancies(items, keyword)
            if not found and got.get("feeds"):
                found = self._feed_vacancies(got["feeds"][0], keyword)
            for j in found:
                res["vacancies"].append({"title": j.title, "url": j.url, "pay": j.salary_raw or "",
                                         "closing_date": j.closing_date or "", "page": got.get("url") or link,
                                         "type": vacancy_type(j)})
        return res

    def _feed_vacancies(self, feed_url: str, keyword: Optional[str]) -> list[Job]:
        """Vacancies in an RSS / Atom feed a careers page advertises (read with our client: robots.txt, rate limit)."""
        if self.http is None:
            return []
        try:
            r = self.http.get(feed_url, check_robots=True, headers={"Accept": "application/rss+xml, application/atom+xml, */*"})
            return matching_vacancies(parse_feed(r.content, feed_url, source="site"), keyword)
        except (RobotsDisallowed, requests.RequestException, ImportError, ValueError) as e:
            log.debug("feed %s failed: %s", feed_url, e)
            return []

    @staticmethod
    def _collect(res: dict, got: dict, page: str, host: str) -> None:
        for e in got.get("emails", []):
            dom = e.rsplit("@", 1)[-1].lower()
            own = dom == host or dom.endswith("." + host) or host.endswith("." + dom)
            if find_emails(e) and (own or _NHS_MAIL.search(e)) and e not in res["emails"]:
                res["emails"].append(e)
                res["source_url"] = res["source_url"] or got.get("url") or page
        for p in got.get("phones", []):
            if p not in res["phones"]:
                res["phones"].append(p)
                res["source_url"] = res["source_url"] or got.get("url") or page
        for name, role in got.get("managers") or []:
            if name not in [m[0] for m in res["managers"]]:
                res["managers"].append((name, role))
                res["source_url"] = res["source_url"] or got.get("url") or page

    def company(self, domain: str) -> list[tuple[str, str]]:
        """Recruiting role addresses (careers@, jobs@, talent@ …) a company publishes on its own site."""
        out: list[tuple[str, str]] = []
        domain = (domain or "").strip().lower().removeprefix("www.")
        if not domain:
            return out
        for path in ("/careers", "/jobs", "/contact", "/contact-us"):
            got = self._fetch(f"https://www.{domain}{path}") or self._fetch(f"https://{domain}{path}")
            if not got or got.get("skipped"):
                continue
            for e in got.get("emails", []):
                local, _, dom = e.partition("@")
                brand = domain.split(".")[0]
                if local in ROLE_LOCALS and (dom == domain or dom.endswith("." + domain) or brand in dom.split(".")):
                    if e not in [x[0] for x in out]:
                        out.append((e, got.get("url") or path))
        return out
