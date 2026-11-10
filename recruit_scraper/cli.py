"""Command-line entry point: python -m recruit_scraper --help"""
from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from pathlib import Path
from typing import Iterable, Optional

from . import __version__
from .export import write_csv, write_json
from .core import DEFAULT_SOURCES, default_sources, search
from .sources import SOURCES

log = logging.getLogger("recruit_scraper")



def _split(values: Optional[Iterable[str]]) -> list[str]:
    out: list[str] = []
    for v in values or []:
        out.extend(x.strip() for x in v.split(",") if x.strip())
    return out


def _read_lines(path: Optional[str]) -> list[str]:
    if not path:
        return []
    return [l.strip() for l in Path(path).read_text().splitlines() if l.strip() and not l.startswith("#")]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="recruit_scraper",
        description="Scrape public job listings from ATS APIs, job-board APIs and careers pages (schema.org JSON-LD) "
                    "and export structured CSV/JSON.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  python -m recruit_scraper --sources remotive arbeitnow -k python -n 50
  python -m recruit_scraper --sources greenhouse lever --companies greenhouse:monzo lever:spotify -l London
  python -m recruit_scraper --sources jsonld --urls https://example.com/careers/123 --format json
""")
    p.add_argument("-s", "--sources", nargs="+", metavar="SRC",
                   help=f"sources to use: {', '.join(SOURCES)} or 'all'. Default: {' '.join(DEFAULT_SOURCES)} "
                        "plus greenhouse/lever/ashby if --companies is given, jsonld if --urls is given, "
                        "reed/adzuna if their API keys are set.")
    p.add_argument("-k", "--keyword", action="append",
                   help="keyword filter (title/company/description). Comma-separated or repeated = OR; "
                        "words within one keyword must all appear. e.g. -k 'data engineer,analytics'")
    p.add_argument("-l", "--location", action="append",
                   help="location filter (substring of location, OR'ed). 'remote' also matches remote jobs.")
    p.add_argument("-c", "--companies", nargs="+", default=[],
                   help="ATS board slugs. Prefix with the ATS to target one, e.g. greenhouse:monzo lever:spotify "
                        "ashby:ramp. Unprefixed slugs are tried on every selected ATS source.")
    p.add_argument("--all-presets", action="store_true",
                   help="also search all ~65 preset companies with UK vacancies (see recruit_scraper/presets.py)")
    p.add_argument("--companies-file", help="file with one company slug per line (same syntax as --companies)")
    p.add_argument("-u", "--urls", nargs="+", default=[], help="careers/job page URLs for the generic jsonld adapter")
    p.add_argument("--urls-file", help="file with one URL per line for the jsonld adapter")
    p.add_argument("-n", "--max-results", type=int, default=1000, help="max total jobs to export (default 1000, 0 = no limit)")
    p.add_argument("--max-per-source", type=int, default=0, help="max jobs kept from each source (default: no cap)")
    p.add_argument("-f", "--format", choices=["csv", "json", "both"], default="both")
    p.add_argument("-o", "--output", default="output/jobs",
                   help="output path without extension (default output/jobs -> output/jobs.csv/.json). "
                        "A .csv/.json extension is accepted too.")
    p.add_argument("--delay", type=float, default=1.0, help="min seconds between requests to the same host (default 1.0)")
    p.add_argument("--follow-links", type=int, default=25,
                   help="jsonld: max job links to follow from a listing page with no JobPosting data (default 25)")
    p.add_argument("--user-agent", help="override the User-Agent header (or set RECRUIT_SCRAPER_UA)")
    p.add_argument("--no-remote-uk", action="store_true",
                   help="with a UK location filter, do NOT include remote jobs open to UK candidates")
    p.add_argument("--salary-only", action="store_true", help="only keep jobs with a salary")
    p.add_argument("--remote-only", action="store_true", help="only keep remote jobs")
    p.add_argument("--contract", choices=["any", "permanent", "temporary"], default="any",
                   help="permanent, or temporary = locum / bank / agency / temp / contract / fixed-term (as the ad says)")
    p.add_argument("--no-contacts", action="store_true",
                   help="drop contact_email (recruiter emails published in postings) from the export")
    p.add_argument("--list-sources", action="store_true", help="list available sources and exit")
    p.add_argument("-v", "--verbose", action="count", default=0)
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "candidates":
        from .candidates.cli import main as candidates_main
        return candidates_main(argv[1:])
    if argv and argv[0] == "jobs":  # explicit form of the default command
        argv = argv[1:]
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose > 1 else logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    if args.list_sources:
        for name, cls in SOURCES.items():
            print(f"{name:11s} {cls.description}")
        return 0

    keywords = _split(args.keyword)
    locations = _split(args.location)
    companies = _split(args.companies) + _read_lines(args.companies_file)
    if args.all_presets:
        from .presets import PRESET_SLUGS
        companies += [c for c in PRESET_SLUGS if c not in companies]
    urls = _split(args.urls) + _read_lines(args.urls_file)

    if args.sources:
        selected = list(SOURCES) if "all" in args.sources else _split(args.sources)
        bad = [s for s in selected if s not in SOURCES]
        if bad:
            print(f"unknown source(s): {', '.join(bad)}. Use --list-sources.", file=sys.stderr)
            return 2
    else:
        selected = default_sources(companies, urls)

    def progress(name, event, r):
        if event == "start":
            print(f"[{name}] fetching...", file=sys.stderr)
        elif r and r.status == "ok":
            print(f"[{name}] fetched {r.fetched}, matched {r.matched}", file=sys.stderr)

    res = search(selected, keywords=keywords, locations=locations, companies=companies, urls=urls,
                 max_results=args.max_results, max_per_source=args.max_per_source, delay=args.delay,
                 follow_links=args.follow_links, user_agent=args.user_agent, salary_only=args.salary_only,
                 remote_only=args.remote_only, remote_uk=not args.no_remote_uk, hide_contacts=args.no_contacts, progress=progress,
                 contract=args.contract)
    jobs = res.jobs
    stats = {r.name: (f"fetched {r.fetched}, matched {r.matched}" if r.status == "ok" else r.status)
             + (f" ({r.message})" if r.message and r.status == "ok" else f": {r.message}" if r.message else "")
             for r in res.sources}

    out = Path(args.output)
    if out.suffix.lower() in (".csv", ".json"):
        out = out.with_suffix("")
    written = []
    if args.format in ("csv", "both"):
        written.append(write_csv(jobs, out.with_suffix(".csv")))
    if args.format in ("json", "both"):
        written.append(write_json(jobs, out.with_suffix(".json")))

    counts = Counter(j.source for j in jobs)
    print("\nSummary", file=sys.stderr)
    for name, s in stats.items():
        print(f"  {name:11s} {s}", file=sys.stderr)
    print(f"Exported {len(jobs)} unique jobs:", file=sys.stderr)
    for src, n in counts.most_common():
        print(f"  {src:30s} {n}", file=sys.stderr)
    for w in written:
        print(f"  -> {w}", file=sys.stderr)
    return 0
