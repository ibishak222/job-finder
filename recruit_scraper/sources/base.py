from __future__ import annotations

import logging
from typing import Iterable, Iterator, Optional

from ..http import HttpClient
from ..models import Job

log = logging.getLogger(__name__)


class Source:
    """Base class for a job source adapter.

    Subclasses set `name` and implement `fetch()`, yielding Job objects.
    `requires` describes what input the adapter needs: 'companies', 'urls', 'api_key' or None.
    """

    name: str = "base"
    requires: Optional[str] = None
    description: str = ""

    def __init__(self, http: HttpClient, *, keyword: Optional[str] = None, location: Optional[str] = None,
                 companies: Iterable[str] = (), urls: Iterable[str] = (), limit: Optional[int] = None,
                 api_keys: Optional[dict] = None):
        self.http = http
        self.keyword = keyword
        self.location = location
        self.companies = list(companies)
        self.urls = list(urls)
        self.limit = limit
        self.api_keys = {k: v for k, v in (api_keys or {}).items() if v}
        self.warnings: list[str] = []  # human-readable problems, shown in the UI / CLI summary

    def key(self, name: str) -> Optional[str]:
        """API key from the explicit api_keys dict (UI) or the environment (CLI / .env)."""
        import os
        return self.api_keys.get(name) or os.environ.get(name)

    def warn(self, what: str, err: Exception) -> None:
        import requests
        resp = getattr(err, "response", None)
        code = getattr(resp, "status_code", None)
        if code == 404:
            msg = f"{what}: not found (check the spelling of the company name)"
        elif code in (401, 403):
            msg = f"{what}: access refused ({code}) - check the API key"
        elif code == 429:
            msg = f"{what}: rate-limited by the site, try again later"
        elif isinstance(err, requests.ConnectionError):
            msg = f"{what}: could not connect"
        elif isinstance(err, requests.Timeout):
            msg = f"{what}: timed out"
        else:
            msg = f"{what}: {err}"
        self.warnings.append(msg)
        log.warning(msg)

    def is_configured(self) -> tuple[bool, str]:
        if self.requires == "companies" and not self.companies:
            return False, "no companies entered for this source"
        if self.requires == "urls" and not self.urls:
            return False, "no careers-page URLs entered"
        return True, ""

    def fetch(self) -> Iterator[Job]:  # pragma: no cover - abstract
        raise NotImplementedError
