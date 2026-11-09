from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from typing import Optional


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class Job:
    title: str
    company: str = ""
    location: str = ""
    salary_min: Optional[float] = None
    salary_max: Optional[float] = None
    salary_currency: Optional[str] = None
    salary_period: Optional[str] = None  # year | month | week | day | hour
    salary_raw: Optional[str] = None
    posted_date: Optional[str] = None  # ISO 8601 date
    description: str = ""
    url: str = ""
    source: str = ""
    employment_type: Optional[str] = None
    remote: Optional[bool] = None
    contact_email: Optional[str] = None  # only if published in the posting itself
    contact_phone: Optional[str] = None  # only if published in the posting itself
    scraped_at: str = field(default_factory=_now)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def field_names(cls) -> list[str]:
        return [f.name for f in fields(cls)]
