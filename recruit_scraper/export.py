from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Iterable

from .models import Job


def write_json(jobs: Iterable[Job], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump([j.to_dict() for j in jobs], f, ensure_ascii=False, indent=2)
    return path


def write_csv(jobs: Iterable[Job], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig so Excel opens £/€ correctly
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=Job.field_names())
        w.writeheader()
        for j in jobs:
            w.writerow({k: ("" if v is None else v) for k, v in j.to_dict().items()})
    return path
