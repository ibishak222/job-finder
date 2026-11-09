"""Adapters for public job-board APIs (Remotive, Arbeitnow, RemoteOK).

Each of these publishes a free public API whose terms ask that you credit and link
back to them - the `url` field always points at the original listing.
"""
from __future__ import annotations

import logging
from typing import Iterator

import requests

from ..models import Job
from ..salary import find_salary_in_text, parse_salary
from ..utils import find_email, html_to_text, normalise_employment_type, to_iso_date
from .base import Source

log = logging.getLogger(__name__)


class RemotiveSource(Source):
    name = "remotive"
    description = "Remotive public API (remote jobs). Terms: link back & credit Remotive; max ~4 calls/day."

    def fetch(self) -> Iterator[Job]:
        params = {}
        if self.keyword:
            params["search"] = self.keyword
        if self.limit:
            params["limit"] = max(self.limit * 3, 50)
        try:
            data = self.http.get_json("https://remotive.com/api/remote-jobs", params=params)
        except requests.RequestException as e:
            self.warn("remotive", e)
            return
        for j in data.get("jobs", []):
            desc = html_to_text(j.get("description"))
            job = Job(
                title=(j.get("title") or "").strip(),
                company=j.get("company_name") or "",
                location=j.get("candidate_required_location") or "Remote",
                posted_date=to_iso_date(j.get("publication_date")),
                description=desc,
                url=j.get("url", ""),
                source="remotive",
                employment_type=normalise_employment_type(j.get("job_type")),
                remote=True,
                contact_email=find_email(desc),
            )
            parsed = parse_salary(j.get("salary")) if j.get("salary") else find_salary_in_text(desc)
            if parsed:
                for k, v in parsed.items():
                    setattr(job, k, v)
            yield job


class ArbeitnowSource(Source):
    name = "arbeitnow"
    description = "Arbeitnow public job board API (mostly Germany/EU + remote)."
    max_pages = 3

    def fetch(self) -> Iterator[Job]:
        url = "https://www.arbeitnow.com/api/job-board-api"
        for page in range(1, self.max_pages + 1):
            try:
                data = self.http.get_json(url, params={"page": page})
            except requests.RequestException as e:
                self.warn(f"arbeitnow page {page}", e)
                return
            rows = data.get("data") or []
            for j in rows:
                desc = html_to_text(j.get("description"))
                job = Job(
                    title=(j.get("title") or "").strip(),
                    company=j.get("company_name") or "",
                    location=j.get("location") or "",
                    posted_date=to_iso_date(j.get("created_at")),
                    description=desc,
                    url=j.get("url", ""),
                    source="arbeitnow",
                    employment_type=normalise_employment_type(j.get("job_types")),
                    remote=bool(j.get("remote")),
                    contact_email=find_email(desc),
                )
                parsed = find_salary_in_text(desc)
                if parsed:
                    for k, v in parsed.items():
                        setattr(job, k, v)
                yield job
            if not rows or not (data.get("links") or {}).get("next"):
                return
            # Stop paging early if we already have plenty for the caller.
            if self.limit and page * 100 >= self.limit * 5:
                return


class RemoteOKSource(Source):
    name = "remoteok"
    description = "RemoteOK public API (remote jobs). Terms: link back to the listing and credit Remote OK."

    def fetch(self) -> Iterator[Job]:
        try:
            data = self.http.get_json("https://remoteok.com/api")
        except requests.RequestException as e:
            self.warn("remoteok", e)
            return
        for j in data:
            if not isinstance(j, dict) or "position" not in j:
                continue  # first element is the legal notice
            desc = html_to_text(j.get("description"))
            job = Job(
                title=(j.get("position") or "").strip(),
                company=j.get("company") or "",
                location=j.get("location") or "Remote",
                posted_date=to_iso_date(j.get("date") or j.get("epoch")),
                description=desc,
                url=j.get("url", ""),
                source="remoteok",
                remote=True,
                contact_email=find_email(desc),
            )
            smin, smax = j.get("salary_min") or 0, j.get("salary_max") or 0
            if smin or smax:
                job.salary_min, job.salary_max = (smin or None), (smax or None)
                job.salary_currency, job.salary_period = "USD", "year"
                job.salary_raw = f"${smin}-${smax}"
            else:
                parsed = find_salary_in_text(desc)
                if parsed:
                    for k, v in parsed.items():
                        setattr(job, k, v)
            yield job
