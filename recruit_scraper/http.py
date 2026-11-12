"""Polite HTTP client: clear user agent, per-host rate limiting, retries with backoff, robots.txt.

robots.txt is read with Protego (https://github.com/scrapy/protego, BSD-3, the parser Scrapy uses). Python's built-in
urllib.robotparser ignores the `*` and `$` wildcards most sites use (`Disallow: /*-jobs*?*`), so it would allow
pages the site has asked crawlers to leave alone."""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Optional
from urllib import robotparser
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)

DEFAULT_UA = "RecruitScraper/0.1 (job-listings aggregator; polite bot; contact: set RECRUIT_SCRAPER_CONTACT)"


class RobotsDisallowed(Exception):
    pass


class _Rules:
    """Parsed robots.txt rules, or a blanket allow / disallow when the file can't be used."""

    def __init__(self, text: Optional[str] = None, allow_all: bool = False, disallow_all: bool = False):
        self.allow_all, self.disallow_all = allow_all, disallow_all
        self._p = None
        if text is not None:
            try:
                from protego import Protego
                self._p = Protego.parse(text)
            except ImportError:    # library missing: fall back to the standard parser (no wildcard support)
                rp = robotparser.RobotFileParser()
                rp.parse(text.splitlines())
                self._p = rp

    def can_fetch(self, user_agent: str, url: str) -> bool:
        if self.disallow_all:
            return False
        if self.allow_all or self._p is None:
            return True
        try:
            return bool(self._p.can_fetch(url, user_agent))        # Protego: (url, user_agent)
        except TypeError:
            return bool(self._p.can_fetch(user_agent, url))        # urllib: (user_agent, url)


class HttpClient:
    def __init__(self, user_agent: Optional[str] = None, min_interval: float = 1.0, timeout: float = 30.0,
                 retries: int = 3, backoff: float = 1.5):
        import os
        contact = os.environ.get("RECRUIT_SCRAPER_CONTACT")
        ua = user_agent or os.environ.get("RECRUIT_SCRAPER_UA") or DEFAULT_UA
        if contact and "RECRUIT_SCRAPER_CONTACT" in ua:
            ua = ua.replace("set RECRUIT_SCRAPER_CONTACT", contact)
        self.user_agent = ua
        self.min_interval = min_interval
        self.timeout = timeout
        self._last: dict[str, float] = {}
        self._robots: dict[str, _Rules] = {}
        self._lock = threading.Lock()
        self.session = requests.Session()
        retry = Retry(total=retries, backoff_factor=backoff, status_forcelist=(429, 500, 502, 503, 504),
                      allowed_methods=frozenset(["GET", "HEAD"]), respect_retry_after_header=True)
        adapter = HTTPAdapter(max_retries=retry, pool_connections=32, pool_maxsize=32)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers.update({"User-Agent": self.user_agent,
                                     "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
                                     "Accept-Language": "en-GB,en;q=0.9"})

    def _throttle(self, host: str, interval: Optional[float] = None) -> None:
        """Reserve the next request slot for this host (thread-safe; sleeps outside the lock). `interval` lets a
        caller that is talking to a public API built for bulk reuse go a little faster than the default."""
        gap = self.min_interval if interval is None else interval
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._last.get(host, 0) + gap)
            self._last[host] = slot
        if slot > now:
            time.sleep(slot - now)

    def allowed_by_robots(self, url: str) -> bool:
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        if base not in self._robots:
            try:
                self._throttle(p.netloc)
                r = self.session.get(base + "/robots.txt", timeout=self.timeout)
                if r.status_code in (401, 403) or r.status_code >= 500:
                    rules = _Rules(disallow_all=True)   # can't read the rules -> assume no (as major crawlers do for 5xx)
                elif r.status_code >= 400:
                    rules = _Rules(allow_all=True)      # no robots.txt -> allowed
                else:
                    rules = _Rules(r.text)
            except requests.RequestException as e:
                log.info("robots.txt unreachable for %s (%s); skipping the site to be safe", base, e)
                rules = _Rules(disallow_all=True)
            self._robots[base] = rules
        return self._robots[base].can_fetch(self.user_agent, url)

    def get(self, url: str, *, params=None, headers=None, check_robots: bool = False, auth=None,
            interval: Optional[float] = None) -> requests.Response:
        if check_robots and not self.allowed_by_robots(url):
            raise RobotsDisallowed(f"robots.txt disallows {url}")
        self._throttle(urlparse(url).netloc, interval)
        log.debug("GET %s %s", url, params or "")
        r = self.session.get(url, params=params, headers=headers, timeout=self.timeout, auth=auth)
        r.raise_for_status()
        return r

    def get_json(self, url: str, **kw):
        r = self.get(url, **kw)
        try:
            return json.loads(r.content.decode("utf-8"))  # JSON is UTF-8; don't trust charset guessing
        except UnicodeDecodeError:
            return r.json()
