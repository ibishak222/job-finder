"""Helpers for reading an employer's OWN website: finding its contact / careers pages from the links on the page, spotting
open vacancies on a careers page, and reading RSS / Atom job feeds.

Libraries (all run on HTML / XML that our own polite HttpClient has already fetched, so robots.txt, rate limits and the
user agent still apply - none of them is allowed to make its own requests):
* trafilatura (https://github.com/adbar/trafilatura, Apache-2.0): main-text extraction for careers pages that carry no
  schema.org data, so the ad text is the article and not the menu / cookie banner.
* extruct (https://github.com/scrapinghub/extruct, BSD-3): schema.org JobPosting / LocalBusiness in JSON-LD, microdata, RDFa.
* feedparser (https://github.com/kurtmckee/feedparser, BSD-2): RSS / Atom / RDF job feeds.
"""
from __future__ import annotations

import logging
import re
from typing import Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .models import Job

log = logging.getLogger(__name__)

_CONTACT = re.compile(r"contact|get[ -]in[ -]touch|find[ -]us|enquir|reach[ -]us", re.I)
_CAREERS = re.compile(r"career|vacanc|\bjobs?\b|join[ -](our|the|us)|work(ing)?[ -](for|with|at)[ -]us|recruit|"
                      r"opportunit|employment|we.re hiring|come and work", re.I)
_BANK = re.compile(r"\b(nurse|nurses|staff|clinician|locum|care|carer|hca|worker|flexible|join[ _-](our|the)|our|a)[ _-]bank\b|"
                   r"\bbank[ _-](staff|worker|nurse|shift|work|team|register|opportunit)|locum|flexi(ble)?[ _-]?(pool|work|staff)|"
                   r"sessional|ad[ _-]?hoc|temporary[ _-](staff|work|roles|staffing)|register[ _-](with[ _-]us|your[ _-]interest)|"
                   r"join[ _-](our|the)[ _-](pool|team[ _-]of[ _-]locums)|agency[ _-](staff|nurses|work)|work[ _-](flexibly|shifts)|"
                   r"shift[ _-]work", re.I)
_BAD_HREF = re.compile(r"\.(pdf|docx?|xlsx?|jpe?g|png|gif|svg|zip|mp4)(\?|$)|^(mailto|tel|javascript):|/wp-json/|"
                       r"/(privacy|cookie|terms|login|sign-?in|basket|cart)(/|$)", re.I)
_NO_VACANCIES = re.compile(r"\b(no|currently no|there are no|sorry,? no)( current| open| available)? "
                           r"(vacanc|positions|openings|roles|jobs)", re.I)
_FEED_TYPES = ("application/rss+xml", "application/atom+xml", "application/rdf+xml", "application/feed+json")


def _same_site(a: str, b: str) -> bool:
    ha, hb = urlparse(a).netloc.lower().removeprefix("www."), urlparse(b).netloc.lower().removeprefix("www.")
    return ha == hb or ha.endswith("." + hb) or hb.endswith("." + ha)


def classify_links(html: str, base_url: str, limit: int = 3) -> dict[str, list[str]]:
    """Contact, careers and bank / locum pages linked from a page ({'contact': [...], 'careers': [...], 'bank': [...]}),
    best first ('Join our bank', 'Locum opportunities', 'Register with us'). Same-site links
    only for contact pages; careers links may point to the site's job platform (recorded, never crawled by us)."""
    soup = BeautifulSoup(html, "lxml")
    out: dict[str, list[tuple[int, str]]] = {"contact": [], "careers": [], "bank": []}
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"].strip()).split("#")[0]
        if not href.startswith("http") or _BAD_HREF.search(href):
            continue
        text = re.sub(r"\s+", " ", a.get_text(" ", strip=True))[:80]
        path = urlparse(href).path
        for kind, rx in (("contact", _CONTACT), ("careers", _CAREERS), ("bank", _BANK)):
            if kind == "contact" and not _same_site(href, base_url):
                continue
            in_text, in_path = bool(rx.search(text)), bool(rx.search(path))
            if not (in_text or in_path) or len(text) > 60 and not in_path:
                continue
            # short link text beats a path match; navigation links (nav / header / footer) beat body links
            score = (2 if in_text and len(text) <= 30 else 1 if in_text else 0) + (1 if in_path else 0)
            score += 1 if a.find_parent(["nav", "header", "footer"]) else 0
            out[kind].append((score, href))
    res: dict[str, list[str]] = {}
    for kind, items in out.items():
        seen: list[str] = []
        for _, href in sorted(items, key=lambda x: -x[0]):
            key = href.rstrip("/").lower()
            if key not in [s.rstrip("/").lower() for s in seen] and key != base_url.rstrip("/").lower():
                seen.append(href)
        res[kind] = seen[:limit]
    return res


def discover_feeds(html: str, base_url: str) -> list[str]:
    """RSS / Atom feeds a page advertises with <link rel="alternate">."""
    soup = BeautifulSoup(html, "lxml")
    out: list[str] = []
    for tag in soup.find_all("link", href=True):
        rel = " ".join(tag.get("rel") or []).lower()
        if "alternate" in rel and (tag.get("type") or "").lower() in _FEED_TYPES:
            url = urljoin(base_url, tag["href"])
            if url not in out:
                out.append(url)
    return out


def page_text(html: str, url: Optional[str] = None) -> str:
    """The readable main text of a page (trafilatura), or the plain text of the page if trafilatura is missing / finds
    nothing. Menus, cookie banners and footers are left out when trafilatura is available."""
    try:
        import trafilatura
        text = trafilatura.extract(html, url=url, favor_recall=True, include_comments=False, include_tables=True)
        if text and len(text) > 40:
            return text
    except Exception as e:  # noqa: BLE001 - extraction must never break a search
        log.debug("trafilatura failed: %s", e)
    soup = BeautifulSoup(html, "lxml")
    for s in soup(["script", "style", "noscript", "nav", "footer", "header"]):
        s.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ")).strip()


def page_title(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        return re.sub(r"\s+", " ", h1.get_text(" ", strip=True))[:150]
    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content"):
        return og["content"].strip()[:150]
    return re.sub(r"\s+", " ", soup.title.get_text(" ", strip=True))[:150] if soup.title else ""


def vacancy_items(html: str, url: str, company: str = "") -> list[Job]:
    """Every vacancy a careers page lists, whatever the search: schema.org JobPosting data (JSON-LD / microdata / RDFa:
    the full ad with pay, hours, closing date) or, if there is none, the short links / headings that read like a job
    title. Pages that say there are no vacancies give nothing. Matching to a search is done by `matching_vacancies`."""
    from .sources.jsonld import extract_jobpostings, jobposting_to_job
    host = urlparse(url).netloc.lower().removeprefix("www.")
    jobs: list[Job] = []
    for jp in extract_jobpostings(html, url):
        job = jobposting_to_job(jp, url, source=f"site:{host}")
        job.company = job.company or company
        jobs.append(job)
    if jobs:
        return jobs[:60]
    soup = BeautifulSoup(html, "lxml")
    for s in soup(["script", "style", "noscript"]):
        s.decompose()
    if _NO_VACANCIES.search(re.sub(r"\s+", " ", soup.get_text(" "))[:20000]):
        return []
    seen: set[str] = set()
    for el in soup.find_all(["a", "h2", "h3", "h4", "li", "strong"]):
        t = re.sub(r"\s+", " ", el.get_text(" ", strip=True))
        # a link / heading / list item that is a title, not a sentence from the middle of a paragraph
        if not 4 <= len(t) <= 110 or len(t.split()) > 14 or t.lower() in seen:
            continue
        seen.add(t.lower())
        link = el if el.name == "a" else el.find("a", href=True)
        href = urljoin(url, link["href"]) if link is not None and link.get("href") else url
        jobs.append(Job(title=t, company=company or host, url=href, source=f"site:{host}", description=t))
        if len(jobs) >= 120:
            break
    return jobs


def matching_vacancies(items: list[Job], keyword: Optional[str], limit: int = 5) -> list[Job]:
    """The vacancies whose title fits the search ('Practice Nurse', or a locum / bank ad for a locum search)."""
    from .core import keyword_hit
    if not keyword:
        return items[:limit]
    return [j for j in items if keyword_hit(j, keyword)][:limit]


def vacancies_on_page(html: str, url: str, keyword: Optional[str], company: str = "") -> list[Job]:
    return matching_vacancies(vacancy_items(html, url, company), keyword)


def parse_feed(content: bytes, url: str, source: str = "rss") -> list[Job]:
    """Jobs from an RSS / Atom / RDF feed already fetched by our HTTP client."""
    import feedparser
    from .utils import find_email, html_to_text, to_iso_date
    d = feedparser.parse(content)
    feed_title = (d.feed.get("title") or "").strip() if getattr(d, "feed", None) else ""
    host = urlparse(url).netloc.lower().removeprefix("www.")
    jobs: list[Job] = []
    for e in d.entries:
        title = html_to_text(e.get("title") or "").strip()
        link = e.get("link") or ""
        if not title or not link:
            continue
        desc = html_to_text(e.get("summary") or (e.get("content") or [{}])[0].get("value") or "")
        company = (e.get("author") or e.get("publisher") or feed_title or host).strip()
        loc = (e.get("location") or e.get("job_location") or e.get("georss_featurename") or "").strip()
        when = e.get("published") or e.get("updated") or ""
        jobs.append(Job(title=title, company=company, location=loc, description=desc, url=link,
                        source=f"{source}:{host}", posted_date=to_iso_date(str(when)[:25]) if when else None,
                        contact_email=find_email(desc)))
    return jobs
