"""NHS Jobs public search feed (England & Wales NHS and NHS-funded employers).

Uses the keyless XML search feed NHSBSA documents as its "Self-Serve API (XML/RSS feeds)"
(https://www.nhsbsa.nhs.uk/about-nhs-jobs/nhs-jobs-integration-and-benefits): same filters as the NHS Jobs search.
robots.txt sets no restrictions. Requests are paced (1 per second by default), at most `limit` results, and every
job links back to the official advert. The feed's <type> gives the contract: Permanent, Fixed-Term, Bank, Locum…
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from typing import Iterator

import requests

from ..models import Job
from ..salary import find_salary_in_text
from ..utils import detect_remote, html_to_text, normalise_employment_type, to_iso_date
from .base import Source

log = logging.getLogger(__name__)
FEED = "https://www.jobs.nhs.uk/api/v1/search_xml"


class NHSJobsSource(Source):
    name = "nhsjobs"
    description = "NHS Jobs public XML search feed (UK healthcare: nurses, HCAs, admin, doctors…). No key."
    max_pages = 10
    server_side_location = True  # the feed already searches within `distance` miles of the place

    def fetch(self) -> Iterator[Job]:
        params = {}
        if self.keyword:
            params["keyword"] = self.keyword
        if self.location:
            params["location"] = self.location
            params["distance"] = 15
        want = self.limit or 100
        n = 0
        for page in range(1, self.max_pages + 1):
            params["page"] = page
            try:
                r = self.http.get(FEED, params=params, headers={"Accept": "application/xml"})
                root = ET.fromstring(r.content)
            except (requests.RequestException, ET.ParseError) as e:
                self.warn("nhsjobs", e)
                return
            items = root.findall("vacancyDetails")
            for v in items:
                g = lambda k: (v.findtext(k) or "").strip()  # noqa: E731
                desc = html_to_text(g("description"))
                locs = [l.text.strip() for l in v.findall("locations/location") if l.text]
                job = Job(title=g("title"), company=g("employer"), location="; ".join(locs),
                          posted_date=to_iso_date(g("postDate")[:10]), description=desc, url=g("url"),
                          source="nhsjobs", employment_type=normalise_employment_type(g("type")),
                          remote=detect_remote(" ".join(locs), g("title")), salary_raw=g("salary") or None)
                sal = find_salary_in_text(g("salary"))
                if sal:
                    for k, v in sal.items():
                        if hasattr(job, k) and k != "salary_raw":
                            setattr(job, k, v)
                yield job
                n += 1
                if n >= want:
                    return
            total = int(root.findtext("totalPages") or 0)
            if not items or page >= total:
                return
