"""Adapters for public applicant-tracking-system (ATS) job board APIs.

These are the official, public, unauthenticated endpoints that companies use to
publish their own careers pages, so they are the most reliable/allowed sources.
"""
from __future__ import annotations

import logging
from typing import Iterator

import requests

from ..models import Job
from ..salary import find_salary_in_text, parse_salary
from ..utils import detect_remote, find_email, html_to_text, normalise_employment_type, to_iso_date
from .base import Source

log = logging.getLogger(__name__)

_INTERVALS = {"1 YEAR": "year", "per-year-salary": "year", "1 MONTH": "month", "per-month-salary": "month",
              "1 HOUR": "hour", "per-hour-wage": "hour", "1 DAY": "day", "per-day-wage": "day",
              "1 WEEK": "week", "per-week-salary": "week", "year": "year", "hour": "hour", "month": "month"}


def _apply_salary(job: Job, parsed: dict | None) -> None:
    if parsed:
        for k, v in parsed.items():
            setattr(job, k, v)


class GreenhouseSource(Source):
    name = "greenhouse"
    requires = "companies"
    description = "Greenhouse job boards API (boards-api.greenhouse.io) - pass board slugs via --companies"

    def fetch(self) -> Iterator[Job]:
        for slug in self.companies:
            url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
            try:
                data = self.http.get_json(url, params={"content": "true"})
            except requests.RequestException as e:
                self.warn(f"greenhouse:{slug}", e)
                continue
            for j in data.get("jobs", []):
                desc = html_to_text(j.get("content"))
                loc = (j.get("location") or {}).get("name", "")
                emp = None
                for m in j.get("metadata") or []:
                    if m and "employment" in str(m.get("name", "")).lower():
                        emp = normalise_employment_type(m.get("value"))
                job = Job(
                    title=(j.get("title") or "").strip(),
                    company=j.get("company_name") or slug,
                    location=loc,
                    posted_date=to_iso_date(j.get("first_published") or j.get("updated_at")),
                    description=desc,
                    url=j.get("absolute_url", ""),
                    source=f"greenhouse:{slug}",
                    employment_type=emp,
                    remote=detect_remote(loc),
                    contact_email=find_email(desc),
                )
                _apply_salary(job, find_salary_in_text(desc))
                yield job


class LeverSource(Source):
    name = "lever"
    requires = "companies"
    description = "Lever postings API (api.lever.co) - pass company slugs via --companies"

    def fetch(self) -> Iterator[Job]:
        for slug in self.companies:
            url = f"https://api.lever.co/v0/postings/{slug}"
            try:
                data = self.http.get_json(url, params={"mode": "json"})
            except requests.RequestException as e:
                self.warn(f"lever:{slug}", e)
                continue
            if not isinstance(data, list):
                log.warning("lever:%s unexpected response: %s", slug, str(data)[:200])
                continue
            for j in data:
                cats = j.get("categories") or {}
                parts = [j.get("descriptionPlain") or html_to_text(j.get("description"))]
                for lst in j.get("lists") or []:
                    parts.append(f"{lst.get('text', '')}\n{html_to_text(lst.get('content'))}")
                parts.append(j.get("additionalPlain") or "")
                desc = "\n\n".join(p.strip() for p in parts if p and p.strip())
                loc = cats.get("location") or ", ".join(cats.get("allLocations") or [])
                wp = (j.get("workplaceType") or "").lower()
                job = Job(
                    title=(j.get("text") or "").strip(),
                    company=slug,
                    location=loc,
                    posted_date=to_iso_date(j.get("createdAt")),
                    description=desc,
                    url=j.get("hostedUrl", ""),
                    source=f"lever:{slug}",
                    employment_type=normalise_employment_type(cats.get("commitment")),
                    remote=True if wp == "remote" else (False if wp in ("onsite", "on-site") else detect_remote(loc)),
                    contact_email=find_email(desc),
                )
                sr = j.get("salaryRange")
                if sr and (sr.get("min") or sr.get("max")):
                    job.salary_min, job.salary_max = sr.get("min"), sr.get("max")
                    job.salary_currency = sr.get("currency")
                    job.salary_period = _INTERVALS.get(sr.get("interval"), sr.get("interval"))
                    job.salary_raw = f"{sr.get('currency')} {sr.get('min')}-{sr.get('max')} {sr.get('interval')}"
                else:
                    _apply_salary(job, find_salary_in_text(desc))
                yield job


class AshbySource(Source):
    name = "ashby"
    requires = "companies"
    description = "Ashby posting API (api.ashbyhq.com) - pass job board names via --companies"

    def fetch(self) -> Iterator[Job]:
        for org in self.companies:
            url = f"https://api.ashbyhq.com/posting-api/job-board/{org}"
            try:
                data = self.http.get_json(url, params={"includeCompensation": "true"})
            except requests.RequestException as e:
                self.warn(f"ashby:{org}", e)
                continue
            for j in data.get("jobs", []):
                if j.get("isListed") is False:
                    continue
                locs = [j.get("location") or ""] + [s.get("location", "") for s in j.get("secondaryLocations") or []]
                loc = "; ".join(l for l in locs if l)
                desc = j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml"))
                job = Job(
                    title=(j.get("title") or "").strip(),
                    company=org,
                    location=loc,
                    posted_date=to_iso_date(j.get("publishedAt")),
                    description=desc,
                    url=j.get("jobUrl", ""),
                    source=f"ashby:{org}",
                    employment_type=normalise_employment_type(j.get("employmentType")),
                    remote=bool(j.get("isRemote")) if j.get("isRemote") is not None else detect_remote(loc),
                    contact_email=find_email(desc),
                )
                comp = j.get("compensation") or {}
                sal = next((c for c in comp.get("summaryComponents") or []
                            if c.get("compensationType") == "Salary" and (c.get("minValue") or c.get("maxValue"))), None)
                if sal:
                    job.salary_min, job.salary_max = sal.get("minValue"), sal.get("maxValue")
                    job.salary_currency = sal.get("currencyCode")
                    job.salary_period = _INTERVALS.get(sal.get("interval"), sal.get("interval"))
                    job.salary_raw = comp.get("scrapeableCompensationSalarySummary") or comp.get("compensationTierSummary")
                else:
                    _apply_salary(job, parse_salary(comp.get("compensationTierSummary")) or find_salary_in_text(desc))
                yield job
