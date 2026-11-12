"""RSS / Atom job feeds (feedparser, https://github.com/kurtmckee/feedparser).

Give it a feed URL, or any careers / vacancies page: if the page advertises feeds with <link rel="alternate"> they are
followed. Every URL is fetched by our own polite client (robots.txt, rate limit, user agent); feedparser only parses.
"""
from __future__ import annotations

import logging
from typing import Iterator

import requests

from ..crawl import discover_feeds, parse_feed
from ..http import RobotsDisallowed
from ..models import Job
from .base import Source

log = logging.getLogger(__name__)
_JOBBY = ("job", "vacanc", "career", "recruit", "work-with", "opportunit")


class RSSSource(Source):
    name = "rss"
    requires = "urls"
    description = "RSS / Atom job feeds, from feed links or from careers pages that advertise a feed."
    max_feeds_per_page = 3

    def _get(self, url: str):
        try:
            return self.http.get(url, check_robots=True,
                                 headers={"Accept": "application/rss+xml, application/atom+xml, text/xml, text/html;q=0.8"})
        except RobotsDisallowed:
            self.warnings.append(f"{url}: the site's robots.txt does not allow reading it - skipped")
        except requests.RequestException as e:
            self.warn(url, e)
        return None

    def fetch(self) -> Iterator[Job]:
        seen: set[str] = set()
        for url in self.urls:
            r = self._get(url)
            if r is None:
                continue
            head = r.content[:600].lower()
            if b"<rss" in head or b"<feed" in head or b"<rdf" in head or b"<?xml" in head:
                feeds = [(url, r.content)]
            else:
                found = discover_feeds(r.text, r.url)
                # a careers page's own feed, or any feed whose address looks like a jobs feed
                found = sorted(found, key=lambda u: not any(w in u.lower() for w in _JOBBY))[: self.max_feeds_per_page]
                feeds = []
                for f in found:
                    got = self._get(f)
                    if got is not None:
                        feeds.append((f, got.content))
            for f, content in feeds:
                for job in parse_feed(content, f):
                    if job.url not in seen:
                        seen.add(job.url)
                        yield job
