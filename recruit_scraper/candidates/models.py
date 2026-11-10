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
    contact_source: str = ""      # URL of the page (or profile) where the email/phone was published
    profile_url: str = ""
    website: str = ""
    linkedin_url: str = ""        # link only - never fetched
    twitter_url: str = ""
    hireable: Optional[bool] = None
    kind: str = "candidate"       # candidate | hiring contact | business (a firm / sole trader listing, not a job seeker)
    contract: str = ""            # what their own profile says: temporary (locum/agency/bank/contract) | permanent | ""
    profile_read: Optional[bool] = None  # GitHub: False = quick-list row from search, profile not read yet
    provider: str = ""            # where it was found, e.g. "github", "bluesky", "github + mastodon"
    match_score: float = 0.0      # how well their own profile text matches the job (3 exact, 2 synonym, 1 skill)
    match_reason: str = ""        # e.g. "profile says 'electrician'"
    other_profiles: str = ""      # the same person's profiles on other sites (merged duplicates), space-separated
    last_active: str = ""         # last public activity we saw (Mastodon last post date, Bluesky feed post), YYYY-MM-DD
    fit: float = 0.0              # overall 'best match' score 0-100 (role match, distance, contactability, experience, recency)
    fit_reason: str = ""          # plain-English breakdown of the fit score
    found_at: str = field(default_factory=_now)
    personal_data: str = "yes - UK GDPR applies"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def field_names(cls) -> list[str]:
        return [f.name for f in fields(cls)]
