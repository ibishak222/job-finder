"""`python -m recruit_scraper candidates ...`"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from .engine import EXPORT_DIR, export, find_candidates, purge_old_exports
from .geo import PostcodeError


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="recruit_scraper candidates",
        description="Find candidate profiles near a UK postcode from lawful public sources (GitHub, Bluesky and "
                    "Mastodon public profiles whose own text matches the job) "
                    "and hiring contacts published in job ads. Output is PERSONAL DATA (UK GDPR).",
        epilog="example: python -m recruit_scraper candidates --title 'python developer' --postcode 'M1 1AE' "
               "--radius 20 --min-exp 3")
    p.add_argument("-t", "--title", required=True, help="job title / skills, e.g. 'python developer'")
    p.add_argument("-p", "--postcode", required=True, help="UK postcode or outcode, e.g. 'M1 1AE' or 'M1'")
    p.add_argument("-r", "--radius", type=float, default=10, help="radius in miles (default 10; area list capped ~15 miles)")
    p.add_argument("--min-exp", type=float, help="minimum years of experience (GitHub: estimated from account age)")
    p.add_argument("--max-exp", type=float, help="maximum years of experience")
    p.add_argument("-k", "--keyword", action="append", default=[], help="extra keyword that must appear in the bio/company (OR)")
    p.add_argument("--providers", nargs="+", default=["github", "bluesky", "mastodon", "openstreetmap"],
                   choices=["github", "bluesky", "mastodon", "openstreetmap", "team_pages", "job_ads"],
                   help="github = public GitHub profiles (software jobs); bluesky / mastodon = people whose public "
                        "bio mentions the job (any job); openstreetmap = businesses / sole traders mapped "
                        "for that trade (labelled 'business'); team_pages = --team-url pages; job_ads = hiring "
                        "contacts published in job ads")
    p.add_argument("--team-url", action="append", default=[],
                   help="a firm's 'our people' page to read schema.org Person data from (repeatable)")
    p.add_argument("--contract", choices=["any", "permanent", "temporary"], default="any",
                   help="keep only people whose own profile says permanent, or temporary (locum/bank/agency/contract)")
    p.add_argument("--hireable-only", action="store_true",
                   help="only GitHub users who marked themselves 'Available for hire'")
    p.add_argument("--no-quick-list", action="store_true",
                   help="leave out GitHub accounts whose profile wasn't read (rate limit / max profiles)")
    p.add_argument("--near-only", action="store_true",
                   help="leave out UK-based Bluesky/Mastodon people who don't name a town near the postcode")
    p.add_argument("--no-websites", action="store_true",
                   help="don't check people's own websites for self-published contact details")
    p.add_argument("--wait-minutes", type=float, default=0,
                   help="if GitHub's hourly limit is hit, wait up to N minutes and continue (good for background runs)")
    p.add_argument("--max-accounts", type=int, default=1000, help="max GitHub accounts to collect from search (default 1000)")
    p.add_argument("--all-presets", action="store_true", help="job_ads: also scan all preset companies' job ads")
    p.add_argument("--no-company-pages", action="store_true",
                   help="job_ads: don't check company careers/contact pages for published recruiting emails")
    p.add_argument("--include-unknown-distance", action="store_true",
                   help="keep people whose location couldn't be placed on the map")
    p.add_argument("--max-profiles", type=int, default=200,
                   help="max NEW GitHub profiles to read per run (cached ones are free; no-token limit ~60/hour)")
    p.add_argument("--max-areas", type=int, default=6, help="max nearby areas to search on GitHub (default 6)")
    p.add_argument("--job-sources", nargs="+", default=["remotive", "arbeitnow", "remoteok", "himalayas", "jobicy"],
                   help="job sources used by the job_ads provider")
    p.add_argument("-c", "--companies", nargs="+", default=[], help="ATS company slugs for job_ads, e.g. greenhouse:monzo")
    p.add_argument("-f", "--format", choices=["csv", "json", "both"], default="both")
    p.add_argument("-o", "--output", help=f"output path without extension (default {EXPORT_DIR}/candidates_<time>)")
    p.add_argument("--retention-days", type=int, default=30,
                   help="delete candidate exports older than N days in output/candidates (default 30, 0 = never)")
    p.add_argument("-v", "--verbose", action="count", default=0)
    return p


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    purged = purge_old_exports(a.retention_days)
    if purged:
        print(f"Deleted {purged} candidate export(s) older than {a.retention_days} days.", file=sys.stderr)
    try:
        res = find_candidates(a.title, a.postcode, a.radius, a.min_exp, a.max_exp, a.keyword, a.providers,
                              github_token=os.environ.get("GITHUB_TOKEN"), hireable_only=a.hireable_only,
                              include_unread=not a.no_quick_list, fetch_websites=not a.no_websites,
                              max_wait_minutes=a.wait_minutes, max_results=a.max_accounts,
                              all_presets=a.all_presets, company_pages=not a.no_company_pages,
                              max_profiles=a.max_profiles, max_towns=a.max_areas,
                              include_unknown_distance=a.include_unknown_distance, job_sources=a.job_sources,
                              companies=a.companies, team_urls=a.team_url, contract=a.contract, uk_unknown=not a.near_only, progress=lambda m: print("  " + m, file=sys.stderr))
    except PostcodeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    base = Path(a.output) if a.output else EXPORT_DIR / f"candidates_{datetime.now():%Y%m%d_%H%M%S}"
    if base.suffix in (".csv", ".json"):
        base = base.with_suffix("")
    files = export(res.candidates, base, a.format)
    print("\nSummary", file=sys.stderr)
    for r in res.reports:
        print(f"  {r.name:8s} {r.status}: {r.found} found" + (f" ({r.message})" if r.message else ""), file=sys.stderr)
        if r.stats:
            print("           " + ", ".join(f"{k.replace('_', ' ')}: {v}" for k, v in r.stats.items()), file=sys.stderr)
    if res.coverage:
        print(f"  note: {res.coverage}", file=sys.stderr)
    if res.suppressed:
        print(f"  removed {res.suppressed} on the do-not-contact list", file=sys.stderr)
    print(f"{len(res.candidates)} records ({sum(1 for c in res.candidates if c.email)} with email). "
          "PERSONAL DATA - see README 'UK GDPR'.", file=sys.stderr)
    for f in files:
        print(f"  -> {f}", file=sys.stderr)
    return 0
