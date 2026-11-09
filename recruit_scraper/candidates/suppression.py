"""Do-not-contact (suppression) list: emails, @domains, profile URLs or GitHub usernames, one per line."""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

from .models import Candidate

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "data" / "suppression.txt"


def load(path: Path = DEFAULT_PATH) -> list[str]:
    if not path.exists():
        return []
    return parse(path.read_text(encoding="utf-8"))


def parse(text: str) -> list[str]:
    return [l.strip().lower() for l in text.splitlines() if l.strip() and not l.strip().startswith("#")]


def save(entries: Iterable[str], path: Path = DEFAULT_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    uniq = sorted({e.strip().lower() for e in entries if e.strip()})
    path.write_text("# Do-not-contact list: one email, @domain, profile URL or username per line\n"
                    + "".join(f"{e}\n" for e in uniq), encoding="utf-8")


def is_suppressed(c: Candidate, entries: list[str]) -> bool:
    email = (c.email or "").lower()
    url = (c.profile_url or "").lower().rstrip("/")
    login = url.rsplit("/", 1)[-1] if "github.com/" in url else ""
    for e in entries:
        e = e.rstrip("/")
        if not e:
            continue
        if email and (e == email or (e.startswith("@") and email.endswith(e))):
            return True
        if url and (e == url or e == url.split("://", 1)[-1]):
            return True
        if login and e in (login, f"github.com/{login}"):
            return True
    return False


def apply(cands: list[Candidate], entries: list[str]) -> tuple[list[Candidate], int]:
    kept = [c for c in cands if not is_suppressed(c, entries)]
    return kept, len(cands) - len(kept)
