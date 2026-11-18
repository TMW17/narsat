"""Producer-side duplicate suppression for the FIRMS poller.

FIRMS has no "changes since" parameter: every poll returns all detections of the
requested days. Without a memory the poller would republish each detection on every
cycle. ``SeenCache`` remembers which hotspot identifiers were already delivered and
for which acquisition day, persists that to a small JSON file and forgets entries
once their day can no longer be returned by a poll.

Losing the file is harmless: the stream sink is idempotent, so the only cost is one
extra copy of the recent detections on the topic.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
from pathlib import Path


class SeenCache:
    def __init__(self, path: Path | None = None, retention_days: int = 8) -> None:
        self._path = path
        self._retention_days = retention_days
        self._seen: dict[str, str] = {}  # hotspot id -> acquisition day (ISO)
        if path is not None and path.exists():
            self._load(path)

    def _load(self, path: Path) -> None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            seen = payload["seen"]
            if isinstance(seen, dict):
                self._seen = {str(k): str(v) for k, v in seen.items()}
        except (OSError, ValueError, KeyError, TypeError):
            # A corrupt state file must never stop ingestion; start empty instead.
            self._seen = {}

    def __len__(self) -> int:
        return len(self._seen)

    def __contains__(self, hotspot_id: object) -> bool:
        return str(hotspot_id) in self._seen

    def add(self, hotspot_id: object, acq_day: dt.date) -> None:
        self._seen[str(hotspot_id)] = acq_day.isoformat()

    def prune(self, today: dt.date) -> int:
        """Forget detections older than the retention window. Returns how many went."""
        cutoff = (today - dt.timedelta(days=self._retention_days)).isoformat()
        stale = [key for key, day in self._seen.items() if day < cutoff]
        for key in stale:
            del self._seen[key]
        return len(stale)

    def save(self) -> None:
        """Write atomically: a crash mid-write leaves the previous file intact."""
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=self._path.parent, prefix=".poller_state_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "seen": self._seen}, handle)
            os.replace(tmp_name, self._path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
