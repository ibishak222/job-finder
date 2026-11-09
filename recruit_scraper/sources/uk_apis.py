"""UK job-board APIs that need a (free) API key. Skipped gracefully when no key is set.

- Reed:   register at https://www.reed.co.uk/developers/jobseeker  -> REED_API_KEY
- Adzuna: register at https://developer.adzuna.com/                -> ADZUNA_APP_ID + ADZUNA_APP_KEY
          (ADZUNA_COUNTRY defaults to 'gb')
"""
from __future__ import annotations

import logging
from typing import Iterator

import requests

from ..models import Job
from ..salary import find_salary_in_text
from ..utils import detect_remote, find_email, html_to_text, normalise_employment_type, to_iso_date
from .base import Source

log = logging.getLogger(__name__)


class ReedSource(Source):
    name = "reed"
    requires = "api_key"
    description = "Reed.co.uk Jobseeker API (UK). Needs REED_API_KEY."

    def is_configured(self):
        if not self.key("REED_API_KEY"):
            return False, "add a Reed API key (free at reed.co.uk/developers/jobseeker)"
        return True, ""

    def fetch(self) -> Iterator[Job]:
        key = self.key("REED_API_KEY")
        take = min(self.limit or 100, 100)
        params = {"resultsToTake": take, "resultsToSkip": 0}
        if self.keyword:
            params["keywords"] = self.keyword
        if self.location:
            params["locationName"] = self.location
        try:
            data = self.http.get_json("https://www.reed.co.uk/api/1.0/search", params=params, auth=(key, ""))
        except requests.RequestException as e:
            self.warn("reed", e)
            return
        for j in data.get("results", []):
            desc = html_to_text(j.get("jobDescription"))
            job = Job(
                title=(j.get("jobTitle") or "").strip(),
                company=j.get("employerName") or "",
                location=j.get("locationName") or "",
                posted_date=to_iso_date(j.get("date")),
                description=desc,
                url=j.get("jobUrl") or "",
                source="reed",
                remote=detect_remote(j.get("locationName"), j.get("jobTitle")),
                contact_email=find_email(desc),
            )
            lo, hi = j.get("minimumSalary"), j.get("maximumSalary")
            if lo or hi:
                job.salary_min, job.salary_max = lo, hi
                job.salary_currency = j.get("currency") or "GBP"
                job.salary_period = "year" if (hi or lo or 0) >= 5000 else ("day" if (hi or lo) >= 100 else "hour")
                job.salary_raw = f"£{lo}-£{hi}"
            else:
                sal = find_salary_in_text(desc)
                if sal:
                    for k, v in sal.items():
                        setattr(job, k, v)
            yield job


class AdzunaSource(Source):
    name = "adzuna"
    requires = "api_key"
    description = "Adzuna API (UK by default). Needs ADZUNA_APP_ID and ADZUNA_APP_KEY; ADZUNA_COUNTRY (default gb)."

    def is_configured(self):
        if not (self.key("ADZUNA_APP_ID") and self.key("ADZUNA_APP_KEY")):
            return False, "add an Adzuna app ID + key (free at developer.adzuna.com)"
        return True, ""

    def fetch(self) -> Iterator[Job]:
        country = (self.key("ADZUNA_COUNTRY") or "gb").lower()
        currency = {"gb": "GBP", "us": "USD", "de": "EUR", "fr": "EUR", "ca": "CAD", "au": "AUD"}.get(country)
        params = {"app_id": self.key("ADZUNA_APP_ID"), "app_key": self.key("ADZUNA_APP_KEY"),
                  "results_per_page": min(self.limit or 50, 50), "content-type": "application/json"}
        if self.keyword:
            params["what"] = self.keyword
        if self.location:
            params["where"] = self.location
        try:
            data = self.http.get_json(f"https://api.adzuna.com/v1/api/jobs/{country}/search/1", params=params)
        except requests.RequestException as e:
            self.warn("adzuna", e)
            return
        for j in data.get("results", []):
            desc = html_to_text(j.get("description"))
            loc = (j.get("location") or {}).get("display_name", "")
            job = Job(
                title=html_to_text(j.get("title")),
                company=(j.get("company") or {}).get("display_name", ""),
                location=loc,
                posted_date=to_iso_date(j.get("created")),
                description=desc,
                url=j.get("redirect_url", ""),
                source="adzuna",
                employment_type=normalise_employment_type(j.get("contract_time") or j.get("contract_type")),
                remote=detect_remote(loc, j.get("title")),
                contact_email=find_email(desc),
            )
            lo, hi = j.get("salary_min"), j.get("salary_max")
            if (lo or hi) and str(j.get("salary_is_predicted", "0")) != "1":
                job.salary_min, job.salary_max = lo, hi
                job.salary_currency, job.salary_period = currency, "year"
                job.salary_raw = f"{lo}-{hi} {currency}"
            else:
                sal = find_salary_in_text(desc)
                if sal:
                    for k, v in sal.items():
                        setattr(job, k, v)
            yield job
