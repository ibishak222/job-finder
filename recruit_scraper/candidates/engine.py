"""Candidate search orchestration shared by the CLI and the UI."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from ..http import HttpClient
from . import suppression
from .geo import Place, PlaceGeocoder, lookup_postcode
from .models import Candidate
from .providers import GitHubProvider, JobAdContactsProvider, ProviderStop

log = logging.getLogger(__name__)
EXPORT_DIR = Path(__file__).resolve().parents[2] / "output" / "candidates"


@dataclass
class ProviderReport:
    name: str
    status: str          # ok | error | skipped
    found: int = 0
    message: str = ""
    stats: dict = field(default_factory=dict)


@dataclass
class CandidateResult:
    place: Optional[Place] = None
    candidates: list[Candidate] = field(default_factory=list)
    reports: list[ProviderReport] = field(default_factory=list)
    suppressed: int = 0


def filter_experience(cands: list[Candidate], min_exp: Optional[float], max_exp: Optional[float],
                      keep_unknown: bool = True) -> list[Candidate]:
    """Keep candidates whose (estimated) experience is within [min_exp, max_exp]."""
    out = []
    for c in cands:
        y = c.experience_years
        if y is None:
            if keep_unknown:
                out.append(c)
            continue
        if min_exp is not None and y < min_exp:
            continue
        if max_exp is not None and y > max_exp:
            continue
        out.append(c)
    return out


class SearchCancelled(Exception):
    pass


def find_candidates(title: str, postcode: str, radius_miles: float = 10, min_exp: Optional[float] = None,
                    max_exp: Optional[float] = None, keywords: list[str] = (), providers: list[str] = ("github",),
                    github_token: Optional[str] = None, hireable_only: bool = False, max_profiles: int = 200,
                    max_towns: int = 6, include_unknown_distance: bool = False, job_sources: list[str] = (),
                    companies: list[str] = (), suppression_entries: Optional[list[str]] = None,
                    delay: float = 1.0, include_unread: bool = True, fetch_websites: bool = True,
                    max_wait_minutes: float = 0, max_results: int = 1000, all_presets: bool = False,
                    company_pages: bool = True,
                    progress: Callable[[str], None] = lambda m: None, cancel=None) -> CandidateResult:
    if cancel is not None:  # stop at the next progress message once the user presses Cancel
        _report = progress

        def progress(m: str) -> None:
            if cancel.is_set():
                raise SearchCancelled("search cancelled")
            _report(m)
    http = HttpClient(min_interval=delay)
    pc_http = HttpClient(min_interval=0.1)  # postcodes.io is a free public service; light but polite
    result = CandidateResult()
    progress(f"Looking up postcode {postcode}…")
    result.place = place = lookup_postcode(pc_http, postcode, radius_miles)
    progress(f"{place.postcode} is in {place.town}, {place.region}. Areas within {radius_miles:g} miles: "
             f"{', '.join(place.nearby_towns[:8])}")
    geocoder = PlaceGeocoder(pc_http)
    found: list[Candidate] = []

    if "github" in providers:
        gh = GitHubProvider(http, token=github_token, max_towns=max_towns, max_profiles=max_profiles,
                            hireable_only=hireable_only, max_results=max_results, include_unread=include_unread,
                            fetch_websites=fetch_websites, max_wait_minutes=max_wait_minutes)
        rep = ProviderReport("github", "ok")
        try:
            for c in gh.search(title, place, radius_miles, min_exp, max_exp, list(keywords),
                               include_unknown_distance, geocoder, progress):
                found.append(c)
                rep.found += 1
        except ProviderStop as e:
            rep.status, rep.message = ("ok" if rep.found else "error"), str(e)
        except SearchCancelled:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("github provider failed")
            rep.status, rep.message = "error", f"GitHub search failed: {e}"
        if gh.notes:
            rep.message = "; ".join(dict.fromkeys(filter(None, [rep.message] + gh.notes)))
        rep.stats = gh.stats
        result.reports.append(rep)

    if "job_ads" in providers:
        from ..presets import PRESET_SLUGS
        comp = list(companies) + ([c for c in PRESET_SLUGS if c not in companies] if all_presets else [])
        ja = JobAdContactsProvider(list(job_sources) or ["remotive", "arbeitnow", "remoteok", "himalayas", "jobicy"],
                                   comp, delay, company_pages=company_pages)
        rep = ProviderReport("job_ads", "ok")
        try:
            for c in ja.search(title, place, radius_miles, geocoder, progress):
                found.append(c)
                rep.found += 1
        except SearchCancelled:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("job ad contacts failed")
            rep.status, rep.message = "error", str(e)
        if ja.notes:
            rep.message = "; ".join(dict.fromkeys(filter(None, [rep.message] + ja.notes)))
        rep.stats = ja.stats
        result.reports.append(rep)

    # de-duplicate (profile URL / email)
    seen, uniq = set(), []
    for c in found:
        key = (c.email or c.profile_url).lower()
        if key and key not in seen:
            seen.add(key)
            uniq.append(c)
    entries = suppression.load() if suppression_entries is None else suppression_entries
    uniq, result.suppressed = suppression.apply(uniq, entries)
    uniq.sort(key=lambda c: (c.kind != "candidate", not c.email, c.profile_read is False,
                             c.distance_miles if c.distance_miles is not None else 999))
    result.candidates = uniq
    return result


def purge_old_exports(days: int, folder: Path = EXPORT_DIR) -> int:
    """Delete candidate export files older than `days` days (storage limitation). Returns count deleted."""
    if days <= 0 or not folder.exists():
        return 0
    cutoff = time.time() - days * 86400
    n = 0
    for f in folder.glob("*"):
        if f.is_file() and f.stat().st_mtime < cutoff:
            f.unlink()
            n += 1
    return n


def export(cands: list[Candidate], base: Path, fmt: str = "both") -> list[Path]:
    import csv
    import json
    base.parent.mkdir(parents=True, exist_ok=True)
    written = []
    rows = [c.to_dict() for c in cands]
    if fmt in ("csv", "both"):
        p = base.with_suffix(".csv")
        with p.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=Candidate.field_names())
            w.writeheader()
            for r in rows:
                w.writerow({k: "" if v is None else v for k, v in r.items()})
        written.append(p)
    if fmt in ("json", "both"):
        p = base.with_suffix(".json")
        p.write_text(json.dumps({"notice": "PERSONAL DATA - UK GDPR applies. Delete when no longer needed.",
                                 "candidates": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
        written.append(p)
    for p in written:
        p.chmod(0o600)
    return written
