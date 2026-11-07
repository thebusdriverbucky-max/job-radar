"""Remember handled jobs between runs so each one reaches Telegram once."""

import json
import logging
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Self

log = logging.getLogger(__name__)

KEEP_DAYS = 45  # far beyond max_age_days, so a forgotten job is too old to come back anyway


class SeenJobs:
    """Job keys and fingerprints with the UTC time they were handled, stored as JSON."""

    def __init__(self, path: Path, entries: dict[str, str] | None = None) -> None:
        self.path = path
        self.entries = entries or {}

    @classmethod
    def load(cls, path: Path) -> Self:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls(path)
        except (OSError, ValueError) as exc:
            log.warning("Ignoring unreadable %s: %s", path, exc)
            return cls(path)
        return cls(path, data if isinstance(data, dict) else {})

    def __contains__(self, key: str) -> bool:
        return key in self.entries

    def add(self, keys: Iterable[str], now: datetime) -> None:
        stamp = now.isoformat(timespec="seconds")
        for key in keys:
            self.entries.setdefault(key, stamp)

    def save(self, now: datetime) -> None:
        cutoff = (now - timedelta(days=KEEP_DAYS)).isoformat(timespec="seconds")
        # Same-format UTC ISO strings sort by time, so a string comparison is enough.
        self.entries = {k: v for k, v in self.entries.items() if v >= cutoff}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.entries, indent=1, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)
