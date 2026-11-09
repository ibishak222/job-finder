"""Search engine shared by the CLI and the web UI."""
from __future__ import annotations

import logging
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed
from itertools import zip_longest
from typing import Callable, Optional

from .http import HttpClient
from .models import Job
from .sources import SOURCES

log = logging.getLogger("recruit_scraper")

ATS = {"greenhouse", "lever", "ashby", "workable", "smartrecruiters", "recruitee"}
DEFAULT_SOURCES = ["remotive", "arbeitnow", "remoteok", "himalayas", "jobicy", "weworkremotely"]


def enrich_contacts(job: Job) -> None:
    """Fill contact_email / contact_phone from the job description when the employer published them there."""
    from .utils import find_email, find_phone
    if not job.contact_email:
        job.contact_email = find_email(job.description)
    if not job.contact_phone:
        job.contact_phone = find_phone(job.description)


_REMOTE_OPEN_UK = re.compile(r"\b(uk|u\.k\.|united kingdom|britain|england|scotland|wales|europe|emea|"
                             r"worldwide|anywhere|global|international)\b", re.I)
_UK_PLACES = re.compile(r"\b(uk|united kingdom|england|scotland|wales|london|manchester|birmingham|leeds|glasgow|"
                        r"edinburgh|bristol|liverpool|sheffield|newcastle|nottingham|cardiff|belfast|cambridge|oxford|"
                        r"reading|brighton|leicester|southampton|milton keynes|york|bath|coventry)\b", re.I)


def matches(job: Job, keywords: list[str], locations: list[str], remote_uk: bool = False) -> bool:
    if keywords:
        hay = f"{job.title}\n{job.company}\n{job.description}".lower()
        # each keyword is OR'ed; the words within one keyword must all appear
        if not any(all(w in hay for w in k.lower().split()) for k in keywords):
            return False
    if locations:
        loc = (job.location or "").lower()
        hit = any((l.lower() == "remote" and job.remote) or l.lower() in loc for l in locations)
        # remote roles open to UK-based people count as matches for a UK place filter
        if not hit and remote_uk and job.remote and any(_UK_PLACES.search(l) for l in locations):
            hit = bool(_REMOTE_OPEN_UK.search(loc)) or loc.strip() in ("remote", "remote (worldwide)", "remote - anywhere")
        if not hit:
            return False
    return True


@dataclass
class SourceResult:
    name: str
    status: str  # ok | skipped | error
    fetched: int = 0
    matched: int = 0
    message: str = ""


@dataclass
class SearchResult:
    jobs: list[Job] = field(default_factory=list)
    sources: list[SourceResult] = field(default_factory=list)


ProgressFn = Callable[[str, str, SourceResult | None], None]  # (source, event, result)


def search(sources: list[str], *, keywords: list[str] = (), locations: list[str] = (),
           companies: list[str] = (), urls: list[str] = (), max_results: int = 200,
           max_per_source: int = 0, delay: float = 1.0, follow_links: int = 25,
           user_agent: Optional[str] = None, salary_only: bool = False, remote_only: bool = False,
           hide_contacts: bool = False, api_keys: Optional[dict] = None, workers: int = 8, remote_uk: bool = True,
           progress: Optional[ProgressFn] = None, cancel=None) -> SearchResult:
    """Run the selected sources and return de-duplicated, filtered, interleaved jobs.

    `cancel` is an optional threading.Event; when set, sources stop at the next job and the partial results return."""
    keywords, locations = list(keywords), list(locations)
    http = HttpClient(user_agent=user_agent, min_interval=delay)
    kw_hint = keywords[0] if len(keywords) == 1 else None
    loc_hint = locations[0] if len(locations) == 1 and locations[0].lower() != "remote" else None
    cap = max_per_source or None

    buckets: "OrderedDict[str, list[Job]]" = OrderedDict((n, []) for n in sources)
    result = SearchResult()
    notify = progress or (lambda *a: None)

    def run_source(name: str) -> tuple[SourceResult, list[Job]]:
        """Runs in a worker thread: fetch + per-job enrichment/filtering (no de-dup across sources here)."""
        src_companies = []
        if name in ATS:
            for c in companies:
                if ":" in c:
                    ats, slug = c.split(":", 1)
                    if ats.strip().lower() == name:
                        src_companies.append(slug.strip())
                elif name in ("greenhouse", "lever", "ashby"):
                    src_companies.append(c)
        src = SOURCES[name](http, keyword=kw_hint, location=loc_hint, companies=src_companies, urls=urls,
                            limit=cap or (max_results or None), api_keys=api_keys)
        if hasattr(src, "follow_links"):
            src.follow_links = follow_links
        ok, why = src.is_configured()
        if not ok:
            return SourceResult(name, "skipped", message=why), []
        r = SourceResult(name, "ok")
        kept: list[Job] = []
        try:
            for job in src.fetch():
                if cancel is not None and cancel.is_set():
                    r.message = "cancelled"
                    break
                r.fetched += 1
                if not job.url or not job.title:
                    continue
                if not matches(job, keywords, locations, remote_uk):
                    continue
                if salary_only and not (job.salary_min or job.salary_max):
                    continue
                if remote_only and not job.remote:
                    continue
                enrich_contacts(job)
                kept.append(job)
                if cap and len(kept) >= cap:
                    break
        except Exception as e:  # one broken adapter must not kill the run
            log.exception("%s crashed", name)
            r.status, r.message = "error", str(e)
        if src.warnings:
            r.message = "; ".join(src.warnings[:5]) + (f" (+{len(src.warnings) - 5} more)" if len(src.warnings) > 5 else "")
            if r.fetched == 0 and r.status == "ok":
                r.status = "error"
        elif r.status == "ok" and r.fetched == 0:
            r.message = "no jobs returned"
        if name == "jsonld" and r.status == "ok" and r.fetched == 0:
            r.message = "no job data found on those pages (the site may not publish schema.org JobPosting data)"
        return r, kept

    valid = []
    for name in sources:
        if name not in SOURCES:
            r = SourceResult(name, "error", message=f"unknown source '{name}'")
            result.sources.append(r)
            notify(name, "done", r)
        else:
            valid.append(name)
            notify(name, "start", None)
    # Different sources hit different hosts, so run them in parallel; per-host politeness is kept by HttpClient.
    by_name: dict[str, SourceResult] = {}
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(valid) or 1))) as ex:
        futs = {ex.submit(run_source, n): n for n in valid}
        for fut in as_completed(futs):
            name = futs[fut]
            r, kept = fut.result()
            by_name[name] = r
            buckets[name] = kept
            notify(name, "done", r)
    # de-duplicate across sources in the user's source order
    seen: set[str] = set()
    for name in valid:
        uniq = []
        for job in buckets.get(name, []):
            key = job.url.split("?")[0].rstrip("/").lower()
            if key not in seen:
                seen.add(key)
                uniq.append(job)
        buckets[name] = uniq
        r = by_name[name]
        if r.status == "ok":
            r.matched = len(uniq)
        result.sources.append(r)

    jobs = [j for group in zip_longest(*buckets.values()) for j in group if j is not None]
    if max_results:
        jobs = jobs[:max_results]
    if hide_contacts:
        for j in jobs:
            j.contact_email = None
            j.contact_phone = None
    result.jobs = jobs
    return result


def default_sources(companies: list[str], urls: list[str]) -> list[str]:
    selected = list(DEFAULT_SOURCES)
    prefixed = {c.split(":", 1)[0] for c in companies if ":" in c}
    if companies:
        selected += [s for s in ("greenhouse", "lever", "ashby", "workable", "smartrecruiters", "recruitee")
                     if s in prefixed or (not prefixed and s in ("greenhouse", "lever", "ashby"))]
    if urls:
        selected.append("jsonld")
    return selected + ["reed", "adzuna"]
