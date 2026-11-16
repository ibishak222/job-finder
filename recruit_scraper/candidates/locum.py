"""Direct employers that hire locum / bank / sessional staff, with the evidence.

Evidence comes from the employers themselves, never from agencies or booking apps:
* an open locum / bank / sessional advert on NHS Jobs, NHS Scotland Jobs or HSC NI (the employer's own advert);
* a staff bank / locum page or a sentence about locum / bank staff on the employer's own website (contacts.py).
`hires_locums` on an EmployerLead is 3 (advert open now), 2 (bank / locum page), 1 (own-site sentence) or 0 (no evidence).
"""
from __future__ import annotations

import logging
import re
import time
from typing import Iterable, Optional

from ..agencies import is_agency_job, is_agency_name
from ..http import HttpClient
from ..models import Job

log = logging.getLogger(__name__)

SCORE_LABEL = {3: "Advert open now", 2: "Staff bank / locum page", 1: "Mentions locum / bank staff", 0: ""}
_TRUST = re.compile(r"\bnhs (foundation )?trust\b|foundation trust|health board|\bnhs trust\b|hospitals? (nhs )?trust|"
                    r"university hospitals?|^nhs [a-z ]{3,40}$", re.I)
_PRACTICE = re.compile(r"surgery|medical (centre|center|practice|group)|health ?cent(re|er)|practice|\bpcn\b|primary care|"
                       r"doctors|federation|\bgp\b", re.I)
_PC = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})\b")


def advert_employer_type(name: str) -> str:
    if _TRUST.search(name or ""):
        return "NHS trust (runs its own staff bank)"
    if re.search(r"hospice", name or "", re.I):
        return "Hospice"
    if _PRACTICE.search(name or ""):
        return "GP practice / primary care"
    return "Employer advertising locum / bank work"


def locum_adverts(title: str, location: str, radius_miles: float, seconds: float = 30.0,
                  pages: int = 3, nations: Optional[set[str]] = None) -> list[Job]:
    """Open locum / bank / sessional adverts for this kind of worker at direct employers (NHS Jobs, plus NHS Scotland and
    HSC NI for the nations asked for). A few contract-filtered pages, one request at a time, within `seconds`."""
    from ..core import keyword_hit
    from ..sources.nations import HSCNIJobsSource, NHSScotlandSource
    from ..sources.nhs import NHSJobsSource
    nations = nations or {"England", "Wales", "Scotland", "Northern Ireland"}
    end = time.monotonic() + seconds
    out: list[Job] = []
    seen: set[str] = set()
    http = HttpClient(min_interval=1.0, timeout=30, retries=1, backoff=2)

    def take(jobs: Iterable[Job]) -> None:
        for j in jobs:
            key = (j.url or "").split("?")[0].lower()
            if not key or key in seen or is_agency_job(j) or not keyword_hit(j, title, temporary=True):
                continue
            seen.add(key)
            out.append(j)
    try:
        if nations & {"England", "Wales"}:
            src = NHSJobsSource(http, keyword=title, location=location)
            src.only_contract, src.contract_pages, src.contract_pages_uk = True, pages, pages
            src.distance, src.budget_seconds = max(5, int(radius_miles)), max(8.0, seconds * 0.7)
            take(src.fetch())
        for nation, cls, share in (("Scotland", NHSScotlandSource, 0.2), ("Northern Ireland", HSCNIJobsSource, 0.1)):
            if nation in nations and time.monotonic() < end - 3:
                src = cls(http, keyword=title, location=location)
                if hasattr(src, "budget_seconds"):
                    src.budget_seconds = max(5.0, seconds * share)
                if hasattr(src, "max_pages"):
                    src.max_pages = 2
                take(src.fetch())
    except Exception as e:  # noqa: BLE001 - evidence is a bonus: never break the search
        log.info("locum adverts failed: %s", e)
    return out


def _postcode(text: str) -> str:
    m = _PC.search((text or "").upper())
    return f"{m.group(1)} {m.group(2)}" if m else ""


def advert_evidence(job: Job) -> str:
    bits = [job.title]
    if job.salary_raw:
        bits.append(str(job.salary_raw))
    if job.closing_date:
        bits.append(f"closes {job.closing_date}")
    return "Advertising now (" + (job.source or "job board").replace("nhsjobs", "NHS Jobs").replace(
        "nhsscotland", "NHS Scotland Jobs").replace("hscni", "HSC NI jobs") + "): " + " · ".join(bits)


def attach_adverts(leads: list, jobs: list[Job], new_lead) -> tuple[int, int]:
    """Mark leads whose own advert is open (hires_locums = 3, with the advert as the evidence) and add the employers that
    advertise but are not in the registers yet. `new_lead(**fields)` builds an EmployerLead. Returns (flagged, added)."""
    from .employers import _name_key, _pc
    try:
        from rapidfuzz import fuzz
        ratio = fuzz.token_sort_ratio      # not token_set: "Herne Bay" must not match "Herne Bay Community Radio Association"
    except ImportError:  # pragma: no cover
        import difflib

        def ratio(a, b):
            return 100 * difflib.SequenceMatcher(None, a, b).ratio()
    by_emp: dict[str, list[Job]] = {}
    for j in jobs:
        if j.company and not is_agency_name(j.company):
            by_emp.setdefault(j.company.strip().lower(), []).append(j)
    flagged = added = 0
    keys = [(l, _name_key(l.name)) for l in leads]
    new = []
    for emp, ads in sorted(by_emp.items(), key=lambda kv: -len(kv[1])):
        ads.sort(key=lambda j: (j.closing_date or "9999"))
        ad = ads[0]
        nk = _name_key(ad.company)
        apc = _postcode(ad.location)
        hit = None
        best = 0
        for l, lk in keys:
            if not nk or not lk:
                continue
            r = ratio(nk, lk)
            if r < 88:
                continue
            if apc and l.postcode and _pc(apc)[:2] != _pc(l.postcode)[:2] and not _TRUST.search(l.name):
                continue
            if r > best:
                best, hit = r, l
        if hit is not None:
            if hit.hires_locums < 3:
                hit.hires_locums, hit.locum_evidence, hit.locum_evidence_url = 3, advert_evidence(ad), ad.url
                flagged += 1
            if not hit.vacancy:
                hit.vacancy, hit.vacancy_url, hit.vacancy_type = ad.title, ad.url, "locum / bank / temporary"
            if "NHS Jobs" not in hit.source and (ad.source or "").startswith("nhs"):
                hit.source += " + NHS Jobs"
                hit.source_url = f"{hit.source_url} {ad.url}".strip()
            continue
        new.append(new_lead(name=ad.company.strip(), employer_type=advert_employer_type(ad.company),
                            address=ad.location or "", postcode=_postcode(ad.location),
                            source={"nhsjobs": "NHS Jobs", "nhsscotland": "NHS Scotland Jobs",
                                    "hscni": "HSC NI jobs"}.get(ad.source or "", "job advert"),
                            source_url=ad.url, vacancy=ad.title, vacancy_url=ad.url, vacancy_type="locum / bank / temporary",
                            hires_locums=3, locum_evidence=advert_evidence(ad), locum_evidence_url=ad.url,
                            careers_url=ad.url, note="Employer advertising locum / bank work - not a candidate"))
    for l in new[:80]:
        leads.append(l)
        added += 1
    return flagged, added
