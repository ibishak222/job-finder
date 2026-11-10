"""Candidate search orchestration shared by the CLI and the UI."""
from __future__ import annotations

import logging
import re
import time
from datetime import date
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from ..http import HttpClient
from . import suppression
from .geo import Place, PlaceGeocoder, lookup_postcode
from .matching import is_tech_role
from .models import Candidate
from .providers import GitHubProvider, JobAdContactsProvider, ProviderStop
from .directories import OpenStreetMapProvider, TeamPageProvider
from .matching import RoleMatcher
from .social import BlueskyProvider, MastodonProvider

PEOPLE_SOURCES = ("github", "bluesky", "mastodon", "openstreetmap")
SOURCE_LABELS = {"github": "GitHub", "bluesky": "Bluesky", "mastodon": "Mastodon", "job_ads": "Hiring contacts",
                 "openstreetmap": "OpenStreetMap businesses", "team_pages": "Team pages"}
CONTRACT_CHOICES = {"any": "Any", "permanent": "Permanent", "temporary": "Locum / temp / contract"}

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
    tech_role: bool = True
    coverage: str = ""   # friendly note when public sources cover this kind of job poorly


def _norm_link(u: str) -> str:
    u = (u or "").strip().lower()
    u = u.split("://", 1)[-1].removeprefix("www.").rstrip("/")
    return u


def merge_duplicates(cands: list[Candidate]) -> list[Candidate]:
    """One row per person across sources: same email, profile link, own website, or a profile one of them links to.
    The better-matching row is kept; the other source is added to 'Found via' and its link to 'other_profiles'."""
    out: list[Candidate] = []
    index: dict[str, Candidate] = {}

    def keys(c: Candidate) -> list[str]:
        ks = [f"e:{c.email.lower()}"] if c.email else []
        ks += [f"l:{_norm_link(u)}" for u in [c.profile_url, c.website] + (c.other_profiles or "").split() if u]
        return ks

    for c in sorted(cands, key=lambda c: -(c.match_score or 0)):
        ks = keys(c)
        hit = next((index[k] for k in ks if k in index), None)
        if hit is not None and hit.kind != c.kind:
            hit = None  # a business listing and a person sharing a website stay separate rows
        if hit is None or c.kind != "candidate":
            if hit is not None:
                continue  # exact duplicate hiring contact / business
            out.append(c)
            for k in ks:
                index.setdefault(k, c)
            continue
        _absorb(hit, c)
        for k in ks:
            index.setdefault(k, hit)
    return out


def _absorb(hit: Candidate, c: Candidate) -> None:
    """Fold duplicate row `c` into `hit` (keeps hit's values, fills gaps, records the other source and link)."""
    for prov in c.provider.split(" + "):
        if prov not in hit.provider.split(" + "):
            hit.provider = f"{hit.provider} + {prov}"
    links = [u for u in (hit.other_profiles or "").split() if u]
    for u in [c.profile_url] + (c.other_profiles or "").split():
        if u and _norm_link(u) != _norm_link(hit.profile_url):
            links.append(u)
    hit.other_profiles = " ".join(dict.fromkeys(links))
    for f in ("email", "email_source", "phone", "website", "linkedin_url", "twitter_url", "location", "company",
              "contract"):
        if not getattr(hit, f) and getattr(c, f):
            setattr(hit, f, getattr(c, f))
    if hit.distance_miles is None and c.distance_miles is not None:
        hit.distance_miles = c.distance_miles
    if hit.experience_years is None and c.experience_years is not None:
        hit.experience_years, hit.experience_source = c.experience_years, c.experience_source
    hit.last_active = max(hit.last_active or "", c.last_active or "")
    hit.match_score = max(hit.match_score or 0, c.match_score or 0)


_NAME_JUNK = re.compile(r"\(.*?\)|\b(he|she|they)/(him|her|them|they)\b|\b(dr|mr|mrs|ms|miss|rn|rgn|rmn|msc|bsc|ba|"
                        r"llb|cert|frcs|mbe|obe)\b\.?|[^a-z\s'\-]", re.I)


def _clean_name(name: str) -> str:
    n = _NAME_JUNK.sub(" ", (name or "").lower())
    return " ".join(w for w in n.split() if len(w) > 1)


def merge_similar(cands: list[Candidate], threshold: float = 92) -> list[Candidate]:
    """Second dedupe pass for the same person on two sites WITHOUT a shared link: near-identical full names
    (RapidFuzz token-sort ratio >= threshold, at least first + last name) in the same area (within 3 miles, or the
    same location text), found on different sources. Conservative on purpose: common names in different towns,
    or two accounts on the same site, are never merged."""
    try:
        from rapidfuzz import fuzz  # https://github.com/rapidfuzz/RapidFuzz (MIT)
        ratio = fuzz.token_sort_ratio
    except ImportError:  # pragma: no cover - fall back to the standard library
        import difflib

        def ratio(a, b):
            return 100 * difflib.SequenceMatcher(None, " ".join(sorted(a.split())), " ".join(sorted(b.split()))).ratio()
    out: list[Candidate] = []
    for c in cands:
        name = _clean_name(c.name)
        hit = None
        if c.kind == "candidate" and len(name.split()) >= 2:
            for o in out:
                if o.kind != "candidate" or set(o.provider.split(" + ")) & set(c.provider.split(" + ")):
                    continue
                oname = _clean_name(o.name)
                if len(oname.split()) < 2 or ratio(name, oname) < threshold:
                    continue
                near = (o.distance_miles is not None and c.distance_miles is not None
                        and abs(o.distance_miles - c.distance_miles) <= 3)
                same_loc = bool(o.location) and o.location.strip().lower() == (c.location or "").strip().lower()
                if near or same_loc:
                    hit = o
                    break
        if hit is None:
            out.append(c)
        else:
            _absorb(hit, c)
            hit.match_reason = (hit.match_reason + "; same name and area on " + c.provider).strip("; ")
    return out


def _days_since(day: str) -> Optional[int]:
    try:
        return (date.today() - date.fromisoformat(day[:10])).days
    except (TypeError, ValueError):
        return None


def score_fit(c: Candidate, radius: float, min_exp: Optional[float] = None, max_exp: Optional[float] = None,
              want: str = "") -> Candidate:
    """'Best match' score 0-100 with a plain-English breakdown:
    role match 45 · distance 20 · contactable 20 · experience fit 10 · recent activity 5 (+ contract / for-hire)."""
    parts, total = [], 0.0
    ms = c.match_score or 0
    total += 45 * min(1.0, max(0.0, ms) / 3)
    parts.append("job title in profile" if ms >= 3 else "synonym in profile" if ms >= 2 else "related words in profile")
    if c.distance_miles is not None:
        total += 20 * max(0.0, 1 - c.distance_miles / (radius + 3))
        parts.append(f"{c.distance_miles:g} mi away")
    elif _uk_only(c):
        parts.append("UK, no nearby town mentioned")
    else:
        total += 6
        parts.append("distance unknown")
    if c.email:
        total += 15
        parts.append("has email")
    elif c.website:
        total += 7
        parts.append("has website")
    elif c.profile_url:
        total += 3
    if c.phone:
        total += 5
        parts.append("has phone")
    y = c.experience_years
    if y is None:
        total += 4
    elif (min_exp is None or y >= min_exp) and (max_exp is None or y <= max_exp):
        total += 10
        parts.append(f"{y:g} yrs fits")
    else:
        parts.append(f"{y:g} yrs outside range")
    d = _days_since(c.last_active)
    if d is None:
        total += 2
    elif d <= 180:
        total += 5
        parts.append("active recently")
    elif d <= 730:
        total += 2
    else:
        parts.append("inactive 2+ yrs")
    if want and c.contract == want:
        total += 5
        parts.append("says " + ("locum/temp/contract" if want == "temporary" else "permanent"))
    if c.hireable:
        total += 3
        parts.append("available for hire")
    c.fit = round(min(100.0, total), 1)
    c.fit_reason = " · ".join(parts)
    return c


def _uk_only(c: Candidate) -> bool:
    return "no nearby town mentioned" in (c.location or "")


def rank_key(c: Candidate, min_exp: Optional[float] = None, max_exp: Optional[float] = None, contract: str = ""):
    """Best matches first: people, then businesses, then hiring contacts; within each, highest fit score."""
    y = c.experience_years
    exp_fit = 1 if y is None else (0 if (min_exp is None or y >= min_exp) and (max_exp is None or y <= max_exp) else 2)
    kind = {"candidate": 0, "business": 1}.get(c.kind, 2)
    bonus = 0.25 if contract and c.contract == contract else 0
    return (kind, _uk_only(c), -(c.fit or 0), -((c.match_score or 0) + bonus), exp_fit, not c.email,
            c.distance_miles if c.distance_miles is not None else 999)


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
                    max_exp: Optional[float] = None, keywords: list[str] = (), providers: list[str] = PEOPLE_SOURCES,
                    github_token: Optional[str] = None, hireable_only: bool = False, max_profiles: int = 200,
                    max_towns: int = 6, include_unknown_distance: bool = False, job_sources: list[str] = (),
                    companies: list[str] = (), suppression_entries: Optional[list[str]] = None,
                    delay: float = 1.0, include_unread: bool = True, fetch_websites: bool = True,
                    max_wait_minutes: float = 0, max_results: int = 1000, all_presets: bool = False,
                    company_pages: bool = True, team_urls: list[str] = (), contract: str = "any",
                    uk_unknown: bool = True,
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

    result.tech_role = tech = is_tech_role(title)
    people = []
    if "github" in providers:
        people.append(GitHubProvider(http, token=github_token, max_towns=max_towns, max_profiles=max_profiles,
                                     hireable_only=hireable_only, max_results=max_results,
                                     include_unread=include_unread, fetch_websites=fetch_websites,
                                     max_wait_minutes=max_wait_minutes))
    if "bluesky" in providers and not hireable_only:
        people.append(BlueskyProvider(fetch_websites=fetch_websites, uk_unknown=uk_unknown))
    if "mastodon" in providers and not hireable_only:
        people.append(MastodonProvider(fetch_websites=fetch_websites, uk_unknown=uk_unknown))
    if "openstreetmap" in providers and not hireable_only:
        people.append(OpenStreetMapProvider(fetch_websites=fetch_websites))
    if ("team_pages" in providers or team_urls) and not hireable_only and team_urls:
        people.append(TeamPageProvider(list(team_urls)))
    for prov in people:
        rep = ProviderReport(prov.name, "ok")
        try:
            for c in prov.search(title, place, radius_miles, min_exp, max_exp, list(keywords),
                                 include_unknown_distance, geocoder, progress):
                found.append(c)
                rep.found += 1
        except ProviderStop as e:
            rep.status, rep.message = ("ok" if rep.found else "error"), str(e)
        except SearchCancelled:
            raise
        except ImportError as e:
            rep.status, rep.message = "error", f"library not installed ({e.name}); run pip install -r requirements.txt"
        except Exception as e:  # noqa: BLE001
            log.exception("%s provider failed", prov.name)
            rep.status, rep.message = "error", f"{SOURCE_LABELS.get(prov.name, prov.name)} search failed: {e}"
        if prov.notes:
            rep.message = "; ".join(dict.fromkeys(filter(None, [rep.message] + prov.notes)))
        rep.stats = prov.stats
        if not rep.found and rep.status == "ok" and (prov.name == "github" and not tech or
                                                     prov.name == "openstreetmap" and any("No OpenStreetMap business category"
                                                                                          in n for n in prov.notes)):
            rep.status = "skipped"
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

    # people must match the job in their own profile text; never pad with location-only results
    found = [c for c in found if c.kind != "candidate" or (c.match_score or 0) > 0]
    uniq = merge_similar(merge_duplicates(found))
    entries = suppression.load() if suppression_entries is None else suppression_entries
    uniq, result.suppressed = suppression.apply(uniq, entries)
    want = RoleMatcher(title).contract or ("" if contract in ("any", "", None) else contract)
    if contract in ("permanent", "temporary"):
        uniq = [c for c in uniq if c.kind != "candidate" or c.contract == contract]
    for c in uniq:
        score_fit(c, radius_miles, min_exp, max_exp, want)
    uniq.sort(key=lambda c: rank_key(c, min_exp, max_exp, want))
    n_people = sum(1 for c in uniq if c.kind == "candidate")
    n_uk = sum(1 for c in uniq if c.kind == "candidate" and "no nearby town" in (c.location or ""))
    n_biz = sum(1 for c in uniq if c.kind == "business")
    if not tech:
        result.coverage = (f"'{title}' isn't a software job, so GitHub (developers only) was skipped. People were "
                           "looked for on Bluesky (search, profession starter packs and feeds) and Mastodon (UK and "
                           "general servers, job hashtags), where they describe their own job in their bio"
                           + (", plus businesses/sole traders mapped in OpenStreetMap" if "openstreetmap" in providers else "")
                           + ". Free public sources cover jobs like this thinly"
                           + (f": {n_people} matching people" if n_people else ": nobody's public profile matched")
                           + (f" ({n_uk} of them UK-based but not naming a town near here - ranked lower)" if n_uk else "")
                           + (f" and {n_biz} businesses (firms / sole traders, not job seekers)" if n_biz else "")
                           + f" near {place.town}."
                           + " For more reach, use the hiring contacts on the Jobs page, or a CV database "
                             "(Reed, Totaljobs or CV-Library) where jobseekers opted in to be contacted.")
    elif not n_people:
        result.coverage = (f"Nobody near {place.town} mentions '{title}' in a public profile. Try a broader title "
                           "(e.g. 'developer'), a bigger radius, or fewer years of experience.")
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
