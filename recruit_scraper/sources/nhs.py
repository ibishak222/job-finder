"""NHS Jobs public search feed (England & Wales NHS and NHS-funded employers).

Uses the keyless XML search feed NHSBSA documents as its "Self-Serve API (XML/RSS feeds)"
(https://www.nhsbsa.nhs.uk/about-nhs-jobs/nhs-jobs-integration-and-benefits): same filters as the NHS Jobs search.
robots.txt sets no restrictions. Requests are paced (1 per second by default), at most `limit` results, and every
job links back to the official advert. The feed's <type> gives the contract: Permanent, Fixed-Term, Bank, Locum…

For a job title the feed is asked for the title and its two commonest synonyms ('practice nurse', 'general practice
nurse', 'gp nurse'); for a locum / bank title it is also asked for Locum and Bank contracts first. NHS Jobs covers
England and Wales: for a place it doesn't know (Glasgow, Belfast) it silently returns jobs elsewhere, so every job's
postcode is checked against the searched place and far-away jobs are dropped. 'UK' / 'United Kingdom' searches
the whole feed with no place.
"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from typing import Iterator, Optional

import requests

from ..models import Job
from ..salary import find_salary_in_text
from ..utils import detect_remote, html_to_text, normalise_employment_type, to_iso_date
from .base import Source

log = logging.getLogger(__name__)
FEED = "https://www.jobs.nhs.uk/api/v1/search_xml"
POSTCODES = "https://api.postcodes.io"
_PC = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})\b", re.I)
_NATIONWIDE = {"uk", "u.k.", "united kingdom", "gb", "great britain", "nationwide", "all uk", "whole uk", "uk-wide",
               "uk wide", "anywhere in the uk", "england", "england and wales"}


# extra feed keywords for a locum / bank search (the feed's keyword search is loose; titles are checked afterwards)
LOCUM_KEYWORDS = {
    "practice nurse": ["general practice nurse", "nurse practitioner", "community nurse", "district nurse",
                       "primary care nurse", "immunisation nurse"],
    "general practice nurse": ["practice nurse", "nurse practitioner", "community nurse", "primary care nurse"],
    "treatment room nurse": ["practice nurse", "nurse practitioner", "community nurse"],
    "advanced nurse practitioner": ["nurse practitioner", "advanced clinical practitioner", "practice nurse"],
    "nurse practitioner": ["advanced nurse practitioner", "advanced clinical practitioner", "practice nurse"],
    "district nurse": ["community nurse", "community staff nurse", "practice nurse"],
    "community nurse": ["district nurse", "community staff nurse", "practice nurse"],
    "healthcare assistant": ["health care assistant", "healthcare support worker", "phlebotomist"],
    "nurse": ["staff nurse", "registered nurse", "practice nurse", "community nurse"],
}
_PERIOD = re.compile(r"£\s?[\d,]+(?:\.\d{2})?(?:\s*(?:-|to)\s*£?\s?[\d,]+(?:\.\d{2})?)?\s*(?:per|an|a|/)\s*"
                     r"(hour|hr|session|day|shift|week|month|annum|year)\b", re.I)
_HOURS = re.compile(r"\b(\d+(?:\.\d+)?\s*(?:-|to)?\s*\d*(?:\.\d+)?\s*(?:clinical\s+)?(?:sessions?|hours?|hrs|days?)\s*"
                    r"(?:per|a|each|/)\s*week|\d+\s*sessions?\b(?:\s*(?:per|a)\s*(?:week|month))?)", re.I)
_HOURS_FIELD = re.compile(r"\b(?:Hours|Working pattern|Sessions):\s*(.{0,70})", re.I)
_HOURS_WORD = re.compile(r"^(bank|flexible|various|sessional|locum|ad hoc|full[- ]time|part[- ]time)(\s*/\s*(full|part)[- ]time)?",
                         re.I)


def pay_and_hours(salary: str, desc: str) -> tuple[str, str]:
    """'£22.50' + 'Salary: £22.50 per hour … Hours: 2 sessions a week' -> ('£22.50 per hour', '2 sessions a week')."""
    pay = (salary or "").strip()
    m = _PERIOD.search(desc or "")
    if m and (not pay or not re.search(r"\b(per|an|a|/)\s*(hour|session|day|annum|year|week|month)", pay, re.I)) \
            and (not pay or re.sub(r"\D", "", pay)[:3] in re.sub(r"\D", "", m.group(0))):
        pay = m.group(0)
    hours = ""
    f = _HOURS_FIELD.search(desc or "")
    if f:
        hm = _HOURS.search(f.group(1)) or _HOURS_WORD.match(f.group(1).strip())
        hours = hm.group(0) if hm else ""
    if not hours:
        hm = _HOURS.search(desc or "")
        hours = hm.group(0) if hm else ""
    return pay, re.sub(r"\s+", " ", hours).strip()


def miles(a: tuple[float, float], b: tuple[float, float]) -> float:
    import math
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = p2 - p1, math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 3958.8 * math.asin(math.sqrt(h))



# postcodes.io's place names are Great Britain only, so the main Northern Ireland towns are listed here
_NI_TOWNS = {"belfast": (54.597, -5.930), "derry": (54.997, -7.309), "londonderry": (54.997, -7.309),
             "lisburn": (54.516, -6.058), "newry": (54.176, -6.349), "armagh": (54.350, -6.653),
             "bangor": (54.660, -5.668), "craigavon": (54.447, -6.387), "portadown": (54.420, -6.445),
             "ballymena": (54.864, -6.276), "coleraine": (55.133, -6.669), "omagh": (54.600, -7.300),
             "enniskillen": (54.344, -7.632), "newtownabbey": (54.659, -5.909), "antrim": (54.716, -6.214),
             "dungannon": (54.503, -6.767), "carrickfergus": (54.716, -5.806), "newtownards": (54.591, -5.693),
             "larne": (54.851, -5.823), "strabane": (54.827, -7.463), "downpatrick": (54.328, -5.716)}


def geocode_place(http, location: str) -> Optional[tuple[float, float, str]]:
    """(lat, lon, country) of a postcode or town from postcodes.io (free, no key); None if unknown."""
    loc = (location or "").strip()
    ni = _NI_TOWNS.get(re.sub(r",?\s*(northern ireland|ni|uk)$", "", loc.lower()).strip())
    if ni:
        return ni[0], ni[1], "Northern Ireland"
    try:
        if _PC.fullmatch(loc.upper()) or re.fullmatch(r"[A-Z]{1,2}\d[A-Z\d]?", loc.upper()):
            path = "postcodes" if _PC.fullmatch(loc.upper()) else "outcodes"
            r = http.get_json(f"{POSTCODES}/{path}/{loc.replace(' ', '')}")["result"]
        else:
            rows = http.get_json(f"{POSTCODES}/places", params={"q": loc, "limit": 5}).get("result") or []
            rows = [x for x in rows if (x.get("name_1") or "").lower() == loc.lower()] or rows
            if not rows:
                return None
            r = rows[0]
        c = r.get("country")
        return float(r["latitude"]), float(r["longitude"]), (c[0] if isinstance(c, list) and c else c or "")
    except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError) as e:
        log.info("nhsjobs: place lookup failed for %s: %s", loc, e)
        return None



class NHSJobsSource(Source):
    name = "nhsjobs"
    description = "NHS Jobs public XML search feed (UK healthcare: nurses, HCAs, admin, doctors…). No key."
    max_pages = 10           # for the main title
    syn_pages = 2            # for each synonym
    contract_pages = 3       # for each of Locum / Bank and each locum keyword (local search)
    contract_pages_uk = 8    # … on a whole-UK search
    budget_seconds = 100     # stop asking the feed after this long (the source is one of several in a search)
    distance = 15
    retry_pause = 2.0
    retries = 3              # the feed sometimes answers 404 for a minute under load
    server_side_location = True  # the feed already searches within `distance` miles of the place

    def _queries(self) -> list[tuple[dict, int]]:
        from ..candidates.matching import RoleMatcher
        kw = (self.keyword or "").strip()
        if not kw:
            return [({}, self.max_pages)]
        rm = RoleMatcher(kw)
        core = rm.core or kw
        out: list[tuple[dict, int]] = []
        if rm.contract == "temporary":
            # locum / bank work: ask the feed for exactly those contracts ('practice nurse' + Locum, + Bank), for the
            # title and the close primary / community nursing titles locum work is also advertised under
            nationwide = (self.location or "").strip().lower() in _NATIONWIDE or not self.location
            pages = self.contract_pages_uk if nationwide else self.contract_pages
            kws = [core] + LOCUM_KEYWORDS.get(core, [])
            out += [({"keyword": k, "contractType": t}, pages) for k in kws for t in ("Locum", "Bank")]
            out.append(({"keyword": f"sessional {core}"}, 1))
        out.append(({"keyword": core}, self.max_pages))
        if rm.synonyms:
            out += [({"keyword": s}, self.syn_pages) for s in rm.search_terms(3)[1:]]
        return out

    def _feed_location(self, loc: str) -> str:
        """The feed only understands some town names ('Cardiff' returns London jobs) but always understands a
        postcode, so a town is sent as the postcode at its centre (postcodes.io reverse lookup)."""
        if _PC.fullmatch(loc.upper()) or re.fullmatch(r"[A-Z]{1,2}\d[A-Z\d]?", loc.upper()):
            return loc
        got = geocode_place(self.http, loc)
        if not got:
            return loc
        try:
            rows = self.http.get_json(f"{POSTCODES}/postcodes", params={"lon": got[1], "lat": got[0], "limit": 1,
                                                                         "radius": 2000}).get("result") or []
            return rows[0]["postcode"] if rows else loc
        except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError):
            return loc

    def _place(self) -> Optional[tuple[float, float]]:
        got = geocode_place(self.http, self.location)
        return got[:2] if got else None


    def _near(self, jobs: list[Job]) -> list[Job]:
        """Drop jobs whose postcode is far from the searched place (NHS Jobs returns English jobs for 'Glasgow')."""
        here = self._place()
        if here is None:
            return jobs
        pcs = {}
        for j in jobs:
            m = _PC.search(j.location or "")
            if m:
                pcs[id(j)] = f"{m.group(1)}{m.group(2)}".upper()
        coords: dict[str, tuple[float, float]] = {}
        todo = list(dict.fromkeys(pcs.values()))
        for i in range(0, len(todo), 100):
            try:
                self.http._throttle("api.postcodes.io")
                r = self.http.session.post(f"{POSTCODES}/postcodes", json={"postcodes": todo[i:i + 100]}, timeout=30)
                r.raise_for_status()
                for row in r.json().get("result") or []:
                    res = row.get("result")
                    if res and res.get("latitude") is not None:
                        coords[row["query"].upper()] = (res["latitude"], res["longitude"])
            except (requests.RequestException, ValueError, AttributeError) as e:
                log.info("nhsjobs: postcode check failed: %s", e)
                return jobs
        kept, dropped = [], 0
        for j in jobs:
            co = coords.get(pcs.get(id(j), ""))
            if co is not None and miles(here, co) > self.distance + 10:
                dropped += 1
                continue
            kept.append(j)
        if dropped:
            self.info.append(f"{dropped} results far from {self.location} dropped (NHS Jobs covers England and Wales; "
                             "Scotland / Northern Ireland GP jobs are mostly not on it)")
        return kept

    def fetch(self) -> Iterator[Job]:
        self.info: list[str] = []
        loc = (self.location or "").strip()
        nationwide = loc.lower() in _NATIONWIDE
        if loc and not nationwide:
            got = geocode_place(self.http, loc)
            if got and got[2] in ("Scotland", "Northern Ireland"):
                # the feed has (almost) no jobs there and answers 404 for those postcodes; the NHS Scotland and
                # HSC NI sources cover them
                self.info.append(f"NHS Jobs covers England and Wales only - skipped for {loc} "
                                 f"(see {'NHS Scotland Jobs' if got[2] == 'Scotland' else 'HSC NI jobs'})")
                return
        base = {"location": self._feed_location(loc), "distance": self.distance} if loc and not nationwide else {}
        want = self.limit or 100
        from ..core import keyword_hit  # lazy: core imports the sources
        hit = (lambda j: keyword_hit(j, self.keyword)) if self.keyword else (lambda j: True)
        seen: set[str] = set()
        hits: list[Job] = []
        rest: list[Job] = []
        import time
        end = time.monotonic() + self.budget_seconds
        failed = 0     # searches in a row the feed did not answer
        for q, pages in self._queries():
            if time.monotonic() > end:
                self.info.append("time limit reached - some NHS Jobs searches skipped")
                break
            params = {**q, **base}
            got = misses = 0
            if failed >= 2:
                self.info.append("NHS Jobs stopped answering - the rest of its searches were skipped")
                break
            for page in range(1, pages + 1):
                params["page"] = page
                root = self._page(params, end)
                if root is None:
                    failed += page == 1
                    break
                failed = 0
                items = root.findall("vacancyDetails")
                page_hits = 0
                for v in items:
                    job = self._job(v)
                    key = job.url.split("?")[0].rstrip("/").lower()
                    if not job.url or key in seen:
                        continue
                    seen.add(key)
                    if hit(job):
                        hits.append(job)
                        page_hits += 1
                        got += 1
                    else:
                        rest.append(job)
                # the feed is fuzzy and relevance-ordered: after two pages in a row with nothing that fits the
                # search, later pages will not have any either, so the time goes to the next search instead
                misses = 0 if page_hits else misses + 1
                total = int(root.findtext("totalPages") or 0)
                if not items or page >= total or got >= want or misses >= 2 or time.monotonic() > end:
                    break
            if len(hits) >= want:
                break
        jobs = hits[:want] + rest[: max(0, want - len(hits))]
        if base:
            jobs = self._near(jobs)
        yield from jobs

    def _page(self, params: dict, end: float = float("inf")):
        """One feed page. The feed now and then answers 404 / 5xx for a valid search under load, so it is asked
        again a few times after a growing pause; a last failure is reported and only that search is given up."""
        import time
        for attempt in range(self.retries + 1):
            try:
                r = self.http.get(FEED, params=params, headers={"Accept": "application/xml"})
                return ET.fromstring(r.content)
            except (requests.RequestException, ET.ParseError) as e:
                code = getattr(getattr(e, "response", None), "status_code", None)
                wait = self.retry_pause * (attempt + 1)
                # (5xx / connection errors are already retried by the HTTP client)
                if attempt < self.retries and code == 404 and time.monotonic() + wait < end:
                    time.sleep(wait)
                    continue
                if code == 404:
                    msg = "nhsjobs: the NHS Jobs feed did not answer some searches (404) - try again"
                    if msg not in self.warnings:
                        self.warnings.append(msg)
                else:
                    self.warn("nhsjobs", e)
                return None

    @staticmethod
    def _job(v) -> Job:
        g = lambda k: (v.findtext(k) or "").strip()  # noqa: E731
        desc = html_to_text(g("description"))
        locs = [l.text.strip() for l in v.findall("locations/location") if l.text]
        job = Job(title=g("title"), company=g("employer"), location="; ".join(locs),
                  posted_date=to_iso_date(g("postDate")[:10]), description=desc, url=g("url"),
                  source="nhsjobs", employment_type=normalise_employment_type(g("type")),
                  remote=detect_remote(" ".join(locs), g("title")), salary_raw=g("salary") or None,
                  closing_date=to_iso_date(g("closeDate")[:10]) or None)
        pay, hours = pay_and_hours(g("salary"), desc)
        job.salary_raw, job.hours = pay or None, hours or None
        sal = find_salary_in_text(pay)
        if sal:
            for k, val in sal.items():
                if hasattr(job, k) and k != "salary_raw":
                    setattr(job, k, val)
        return job
