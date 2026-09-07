"""session_id -> owning upstream map, persisted to disk.

Why persist at all: the extension keeps its sessions in browser storage, so a tab
opened before the router restarted is still there afterwards. If the map lived only
in memory, every restart would turn every in-flight conversation into a new one on a
possibly different browser -- silently, because the extension answers an unknown
session id by starting a fresh session rather than erroring.

The file is small (one line per live conversation) so it is written synchronously on
change; the cost is microseconds and it removes any window where a crash loses the
mapping. Writes go through a temp file + os.replace so a torn write can never leave
half a JSON document behind.

Failing to persist is never fatal: the map is a cache of something the browsers are
the real owners of, so I/O errors are logged and swallowed.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class SessionRecord:
    upstream: str
    provider: Optional[str] = None
    created_at: datetime = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.created_at is None:
            self.created_at = _utcnow()
        elif self.created_at.tzinfo is None:
            # Records written by an older build (or hand-edited) may be naive; treat
            # them as UTC so the age comparison in prune() cannot raise.
            self.created_at = self.created_at.replace(tzinfo=timezone.utc)

    def to_json(self) -> dict:
        return {
            "upstream": self.upstream,
            "provider": self.provider,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_json(cls, data: dict) -> "SessionRecord":
        created_raw = data.get("created_at")
        created_at = None
        if isinstance(created_raw, str):
            try:
                created_at = datetime.fromisoformat(created_raw)
            except ValueError:
                created_at = None
        return cls(
            upstream=data["upstream"],
            provider=data.get("provider"),
            created_at=created_at,
        )


class SessionMap:
    def __init__(self, path: Path, ttl_hours: float = 24.0) -> None:
        self.path = Path(path).expanduser()
        self.ttl_hours = ttl_hours
        self._records: dict[str, SessionRecord] = {}

    # --- persistence ----------------------------------------------------------

    def load(self) -> None:
        """Populate from disk. A missing or corrupt file starts an empty map."""
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("session map %s unreadable (%s); starting empty", self.path, exc)
            return

        if not isinstance(raw, dict):
            logger.warning("session map %s is not a JSON object; starting empty", self.path)
            return

        for session_id, data in raw.items():
            if not isinstance(data, dict) or "upstream" not in data:
                continue
            try:
                self._records[session_id] = SessionRecord.from_json(data)
            except Exception as exc:  # noqa: BLE001 - one bad row must not lose the rest
                logger.warning("skipping session %s in %s: %s", session_id, self.path, exc)

        pruned = self.prune()
        if pruned:
            self.save()
        logger.info("loaded %d session(s) from %s", len(self._records), self.path)

    def save(self) -> None:
        payload = {sid: record.to_json() for sid, record in self._records.items()}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Same directory as the target, otherwise os.replace could cross a
            # filesystem boundary and stop being atomic.
            tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            # 0600 before the rename, not after: a session id is a bearer capability
            # (anyone holding one can continue that conversation through the router),
            # and on a shared box the default 0644 would hand every local user the
            # whole map. Setting the mode on the temp file means the target is never
            # briefly world-readable. Same posture as router.json and the unix socket.
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except OSError as exc:
            logger.warning("could not persist session map to %s: %s", self.path, exc)

    # --- lookups --------------------------------------------------------------

    def get(self, session_id: str) -> Optional[SessionRecord]:
        record = self._records.get(session_id)
        if record is None:
            return None
        if self._expired(record):
            self.forget(session_id)
            return None
        return record

    def owner(self, session_id: str) -> Optional[str]:
        record = self.get(session_id)
        return record.upstream if record else None

    def owned_by(self, upstream: str) -> int:
        return sum(1 for record in self._records.values() if record.upstream == upstream)

    def ids(self) -> Iterable[str]:
        return tuple(self._records)

    def __len__(self) -> int:
        return len(self._records)

    def __contains__(self, session_id: object) -> bool:
        return self.get(str(session_id)) is not None

    # --- mutations ------------------------------------------------------------

    def record(
        self,
        session_id: str,
        upstream: str,
        provider: Optional[str] = None,
        created_at: Optional[datetime] = None,
    ) -> None:
        """Remember who owns a session. Writes only when something actually changed.

        Follow-ups on an existing conversation re-record the same pair on every
        prompt; skipping the no-op write keeps a chatty caller from rewriting the
        file dozens of times a minute.
        """
        existing = self._records.get(session_id)
        if (
            existing is not None
            and existing.upstream == upstream
            and (provider is None or existing.provider == provider)
        ):
            return
        self._records[session_id] = SessionRecord(
            upstream=upstream,
            provider=provider if provider is not None else (existing.provider if existing else None),
            created_at=created_at or (existing.created_at if existing else None),
        )
        self.save()

    def forget(self, session_id: str) -> bool:
        if self._records.pop(session_id, None) is None:
            return False
        self.save()
        return True

    def forget_upstream(self, upstream: str) -> int:
        """Drop every session owned by an upstream that vanished from the config."""
        stale = [sid for sid, rec in self._records.items() if rec.upstream == upstream]
        for sid in stale:
            del self._records[sid]
        if stale:
            self.save()
        return len(stale)

    def prune(self, now: Optional[datetime] = None) -> int:
        """Drop entries older than the TTL. ttl_hours <= 0 disables ageing."""
        if self.ttl_hours <= 0:
            return 0
        now = now or _utcnow()
        expired = [sid for sid, rec in self._records.items() if self._expired(rec, now)]
        for sid in expired:
            del self._records[sid]
        return len(expired)

    def _expired(self, record: SessionRecord, now: Optional[datetime] = None) -> bool:
        if self.ttl_hours <= 0:
            return False
        now = now or _utcnow()
        return record.created_at + timedelta(hours=self.ttl_hours) < now
