"""Public NHS job sites of Scotland and Northern Ireland (NHS Jobs covers England and Wales only).

* NHS Scotland Jobs (apply.jobs.scot.nhs.uk, Jobtrain): the public search-results fragment the site's own search page
  loads (/Home/_JobCard?what=…&Skip=…, 12 per page; lat/long/Miles for a place). No robots.txt restrictions; no login.
* HSC Northern Ireland jobs (jobs.hscni.net): the public /Search?keyword=… results page (all results on one page).
  No robots.txt restrictions; no login.
Both are read politely (paced, few pages), parsed from the listing only (title, employer, contract, pay, hours,
closing date) and every job links to the official advert. Neither publishes a feed or API; if their page layout
changes the source returns nothing with a note rather than failing the search.
"""
from __future__ import annotations

import html as htmllib
import logging
import re
import time
from typing import Iterator

import requests

from ..models import Job
from ..salary import find_salary_in_text
from ..utils import normalise_employment_type, to_iso_date
from .base import Source
from .nhs import _NATIONWIDE, LOCUM_KEYWORDS, geocode_place

log = logging.getLogger(__name__)


def _text(s: str) -> str:
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def _terms(keyword: str, temporary_extra: tuple[str, ...] = ()) -> tuple[list[str], bool]:
    from ..candidates.matching import RoleMatcher
    rm = RoleMatcher(keyword or "")
    core = rm.core or (keyword or "")
    terms = [core] + [s for s in rm.search_terms(3)[1:]]
    temp = rm.contract == "temporary"
    if temp:
        terms += LOCUM_KEYWORDS.get(core, []) + list(temporary_extra)
    return list(dict.fromkeys(t for t in terms if t)), temp


class NHSScotlandSource(Source):
    name = "nhsscotland"
    description = "NHS Scotland Jobs (apply.jobs.scot.nhs.uk) public search: NHS boards in Scotland. No key."
    BASE = "https://apply.jobs.scot.nhs.uk"
    max_pages = 6
    budget_seconds = 60
    miles = 15

    def _cards(self, page_html: str) -> list[Job]:
        out = []
        for card in re.split(r'<div[^>]+class="card mb-7', page_html)[1:]:
            m = re.search(r'href="(/Job/JobDetail\?JobId=\d+)"[^>]*>\s*([^<]+?)\s*</a>', card)
            if not m:
                continue
            f = {k.strip().rstrip(":").lower(): _text(v) for k, v in
                 re.findall(r"<strong>\s*([^<]+?)\s*</strong>(.*?)</p>", card, re.S)}
            pay = f.get("salary", "")
            job = Job(title=_text(m.group(2)), company=f.get("employer (nhs board)", ""),
                      location=", ".join(x for x in (f.get("location", ""), f.get("department", ""))
                                         if x and x.lower() != "see below"),
                      url=self.BASE + htmllib.unescape(m.group(1)), source=self.name,
                      employment_type=None if "various" in f.get("employment type", "").lower() else
                      normalise_employment_type(f.get("employment type", "")),
                      posted_date=to_iso_date(f.get("live date")), closing_date=to_iso_date(f.get("closing date")),
                      salary_raw=pay if pay and "see advert" not in pay.lower() else None,
                      hours=f.get("hours per week") and f"{f['hours per week']} hours per week".replace(
                          "hours hours", "hours"),
                      description=" · ".join(f"{k}: {v}" for k, v in f.items() if v))
            sal = find_salary_in_text(pay)
            for k, v in (sal or {}).items():
                if hasattr(job, k) and k != "salary_raw":
                    setattr(job, k, v)
            out.append(job)
        return out

    def fetch(self) -> Iterator[Job]:
        self.info: list[str] = []
        base: dict = {}
        loc = (self.location or "").strip()
        if loc and loc.lower() not in _NATIONWIDE and loc.lower() != "scotland":
            got = geocode_place(self.http, loc)
            if got and got[2] and got[2] != "Scotland":
                self.info.append("covers Scotland only - skipped for this place")
                return
            if got:
                base = {"lat": round(got[0], 4), "long": round(got[1], 4), "Miles": self.miles}
        terms, temp = _terms(self.keyword or "", ("nurse",))
        end, seen = time.monotonic() + self.budget_seconds, set()
        for t in terms:
            for page in range(self.max_pages):
                if time.monotonic() > end:
                    return
                try:
                    r = self.http.get(f"{self.BASE}/Home/_JobCard",
                                      params={"what": t, "Skip": page * 12, **base}, headers={"Accept": "text/html"})
                except requests.RequestException as e:
                    self.warn("nhsscotland", e)
                    return
                jobs = self._cards(r.text)
                total = int((re.search(r'totalMatchRecords"\s+value="(\d+)"', r.text) or [0, 0])[1])
                for j in jobs:
                    if j.url not in seen:
                        seen.add(j.url)
                        yield j
                if not jobs or (page + 1) * 12 >= total:
                    break


class HSCNIJobsSource(Source):
    name = "hscni"
    description = "HSC Northern Ireland jobs (jobs.hscni.net) public search: HSC trusts in Northern Ireland. No key."
    BASE = "https://jobs.hscni.net"

    def _articles(self, page_html: str) -> list[Job]:
        out = []
        for art in re.findall(r'<article class="job-result.*?</article>', page_html, re.S):
            m = re.search(r'<h2>\s*<a[^>]+href="([^"]+)"[^>]*>([^<]+)</a>', art)
            if not m:
                continue
            f = {k.strip().rstrip(":").lower(): _text(v) for k, v in
                 re.findall(r"<li><strong>\s*([^<]+?)\s*</strong>(.*?)</li>", art, re.S)}
            close = re.search(r"<strong>Closing:</strong>\s*([^<]+)", art)
            org = re.search(r'data-category="([^"]+)"', art)
            company = htmllib.unescape(org.group(1)).split(" Band-")[0].replace("-", " ") if org else ""
            pay = f.get("salary", "")
            cd = None
            if close:
                mm = re.search(r"(\d{1,2} [A-Z][a-z]+ \d{4})", close.group(1))
                cd = to_iso_date(mm.group(1)) if mm else None
            job = Job(title=_text(m.group(2)), company=company, location=f.get("location", ""),
                      url=self.BASE + m.group(1) if m.group(1).startswith("/") else m.group(1), source=self.name,
                      employment_type=normalise_employment_type(f.get("contract type", "")), closing_date=cd,
                      salary_raw=pay or None, description=" · ".join(f"{k}: {v}" for k, v in f.items() if v))
            for k, v in (find_salary_in_text(pay) or {}).items():
                if hasattr(job, k) and k != "salary_raw":
                    setattr(job, k, v)
            out.append(job)
        return out

    def fetch(self) -> Iterator[Job]:
        self.info: list[str] = []
        loc = (self.location or "").strip()
        if loc and loc.lower() not in _NATIONWIDE and loc.lower() != "northern ireland":
            got = geocode_place(self.http, loc)
            if got and got[2] and got[2] != "Northern Ireland":
                self.info.append("covers Northern Ireland only - skipped for this place")
                return
        terms, temp = _terms(self.keyword or "", ("nurse",))
        seen = set()
        for t in terms[:6]:
            try:
                r = self.http.get(f"{self.BASE}/Search", params={"keyword": t}, headers={"Accept": "text/html"})
            except requests.RequestException as e:
                self.warn("hscni", e)
                return
            for j in self._articles(r.text):
                if j.url not in seen:
                    seen.add(j.url)
                    yield j
