"""GOV.UK Teaching Vacancies (Department for Education): teaching, leadership and school / college support jobs at state
schools in England - the official open API, free, no key.

* Endpoint: https://teaching-vacancies.service.gov.uk/api/v1/jobs.json (schema.org JobPosting, 100 per page, no filters).
* Licence: Open Government Licence v3.0. The API terms only say the data must not be used to charge people a fee for
  contacting / interviewing / hiring someone, which this tool never does. robots.txt allows /api/ and is read with
  Protego, since it uses wildcard rules.
* The API cannot filter, so the pages are read at a modest pace (a public API built for reuse, 0.4s apart), cached for a few
  hours, and filtered here - only for searches that are about school / education roles.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Iterator, Optional

import requests

from ..candidates.cache import DiskCache
from ..models import Job
from ..salary import find_salary_in_text
from ..utils import html_to_text, normalise_employment_type
from .base import Source
from .jsonld import jobposting_to_job
from .nhs import geocode_place

log = logging.getLogger(__name__)

API = "https://teaching-vacancies.service.gov.uk/api/v1/jobs.json"
POSTCODES = "https://api.postcodes.io"
# roles the service carries: teaching, leadership and school / college based support
EDUCATION = re.compile(r"teach|tutor|lectur|school|college|academy|senco|sendco|head ?of|deputy head|pastoral|"
                       r"learning support|cover supervisor|classroom|early years|nursery|\bta\b|\bnqt\b|\bect\b|"
                       r"\bsen\b|special (needs|educational)|midday|lunchtime|caretaker|school business|"
                       r"educational psychologist|safeguarding lead|librarian|exams? officer|invigilator|"
                       r"reprographics|science technician|bursar", re.I)
_CONTRACT = re.compile(r"Contract Type:\s*([A-Za-z][A-Za-z \-]{2,24}?)(?=\s{2,}|\s*(?:Salary|Hours|Location|Required)\b|$)")


def _location(jp: dict) -> str:
    loc = jp.get("jobLocation") or {}
    loc = loc[0] if isinstance(loc, list) and loc else loc
    a = (loc or {}).get("address") or {}
    return ", ".join(str(x) for x in (a.get("addressLocality"), a.get("addressRegion"), a.get("postalCode")) if x)


class TeachingVacanciesSource(Source):
    name = "teachingvacancies"
    description = ("GOV.UK Teaching Vacancies open API (England state schools: teachers, TAs, SENCOs, headteachers, "
                   "school support staff). Open Government Licence, no key.")
    page_pause = 0.4          # seconds between pages of the API
    budget_seconds = 45
    distance = 15
    server_side_location = True   # this source does its own distance filter (below)
    cache_hours = 3

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.info: list[str] = []
        self.cache = DiskCache("teachingvacancies", self.cache_hours * 3600)

    def _get_page(self, page: int) -> Optional[dict]:
        try:
            return self.http.get(API, params={"page": page}, check_robots=True, interval=self.page_pause).json()
        except Exception as e:  # noqa: BLE001 - robots / network / JSON problems end the read, not the search
            if isinstance(e, requests.RequestException):
                self.warn("teachingvacancies", e)
            else:
                self.warnings.append(f"teachingvacancies: {e}")
            return None

    def _pages(self) -> list[dict]:
        """Every posting on the API (cached for a few hours; newest first). The first page says how many there are; the
        rest are read a few at a time, still one request every `page_pause` seconds."""
        cached = self.cache.get("all")
        if cached:
            return cached
        from concurrent.futures import ThreadPoolExecutor, wait
        first = self._get_page(1)
        if not first:
            return []
        total = int((first.get("meta") or {}).get("totalPages") or 1)
        results = {1: first.get("data") or []}
        end = time.monotonic() + self.budget_seconds
        pool = ThreadPoolExecutor(max_workers=4)
        futs = {pool.submit(self._get_page, p): p for p in range(2, total + 1)}
        done, pending = wait(futs, timeout=max(1.0, end - time.monotonic()))
        pool.shutdown(wait=False, cancel_futures=True)
        for f in done:
            d = f.result()
            if d:
                results[futs[f]] = d.get("data") or []
        if len(results) < total:
            self.info.append(f"read {len(results)} of {total} pages of Teaching Vacancies (time limit)")
        out: list[dict] = []
        for page in sorted(results):
            for jp in results[page]:
                jp["description"] = (jp.get("description") or "")[:3500]      # keep the cache small
                out.append(jp)
        if out:
            self.cache.set("all", out)
        return out

    def _near_outcodes(self) -> Optional[set[str]]:
        loc = (self.location or "").strip()
        if not loc or loc.lower() in ("uk", "united kingdom", "gb", "great britain", "england", "remote"):
            return None
        got = geocode_place(self.http, loc)
        if not got:
            return None
        try:
            rows = self.http.get_json(f"{POSTCODES}/outcodes", params={
                "lon": got[1], "lat": got[0], "radius": int(min(self.distance * 1609.344, 25000)),
                "limit": 100}).get("result") or []
        except requests.RequestException:
            return None
        return {r["outcode"].upper() for r in rows}

    def fetch(self) -> Iterator[Job]:
        self.info = []
        kw = (self.keyword or "").strip()
        if not kw or not EDUCATION.search(kw):
            self.info.append("Teaching Vacancies only carries school / college jobs - skipped for this search")
            return
        from ..core import keyword_hit
        outcodes = self._near_outcodes()
        loc = (self.location or "").strip().lower()
        n = 0
        for jp in self._pages():
            job = jobposting_to_job(jp, API, source=self.name)
            if not keyword_hit(job, kw):
                continue
            job.location = _location(jp) or job.location
            if outcodes is not None:
                pc = ((jp.get("jobLocation") or {}).get("address") or {}).get("postalCode") if isinstance(
                    jp.get("jobLocation"), dict) else None
                out = (pc or "").upper().replace(" ", "")
                out = out[:-3] if len(out) > 4 else out
                if pc and out not in outcodes:
                    continue
                if not pc and loc not in job.location.lower():
                    continue
            text = html_to_text(jp.get("description"))
            m = _CONTRACT.search(text)
            if m:
                job.employment_type = normalise_employment_type(m.group(1).strip()) or job.employment_type
            raw = (((jp.get("baseSalary") or {}).get("value") or {}).get("value") if isinstance(jp.get("baseSalary"), dict)
                   and isinstance(jp["baseSalary"].get("value"), dict) else None)
            if raw:
                job.salary_raw = str(raw)
                for k, v in (find_salary_in_text(str(raw)) or {}).items():
                    if hasattr(job, k) and k != "salary_raw":
                        setattr(job, k, v)
            org = jp.get("hiringOrganization") or {}
            if isinstance(org, dict) and org.get("sameAs"):
                job.description = (job.description + f"\n\nEmployer website: {org['sameAs']}").strip()
            yield job
            n += 1
            if self.limit and n >= self.limit:
                break
