"""Vacancies straight from the careers pages of local DIRECT employers (law firms, accountants, trade firms, care homes,
home-care providers, restaurants / hotels, schools ...), for jobs no official feed carries.

How: the employer finder (official registers - Legal Aid Agency, CQC, Care Inspectorate, FSA, GIAS ... - plus
OpenStreetMap) lists the employers of this kind of worker near the place; their own websites are read politely
(robots.txt, rate limit, time-boxed) for schema.org JobPosting adverts / job feeds matching the role. Every advert comes
from the employer's own site; recruitment / staffing agencies are never listed (agencies.py).
"""
from __future__ import annotations

import logging
import time
from typing import Iterator

from ..agencies import is_agency_name
from ..models import Job
from .base import Source

log = logging.getLogger(__name__)

# sectors with an official vacancy feed already (NHS Jobs / NHS Scotland / HSC NI) are left to those sources
_HEALTH_ONLY = {"gp", "hospital", "clinic", "urgent_care", "community", "mental_health", "trust", "dental", "pharmacy",
                "independent"}


class EmployerSitesSource(Source):
    name = "employersites"
    description = ("Local direct employers' own careers pages (found from official registers and OpenStreetMap): law "
                   "firms, accountants, trades, care providers, hospitality, schools. No agencies.")
    server_side_location = True   # employers are found around the place already
    budget_seconds = 95.0       # the whole stage (registers + websites); runs alongside the other sources
    radius_miles = 10
    sites = 60

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.info: list[str] = []
        self.stats: dict = {}

    def kinds(self) -> list[str]:
        from ..candidates.employers import employer_kinds
        ks = employer_kinds(self.keyword or "")
        return [] if not ks or set(ks) <= _HEALTH_ONLY else ks

    def _place(self):
        from ..candidates.geo import Place, is_nationwide, nationwide_place
        from .nhs import geocode_place
        loc = (self.location or "").strip()
        if not loc or is_nationwide(loc):
            return nationwide_place()
        got = geocode_place(self.http, loc)
        if not got:
            return None
        return Place(postcode=loc, lat=got[0], lon=got[1], town=loc, region="", country=got[2] or "")

    def fetch(self) -> Iterator[Job]:
        self.info = []
        kw = (self.keyword or "").strip()
        if not kw or not self.kinds():
            self.info.append("not used for this search (an official job feed covers it, or no employer type is known)")
            return
        place = self._place()
        if place is None:
            self.info.append(f"place '{self.location}' not recognised")
            return
        from ..candidates.employers import EmployerLeadsFinder
        finder = EmployerLeadsFinder(locum_adverts=False, practice_sites=self.sites)
        finder.budget_seconds = self.budget_seconds
        t = time.monotonic()
        try:
            leads, _ = finder.find(kw, place, self.radius_miles)
        except Exception as e:  # noqa: BLE001 - a bonus source: never break the search
            log.info("employer sites failed: %s", e)
            self.warnings.append(f"employersites: {e}")
            return
        self.stats = {"employers": len(leads), "sites_read": finder.stats.get("practice_sites_checked", 0),
                      "seconds": round(time.monotonic() - t, 1)}
        seen: set[str] = set()
        n = 0
        for lead, v in finder.site_jobs:
            url = v.get("url") or v.get("page") or ""
            if not url or url in seen or is_agency_name(lead.name):
                continue
            seen.add(url)
            temp = v.get("type", "").startswith("locum")
            job = Job(title=v.get("title", ""), company=lead.name, location=lead.address or lead.postcode or place.town,
                      url=url, source=self.name, salary_raw=v.get("pay") or None,
                      closing_date=v.get("closing_date") or None, employment_type="temporary" if temp else None,
                      contact_email=lead.email or None, contact_phone=lead.phone or None,
                      description=f"Advertised on {lead.name}'s own website ({lead.employer_type}). "
                                  f"Employer found via {lead.source}.")
            yield job
            n += 1
            if self.limit and n >= self.limit:
                break
        self.info.append(f"{len(leads)} local employers found, {self.stats['sites_read']} of their websites read, "
                         f"{n} vacancies on their own sites")
