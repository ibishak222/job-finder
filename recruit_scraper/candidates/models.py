from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from typing import Optional


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class Candidate:
    """One person record. THIS IS PERSONAL DATA under UK GDPR - handle accordingly."""
    name: str = ""
    headline: str = ""            # bio / current title, or "Hiring contact for: <job>"
    company: str = ""
    location: str = ""
    distance_miles: Optional[float] = None
    experience_years: Optional[float] = None
    experience_source: str = ""   # e.g. "estimated from GitHub account age" - a signal, not a fact
    email: str = ""
    email_source: str = ""        # e.g. "published in job ad"
    phone: str = ""               # only if the person/company published it themselves
    profile_url: str = ""
    website: str = ""
    linkedin_url: str = ""        # link only - never fetched
    twitter_url: str = ""
    hireable: Optional[bool] = None
    kind: str = "candidate"       # candidate | hiring contact
    profile_read: Optional[bool] = None  # GitHub: False = quick-list row from search, profile not read yet
    provider: str = ""
    found_at: str = field(default_factory=_now)
    personal_data: str = "yes - UK GDPR applies"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def field_names(cls) -> list[str]:
        return [f.name for f in fields(cls)]
