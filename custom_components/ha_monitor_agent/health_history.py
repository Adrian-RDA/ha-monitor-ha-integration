"""Private, bounded local evidence; never a source for current Health v1 status."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN

STORAGE_TIMEOUT = 5
_FIELDS = frozenset(
    {"status", "observed_at", "last_attempt_at", "last_success_at", "error_code"}
)
_ERRORS = frozenset(
    {
        "authentication",
        "connection",
        "storage",
        "insufficient_space",
        "timeout",
        "unknown",
    }
)


def backup_history_store(hass: HomeAssistant, entry_id: str) -> Store:
    """One fixed-size record per entry, protected by HA's private atomic store."""
    return Store(
        hass, 1, f"{DOMAIN}.{entry_id}.backup", private=True, atomic_writes=True
    )


def _validated(data: Any) -> dict[str, Any] | None:
    """Reject corrupt, future, unconfirmed or unexpected persisted metadata."""
    if not isinstance(data, dict) or data.keys() != _FIELDS:
        return None
    status, error = data["status"], data["error_code"]
    if status not in ("ok", "failed"):
        return None
    if status == "ok" and error is not None:
        return None
    if status == "failed" and (not isinstance(error, str) or error not in _ERRORS):
        return None
    times: dict[str, datetime | None] = {}
    for key in ("observed_at", "last_attempt_at", "last_success_at"):
        value = data[key]
        if value is None and key == "last_success_at" and status == "failed":
            times[key] = None
            continue
        if not isinstance(value, str) or not 1 <= len(value) <= 40:
            return None
        parsed = dt_util.parse_datetime(value)
        if parsed is None or parsed.tzinfo is None or parsed > dt_util.utcnow():
            return None
        times[key] = parsed
    observed, attempt, success = (
        times[key] for key in ("observed_at", "last_attempt_at", "last_success_at")
    )
    if attempt > observed or success and success > observed:
        return None
    if success and (success < attempt if status == "ok" else success > attempt):
        return None
    return dict(data)


class BackupHistory:
    """Retain only a confirmed terminal lifecycle, coalescing pending writes.

    The native manager cannot corroborate saved events after an observation gap.
    Loaded records therefore remain historical and are exposed only in local
    diagnostics. They never seed BackupCollector or a new report timestamp.
    """

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self.store = backup_history_store(hass, entry_id)
        self._data: dict[str, Any] | None = None
        self._dirty = False

    @property
    def last_confirmed(self) -> dict[str, Any] | None:
        # Revalidate on read too: an intervening clock rollback invalidates ages.
        return _validated(self._data)

    async def async_load(self) -> None:
        try:
            loaded = _validated(await self.store.async_load())
        except Exception:
            return
        # A live observation during disk I/O must always win over restored data.
        if self._data is None:
            self._data = loaded

    @callback
    def observed(self, data: dict[str, Any]) -> None:
        if (confirmed := _validated({key: data[key] for key in _FIELDS})) is None:
            return
        previous = self.last_confirmed
        if (
            confirmed["last_success_at"] is None
            and previous
            and previous["last_success_at"]
            and datetime.fromisoformat(previous["last_success_at"])
            <= datetime.fromisoformat(confirmed["last_attempt_at"])
        ):
            # Preserve a known older success in local history only. It cannot
            # establish the latest success across the observation gap for v1.
            confirmed["last_success_at"] = previous["last_success_at"]
        self._data = confirmed
        self._dirty = True
        # HA handles executor I/O, atomic replacement and its final-write event.
        self.store.async_delay_save(lambda: dict(self._data), delay=1)

    async def async_flush(self) -> None:
        if not self._dirty:
            return
        try:
            async with asyncio.timeout(STORAGE_TIMEOUT):
                await self.store.async_save(dict(self._data))
        except Exception:
            # Persistence is best effort and cannot gate another agent worker.
            return
        self._dirty = False
