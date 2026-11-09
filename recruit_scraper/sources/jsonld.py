"""Generic career-page adapter: extracts schema.org JobPosting data (JSON-LD, microdata, RDFa) from any URL.

Most careers sites embed JobPosting structured data for Google for Jobs, so this
works on many company pages without site-specific code. robots.txt is honoured
for every URL fetched, and pages are fetched with a polite rate limit.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterator, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ..http import RobotsDisallowed
from ..models import Job
from ..salary import find_salary_in_text
from ..utils import detect_remote, find_email, html_to_text, normalise_employment_type, to_iso_date
from .base import Source

log = logging.getLogger(__name__)

_UNIT = {"YEAR": "year", "MONTH": "month", "WEEK": "week", "DAY": "day", "HOUR": "hour",
         "ANNUM": "year", "ANNUAL": "year", "YEARLY": "year", "HOURLY": "hour"}
_JOB_LINK_RE = re.compile(r"/(jobs?|careers?|positions?|vacanc(y|ies)|openings?|roles?)/", re.I)


def _walk(node: Any) -> Iterator[dict]:
    """Yield every dict in a JSON-LD structure (handles @graph, lists, nesting)."""
    if isinstance(node, list):
        for n in node:
            yield from _walk(n)
    elif isinstance(node, dict):
        yield node
        for key in ("@graph", "itemListElement", "item", "mainEntity"):
            if key in node:
                yield from _walk(node[key])


def _is_jobposting(d: dict) -> bool:
    t = d.get("@type")
    types = t if isinstance(t, list) else [t]
    return any(str(x).split("/")[-1] == "JobPosting" for x in types if x)


def _loads_lenient(text: str) -> Any:
    text = text.strip().rstrip(";")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # common breakages: raw newlines/tabs inside strings, trailing commas
        fixed = re.sub(r"[\r\n\t]+", " ", text)
        fixed = re.sub(r",\s*([}\]])", r"\1", fixed)
        return json.loads(fixed)


def _extract_jsonld_lenient(html: str) -> list[dict]:
    """Our own JSON-LD reader; tolerant of common breakage (raw newlines, trailing commas)."""
    soup = BeautifulSoup(html, "lxml")
    out: list[dict] = []
    for tag in soup.find_all("script", attrs={"type": re.compile(r"application/ld\+json", re.I)}):
        raw = tag.string or tag.get_text() or ""
        if not raw.strip():
            continue
        try:
            data = _loads_lenient(raw)
        except Exception as e:
            log.debug("bad JSON-LD block skipped: %s", e)
            continue
        out.extend(d for d in _walk(data) if _is_jobposting(d))
    return out


def _extract_with_extruct(html: str, base_url: Optional[str]) -> list[dict]:
    """Microdata / RDFa JobPostings via extruct (BSD-3, scrapinghub/extruct). Optional dependency."""
    try:
        import extruct
    except ImportError:
        return []
    try:
        data = extruct.extract(html, base_url=base_url, syntaxes=["microdata", "rdfa"], uniform=True, errors="ignore")
    except Exception as e:  # noqa: BLE001
        log.debug("extruct failed: %s", e)
        return []
    out: list[dict] = []
    for syntax in ("microdata", "rdfa"):
        for item in data.get(syntax) or []:
            for d in _walk(item):
                if _is_jobposting(d):
                    out.append(_flatten_rdfa(d) if syntax == "rdfa" else d)
    return out


def _flatten_rdfa(d: dict) -> dict:
    """extruct's uniform RDFa output uses full IRIs and [{'@value': ..}] lists; map to plain schema.org keys."""
    flat: dict = {}
    for k, v in d.items():
        key = k.rsplit("/", 1)[-1] if k.startswith("http") else k
        if isinstance(v, list) and len(v) == 1 and isinstance(v[0], dict) and "@value" in v[0]:
            v = v[0]["@value"]
        flat[key] = v
    return flat


def extract_jobpostings(html: str, base_url: Optional[str] = None) -> list[dict]:
    """Return all schema.org JobPosting objects in the page: JSON-LD, plus microdata/RDFa via extruct."""
    found = _extract_jsonld_lenient(html) + _extract_with_extruct(html, base_url)
    seen, out = set(), []
    for jp in found:
        key = (str(jp.get("title") or jp.get("name") or "").strip().lower(), str(jp.get("url") or ""))
        if key not in seen:
            seen.add(key)
            out.append(jp)
    return out


def _name(v: Any) -> str:
    if isinstance(v, dict):
        return str(v.get("name") or "")
    if isinstance(v, list) and v:
        return _name(v[0])
    return str(v or "")


def _location(jp: dict) -> str:
    locs = jp.get("jobLocation")
    if not locs:
        req = jp.get("applicantLocationRequirements")
        return ", ".join(_name(r) for r in (req if isinstance(req, list) else [req]) if r) if req else ""
    parts = []
    for loc in locs if isinstance(locs, list) else [locs]:
        if isinstance(loc, str):
            parts.append(loc)
            continue
        addr = (loc or {}).get("address") or {}
        if isinstance(addr, str):
            parts.append(addr)
            continue
        country = addr.get("addressCountry")
        country = _name(country) if isinstance(country, (dict, list)) else country
        bits = [addr.get("addressLocality"), addr.get("addressRegion"), country]
        s = ", ".join(str(b) for b in bits if b)
        if s:
            parts.append(s)
        elif loc.get("name"):
            parts.append(loc["name"])
    seen, uniq = set(), []
    for p in parts:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return "; ".join(uniq)


def _salary(jp: dict) -> Optional[dict]:
    bs = jp.get("baseSalary") or jp.get("estimatedSalary")
    if isinstance(bs, list):
        bs = bs[0] if bs else None
    if not bs:
        return None
    if isinstance(bs, (str, int, float)):
        from ..salary import parse_salary
        return parse_salary(str(bs))
    currency = bs.get("currency") or bs.get("salaryCurrency") or jp.get("salaryCurrency")
    val = bs.get("value", bs)
    unit = None
    lo = hi = None
    if isinstance(val, dict):
        unit = val.get("unitText")
        lo = val.get("minValue")
        hi = val.get("maxValue")
        if lo is None and hi is None and val.get("value") is not None:
            lo = hi = val.get("value")
    else:
        lo = hi = val
    unit = unit or bs.get("unitText")

    def num(x):
        try:
            return float(str(x).replace(",", "")) if x not in (None, "") else None
        except ValueError:
            return None

    lo, hi = num(lo), num(hi)
    if lo is None and hi is None:
        return None
    period = _UNIT.get(str(unit).upper(), str(unit).lower() if unit else None)
    raw = f"{currency or ''} {lo if lo is not None else ''}-{hi if hi is not None else ''} {unit or ''}".strip()
    return {"salary_min": lo, "salary_max": hi, "salary_currency": currency,
            "salary_period": period, "salary_raw": raw}


def jobposting_to_job(jp: dict, page_url: str, source: str = "jsonld") -> Job:
    desc = html_to_text(jp.get("description"))
    loc = _location(jp)
    loc_type = str(jp.get("jobLocationType") or "")
    remote = True if "TELECOMMUTE" in loc_type.upper() else detect_remote(loc)
    url = jp.get("url") or page_url
    if isinstance(url, list):
        url = url[0]
    if not urlparse(str(url)).scheme:
        url = urljoin(page_url, str(url))
    job = Job(
        title=html_to_text(str(jp.get("title") or jp.get("name") or "")),
        company=_name(jp.get("hiringOrganization")),
        location=loc or ("Remote" if remote else ""),
        posted_date=to_iso_date(jp.get("datePosted")),
        description=desc,
        url=str(url),
        source=source,
        employment_type=normalise_employment_type(jp.get("employmentType")),
        remote=remote,
        contact_email=find_email(desc),
    )
    sal = _salary(jp) or find_salary_in_text(desc)
    if sal:
        for k, v in sal.items():
            setattr(job, k, v)
    return job


class JsonLdSource(Source):
    name = "jsonld"
    requires = "urls"
    description = ("Generic careers-page adapter: reads schema.org JobPosting JSON-LD from --urls. "
                   "If a URL is a listing page with no JobPosting data, follows same-site job links (up to --follow-links).")
    follow_links: int = 25

    def _fetch_html(self, url: str) -> Optional[str]:
        try:
            r = self.http.get(url, check_robots=True, headers={"Accept": "text/html,application/xhtml+xml"})
            return r.text
        except RobotsDisallowed:
            self.warnings.append(f"{url}: the site's robots.txt does not allow scraping - skipped")
        except requests.RequestException as e:
            self.warn(url, e)
        return None

    def _job_links(self, html: str, base: str) -> list[str]:
        soup = BeautifulSoup(html, "lxml")
        host = urlparse(base).netloc
        links: list[str] = []
        for a in soup.find_all("a", href=True):
            href = urljoin(base, a["href"]).split("#")[0]
            p = urlparse(href)
            jobsy_host = re.match(r"(jobs|careers|apply|boards)\.", host or "") and p.path.count("/") >= 2
            if p.netloc == host and (_JOB_LINK_RE.search(p.path) or jobsy_host) \
                    and href.rstrip("/") != base.rstrip("/") and href not in links:
                links.append(href)
        return links

    def fetch(self) -> Iterator[Job]:
        for url in self.urls:
            html = self._fetch_html(url)
            if html is None:
                continue
            postings = extract_jobpostings(html, url)
            host = urlparse(url).netloc
            if postings:
                for jp in postings:
                    yield jobposting_to_job(jp, url, source=f"jsonld:{host}")
                continue
            links = self._job_links(html, url)[: self.follow_links]
            log.info("jsonld: no JobPosting on %s; following %d job links", url, len(links))
            for link in links:
                sub = self._fetch_html(link)
                if not sub:
                    continue
                for jp in extract_jobpostings(sub, link):
                    yield jobposting_to_job(jp, link, source=f"jsonld:{host}")
