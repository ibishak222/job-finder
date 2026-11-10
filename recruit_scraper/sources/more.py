"""More no-key sources.

Company career-site APIs (public, published by the employer's ATS for their own careers pages):
  workable        apply.workable.com/api/v1/widget/accounts/<slug>?details=true
  smartrecruiters api.smartrecruiters.com/v1/companies/<CompanyId>/postings   (Posting API, public)
  recruitee       <slug>.recruitee.com/api/offers/

Job-board feeds that invite reuse with attribution + a link to the original ad (kept in `url`):
  himalayas       himalayas.app/jobs/api(/search)      remote jobs, ~100k
  jobicy          jobicy.com/api/v2/remote-jobs        remote jobs ("credit Jobicy with a direct link")
  weworkremotely  weworkremotely.com/remote-jobs.rss   remote jobs (RSS)
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from typing import Iterator

import requests

from ..models import Job
from ..salary import find_salary_in_text
from ..utils import detect_remote, find_email, html_to_text, normalise_employment_type, to_iso_date
from .base import Source

# Himalayas' 'country' filter only takes a country: a town or postcode ('Glasgow', 'M1 1AE') gets HTTP 400
_COUNTRIES = ("united states", "usa", "canada", "ireland", "germany", "france", "spain", "italy", "netherlands",
              "portugal", "poland", "sweden", "denmark", "norway", "finland", "belgium", "switzerland", "austria",
              "australia", "new zealand", "india", "singapore", "south africa", "brazil", "mexico", "japan")


def himalayas_country(location: str) -> str:
    """A country name for Himalayas: a named country passes through; any UK town, region or postcode -> UK."""
    loc = (location or "").strip().lower()
    if loc in _COUNTRIES:
        return "United States" if loc == "usa" else location.strip().title()
    return "United Kingdom"     # this is a UK job finder: towns, counties and postcodes are UK places

log = logging.getLogger(__name__)


def _sal(job: Job, parsed) -> None:
    if parsed:
        for k, v in parsed.items():
            setattr(job, k, v)


class WorkableSource(Source):
    name = "workable"
    requires = "companies"
    description = "Workable public careers widget API - pass account slugs via --companies (workable:huggingface)"

    def fetch(self) -> Iterator[Job]:
        for slug in self.companies:
            try:
                data = self.http.get_json(f"https://apply.workable.com/api/v1/widget/accounts/{slug}",
                                          params={"details": "true"})
            except requests.RequestException as e:
                self.warn(f"workable:{slug}", e)
                continue
            company = data.get("name") or slug
            for j in data.get("jobs", []):
                locs = j.get("locations") or [{"city": j.get("city"), "region": j.get("state"), "country": j.get("country")}]
                loc = "; ".join(", ".join(x for x in (l.get("city"), l.get("region"), l.get("country")) if x) for l in locs)
                desc = html_to_text(j.get("description"))
                job = Job(title=(j.get("title") or "").strip(), company=company, location=loc,
                          posted_date=to_iso_date(j.get("published_on") or j.get("created_at")), description=desc,
                          url=j.get("shortlink") or j.get("url") or "", source=f"workable:{slug}",
                          employment_type=normalise_employment_type(j.get("employment_type")),
                          remote=True if j.get("telecommuting") else detect_remote(loc), contact_email=find_email(desc))
                _sal(job, find_salary_in_text(desc))
                yield job


class SmartRecruitersSource(Source):
    name = "smartrecruiters"
    requires = "companies"
    description = "SmartRecruiters public Posting API - pass company identifiers via --companies (smartrecruiters:Wise)"
    max_per_company = 300
    detail_limit = 60  # full descriptions are one extra request each

    def fetch(self) -> Iterator[Job]:
        for cid in self.companies:
            base = f"https://api.smartrecruiters.com/v1/companies/{cid}/postings"
            offset, details = 0, 0
            while offset < self.max_per_company:
                try:
                    data = self.http.get_json(base, params={"limit": 100, "offset": offset})
                except requests.RequestException as e:
                    self.warn(f"smartrecruiters:{cid}", e)
                    break
                rows = data.get("content") or []
                for p in rows:
                    loc_d = p.get("location") or {}
                    loc = loc_d.get("fullLocation") or ", ".join(x for x in (loc_d.get("city"), loc_d.get("region"),
                                                                             (loc_d.get("country") or "").upper()) if x)
                    desc = ""
                    if details < self.detail_limit and self._wanted(p, loc):
                        details += 1
                        try:
                            d = self.http.get_json(f"{base}/{p['id']}")
                            secs = (d.get("jobAd") or {}).get("sections") or {}
                            desc = "\n\n".join(html_to_text((secs.get(k) or {}).get("text"))
                                               for k in ("companyDescription", "jobDescription", "qualifications",
                                                         "additionalInformation") if secs.get(k))
                        except requests.RequestException:
                            pass
                    job = Job(title=(p.get("name") or "").strip(), company=(p.get("company") or {}).get("name") or cid,
                              location=loc, posted_date=to_iso_date(p.get("releasedDate")), description=desc,
                              url=f"https://jobs.smartrecruiters.com/{cid}/{p.get('id')}", source=f"smartrecruiters:{cid}",
                              employment_type=normalise_employment_type((p.get("typeOfEmployment") or {}).get("label")),
                              remote=bool(loc_d.get("remote")) or detect_remote(loc), contact_email=find_email(desc))
                    _sal(job, find_salary_in_text(desc))
                    yield job
                offset += 100
                if offset >= (data.get("totalFound") or 0):
                    break

    def _wanted(self, p: dict, loc: str) -> bool:
        if self.keyword and self.keyword.lower().split()[0] not in (p.get("name") or "").lower():
            return False
        if self.location and self.location.lower() not in loc.lower():
            return False
        return True


class RecruiteeSource(Source):
    name = "recruitee"
    requires = "companies"
    description = "Recruitee public offers API - pass company subdomains via --companies (recruitee:bunq)"

    def fetch(self) -> Iterator[Job]:
        for slug in self.companies:
            try:
                data = self.http.get_json(f"https://{slug}.recruitee.com/api/offers/")
            except requests.RequestException as e:
                self.warn(f"recruitee:{slug}", e)
                continue
            for o in data.get("offers", []):
                desc = html_to_text((o.get("description") or "") + (o.get("requirements") or ""))
                sal = o.get("salary") or {}
                job = Job(title=(o.get("title") or "").strip(), company=o.get("company_name") or slug,
                          location=o.get("location") or "", posted_date=to_iso_date(o.get("published_at")),
                          description=desc, url=o.get("careers_url") or "", source=f"recruitee:{slug}",
                          employment_type=normalise_employment_type(o.get("employment_type_code")),
                          remote=bool(o.get("remote")) or None,
                          # the employer's own published application mailbox
                          contact_email=find_email(desc) or o.get("mailbox_email"))
                if sal.get("min") or sal.get("max"):
                    job.salary_min, job.salary_max = sal.get("min"), sal.get("max")
                    job.salary_currency, job.salary_period = sal.get("currency"), sal.get("period")
                    job.salary_raw = f"{sal.get('min')}-{sal.get('max')} {sal.get('currency')} {sal.get('period')}"
                else:
                    _sal(job, find_salary_in_text(desc))
                yield job


class HimalayasSource(Source):
    name = "himalayas"
    description = "Himalayas remote-jobs API (~100k remote jobs; uses its search when a keyword is given)."
    max_pages = 10

    def fetch(self) -> Iterator[Job]:
        seen = 0
        cursor = None
        for page in range(self.max_pages):
            if self.keyword:
                url, params = "https://himalayas.app/jobs/api/search", {"q": self.keyword, "page": page + 1}
                if self.location:
                    params["country"] = himalayas_country(self.location)
            else:
                url, params = "https://himalayas.app/jobs/api", {"limit": 20}
                if cursor:
                    params["cursor"] = cursor
            try:
                data = self.http.get_json(url, params=params)
            except requests.RequestException as e:
                self.warn("himalayas", e)
                return
            rows = data.get("jobs") or []
            for j in rows:
                desc = html_to_text(j.get("description"))
                locs = j.get("locationRestrictions") or []
                loc = ", ".join(l if isinstance(l, str) else (l.get("name") or "") for l in locs) or "Remote (worldwide)"
                job = Job(title=(j.get("title") or "").strip(), company=j.get("companyName") or "",
                          location=f"Remote - {loc}" if "Remote" not in loc else loc,
                          posted_date=to_iso_date(j.get("pubDate")), description=desc,
                          url=(j.get("applicationLink") or j.get("guid") or "").split("?utm")[0], source="himalayas",
                          employment_type=normalise_employment_type(j.get("employmentType")), remote=True,
                          contact_email=find_email(desc))
                if j.get("minSalary") or j.get("maxSalary"):
                    job.salary_min, job.salary_max = j.get("minSalary"), j.get("maxSalary")
                    job.salary_currency = j.get("currency")
                    job.salary_period = {"hourly": "hour", "yearly": "year", "annual": "year", "monthly": "month"}.get(
                        str(j.get("salaryPeriod") or "").lower(), "year")
                    job.salary_raw = f"{j.get('currency')} {j.get('minSalary')}-{j.get('maxSalary')} {j.get('salaryPeriod') or ''}".strip()
                else:
                    _sal(job, find_salary_in_text(desc))
                seen += 1
                yield job
            cursor = data.get("nextCursor")
            if not rows or (not self.keyword and not cursor) or (self.limit and seen >= self.limit * 3):
                return


class JobicySource(Source):
    name = "jobicy"
    description = "Jobicy remote-jobs API (credit Jobicy + link to the original ad)."

    def fetch(self) -> Iterator[Job]:
        params = {"count": 100}
        if self.keyword:
            params["tag"] = self.keyword
        if self.location and self.location.lower() in ("uk", "london", "united kingdom", "england", "manchester"):
            params["geo"] = "uk"
        try:
            data = self.http.get_json("https://jobicy.com/api/v2/remote-jobs", params=params)
        except requests.RequestException as e:
            self.warn("jobicy", e)
            return
        for j in data.get("jobs") or []:
            desc = html_to_text(j.get("jobDescription"))
            job = Job(title=html_to_text(j.get("jobTitle")), company=j.get("companyName") or "",
                      location=f"Remote - {j.get('jobGeo') or 'anywhere'}", posted_date=to_iso_date(j.get("pubDate")),
                      description=desc, url=j.get("url") or "", source="jobicy",
                      employment_type=normalise_employment_type(j.get("jobType")), remote=True,
                      contact_email=find_email(desc))
            lo, hi = j.get("salaryMin") or j.get("annualSalaryMin"), j.get("salaryMax") or j.get("annualSalaryMax")
            if lo or hi:
                job.salary_min, job.salary_max = lo, hi
                job.salary_currency = j.get("salaryCurrency")
                job.salary_period = {"yearly": "year", "hourly": "hour", "monthly": "month"}.get(
                    str(j.get("salaryPeriod") or "yearly").lower(), "year")
                job.salary_raw = f"{job.salary_currency} {lo}-{hi} {j.get('salaryPeriod') or ''}".strip()
            else:
                _sal(job, find_salary_in_text(desc))
            yield job


class WeWorkRemotelySource(Source):
    name = "weworkremotely"
    description = "We Work Remotely RSS feed (remote jobs; link back to the original ad)."

    def fetch(self) -> Iterator[Job]:
        try:
            r = self.http.get("https://weworkremotely.com/remote-jobs.rss")
        except requests.RequestException as e:
            self.warn("weworkremotely", e)
            return
        root = ET.fromstring(r.content)
        for it in root.iter("item"):
            title = (it.findtext("title") or "").strip()
            company, _, role = title.partition(":")
            desc = html_to_text(it.findtext("description"))
            region = (it.findtext("region") or "").strip()
            job = Job(title=(role or title).strip(), company=company.strip() if role else "",
                      location=f"Remote - {region}" if region else "Remote", posted_date=to_iso_date(it.findtext("pubDate")),
                      description=desc, url=(it.findtext("link") or "").strip(), source="weworkremotely",
                      employment_type=normalise_employment_type(it.findtext("type")), remote=True,
                      contact_email=find_email(desc))
            _sal(job, find_salary_in_text(desc))
            yield job
