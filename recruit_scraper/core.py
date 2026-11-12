"""Search engine shared by the CLI and the web UI."""
from __future__ import annotations

import logging
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor, as_completed
from itertools import zip_longest
from typing import Callable, Optional

from .http import HttpClient
from .models import Job
from .sources import SOURCES
from .utils import contract_bucket, detect_employment_type

_CONTRACT_WORDS = {"locum": "temporary", "bank": "temporary", "agency": "temporary", "temp": "temporary",
                   "temporary": "temporary", "contract": "temporary", "contractor": "temporary",
                   "freelance": "temporary", "interim": "temporary", "permanent": "permanent", "perm": "permanent"}


def split_contract_words(keywords: list[str]) -> tuple[list[str], str]:
    """Pull contract words out of keyword phrases: ['locum nurse'] -> (['nurse'], 'temporary')."""
    out, kinds = [], set()
    for k in keywords:
        words = k.split()
        rest = [w for w in words if w.lower() not in _CONTRACT_WORDS]
        kinds |= {_CONTRACT_WORDS[w.lower()] for w in words if w.lower() in _CONTRACT_WORDS}
        if rest:
            out.append(" ".join(rest))
    return out, (kinds.pop() if len(kinds) == 1 else "")

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


_ROLE_NOUN = re.compile(r"(er|or|ist|ant|ian|nurse|chef|cook|lawyer|clerk|officer|manager|lead|head|carer|worker|"
                        r"assistant|agent|nanny|midwife|surgeon|dentist|vet|gp|staff|crew)s?$")


@lru_cache(maxsize=256)
def _keyword_rule(keyword: str):
    """(role matcher or None, whole-phrase regex) for one search keyword."""
    from .candidates.matching import RoleMatcher, _phrase_re   # lazy: candidates imports core helpers
    k = re.sub(r"\s+", " ", keyword.strip().lower())
    rm = RoleMatcher(k)
    role = bool(rm.synonyms or rm.related or rm.contract or _ROLE_NOUN.search(rm.core))
    return (rm if role else None), _phrase_re(k)


_HOSPITAL_ONLY = re.compile(r"\b(pre[- ]?op|theatres?|ward|inpatient|icu|itu|critical care|a ?& ?e|emergency department|"
                            r"addiction|mental health|paediatric|neonatal|maternity|oncology|endoscopy|cardiac|surgical|"
                            r"dermatology|urology|outpatients?|hospital|safeguarding|research)\b", re.I)


def keyword_hit(job: Job, keyword: str, temporary: bool = False) -> bool:
    """A job title keyword ('care worker', 'locum nurse') must be in the job's TITLE as the whole phrase or a known
    synonym ('carer', 'support worker'); single words from it anywhere in the ad ('care' + 'worker') don't count.
    Skill keywords ('python', 'machine learning') match as whole words anywhere in the ad."""
    from .candidates.matching import CONTRACT_TEMP, _not_the_job
    rm, phrase = _keyword_rule(keyword)
    hay = f"{job.title}\n{job.company}\n{job.description}"
    if rm is None:
        return bool(phrase.search(hay.lower()))
    title = re.sub(r"\s+", " ", (job.title or "").lower())
    hit = None
    if rm._exact is not None and rm._exact.search(title):
        hit = f"profile says '{rm.core}'"
    else:
        hit = next((f"profile says '{s}'" for s, rx, sc in rm._syn if sc >= 2 and rx.search(title)), None)
    if hit is None and rm.related:
        # 'family lawyer' ~ 'Family Solicitor': a related title counts only with the rest of the phrase ('family')
        from .candidates.matching import SYNONYMS
        keys = [k for k in SYNONYMS if k != rm.core and re.search(rf"\b{re.escape(k)}\b", rm.core)]
        rest = [w for w in rm.content_words if not any(w in k.split() for k in keys)]
        if rest and all(re.search(rf"\b{re.escape(w)}", title) for w in rest):
            hit = next((f"profile says '{s}'" for s, rx, sc in rm._syn if sc < 2 and rx.search(title)), None)
    if hit is None and (rm.contract == "temporary" or temporary):
        # locum / bank searches ('temporary': the search's contract filter is locum / temp, the word was split off) also take the close primary / community nursing titles (locum ANP, bank district nurse)
        from .candidates.matching import LOCUM_FAMILY, _phrase_re
        if not _HOSPITAL_ONLY.search(title):   # a locum pre-op / theatre / ward nurse practitioner isn't primary care
            hit = next((f"profile says '{s}'" for s in LOCUM_FAMILY.get(rm.core, []) if _phrase_re(s).search(title)),
                       None)
    if hit is None or _not_the_job(title, hit):
        return False
    if rm.contract == "temporary" and not CONTRACT_TEMP.search(f"{hay}\n{job.employment_type or ''}".lower()):
        return False      # 'locum nurse': the ad has to say locum / bank / agency / temporary somewhere
    return True


def matches(job: Job, keywords: list[str], locations: list[str], remote_uk: bool = False,
            temporary: bool = False) -> bool:
    if keywords:
        # each keyword is OR'ed
        if not any(keyword_hit(job, k, temporary) for k in keywords if k and k.strip()):
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
           progress: Optional[ProgressFn] = None, cancel=None, contract: str = "any") -> SearchResult:
    """Run the selected sources and return de-duplicated, filtered, interleaved jobs.

    `cancel` is an optional threading.Event; when set, sources stop at the next job and the partial results return."""
    keywords, locations = list(keywords), list(locations)
    # "locum nurse" -> match ads for "nurse" whose contract is locum/temp (contract words rarely all appear in ads)
    kw_hint_raw = keywords[0] if len(keywords) == 1 else None
    keywords, inferred = split_contract_words(keywords)
    if contract in ("any", "", None) and inferred:
        contract = inferred
    http = HttpClient(user_agent=user_agent, min_interval=delay)
    kw_hint = kw_hint_raw if kw_hint_raw is not None else (keywords[0] if len(keywords) == 1 else None)
    if contract == "temporary" and kw_hint and not inferred:
        kw_hint = f"locum {kw_hint}"   # sources that can ask for locum / bank contracts (NHS Jobs) do so
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
                # sources that already searched by distance around the place (NHS Jobs) skip the text place check
                if not matches(job, keywords, [] if getattr(src, "server_side_location", False) and locations
                               else locations, remote_uk, temporary=contract == "temporary"):
                    continue
                if salary_only and not (job.salary_min or job.salary_max):
                    continue
                if remote_only and not job.remote:
                    continue
                enrich_contacts(job)
                if not job.employment_type:
                    job.employment_type = detect_employment_type(job.title, job.description)
                if contract in ("permanent", "temporary") and contract_bucket(job.employment_type) != contract:
                    continue
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
        if getattr(src, "info", None):     # notes that aren't errors (e.g. far-away results dropped)
            r.message = "; ".join(filter(None, [r.message] + list(src.info)))
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
        selected += ["jsonld", "rss"]
    return selected + ["reed", "adzuna"]
