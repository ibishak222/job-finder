"""Tiny JSON disk cache with TTL (data/cache). Holds public profile data -> personal data: chmod 600, auto-expiry."""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Optional

CACHE_ROOT = Path(__file__).resolve().parents[2] / "data" / "cache"


class DiskCache:
    def __init__(self, namespace: str, ttl_seconds: float, root: Path = CACHE_ROOT):
        self.dir = root / namespace
        self.ttl = ttl_seconds
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(root, 0o700)
        os.chmod(self.dir, 0o700)

    def _path(self, key: str) -> Path:
        return self.dir / (hashlib.sha256(key.encode()).hexdigest()[:32] + ".json")

    def get(self, key: str) -> Optional[Any]:
        p = self._path(key)
        if not p.exists():
            return None
        if time.time() - p.stat().st_mtime > self.ttl:
            p.unlink(missing_ok=True)
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None

    def set(self, key: str, value: Any) -> None:
        p = self._path(key)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(value), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(p)

    def purge_expired(self) -> int:
        n = 0
        for p in self.dir.glob("*.json"):
            if time.time() - p.stat().st_mtime > self.ttl:
                p.unlink(missing_ok=True)
                n += 1
        return n


def clear_all(root: Path = CACHE_ROOT) -> int:
    n = 0
    for p in root.glob("*/*.json"):
        p.unlink(missing_ok=True)
        n += 1
    return n
