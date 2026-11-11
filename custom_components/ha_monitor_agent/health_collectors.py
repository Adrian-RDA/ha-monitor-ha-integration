"""Read-only Health v1 collectors. No transport or raw attributes."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime
import hashlib
import heapq
import re
from typing import Any
import unicodedata

from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

COLLECTION_CAPS = {"repairs": 100, "config_entries": 256, "unavailable_entities": 1000}
ENTRY_STATES = {
    "setup_error",
    "setup_retry",
    "migration_error",
    "failed_unload",
    "not_loaded",
    "setup_in_progress",
    "unload_in_progress",
}
# This is a wire-state allowlist, not an error list. In particular NOT_LOADED
# proves no failure; setup/unload-in-progress are transitional observations.


def neutral_collection(coverage: str, scope: str = "all") -> dict[str, Any]:
    return {"coverage": coverage, "scope": scope, "total": None, "items": []}


def neutral_backup(coverage: str = "unknown") -> dict[str, Any]:
    return {
        "coverage": coverage,
        "status": "unknown",
        "last_success_at": None,
        "last_attempt_at": None,
        "error_code": None,
    }


def source_key(value: str, instance_id: str) -> str:
    """Keep valid local source IDs; scope fallback digests to this installation."""
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value):
        return value
    return "sha256:" + hashlib.sha256(f"{instance_id}\0{value}".encode()).hexdigest()


def _domain(value: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value):
        raise ValueError("invalid_domain")
    return value


def _collection(
    sources: Iterable, project: Callable, cap: int, scope: str
) -> dict[str, Any]:
    total = 0
    incomplete = False

    def findings():
        nonlocal total, incomplete
        try:
            for source in sources:
                try:
                    item = project(source)
                except Exception:
                    incomplete = True
                    continue
                if item is not None:
                    total += 1
                    yield item
        except Exception:
            incomplete = True

    # The heap retains only the deterministic capped subset, even for large HA registries.
    items = heapq.nsmallest(
        cap, findings(), key=lambda item: item.get("key", item.get("entity_id"))
    )
    if total > 1_000_000:
        incomplete = True
    return {
        "coverage": "partial" if incomplete or total > cap else "complete",
        "scope": scope,
        "total": None if incomplete else total,
        "items": items,
    }


def collect_repairs(hass: HomeAssistant, instance_id: str) -> dict[str, Any]:
    def project(issue):
        # Persisted issues that were not raised again this session are inactive.
        if not issue.active:
            return None
        if issue.severity not in {"critical", "error", "warning"}:
            raise ValueError("unsupported_severity")
        return {
            "key": source_key(f"{issue.domain}:{issue.issue_id}", instance_id),
            "domain": _domain(issue.domain),
            "name": None,
            "status": "ignored" if issue.dismissed_version is not None else "open",
            "severity": issue.severity.value,
        }

    return _collection(ir.async_get(hass).issues.values(), project, 100, "all")


def approved_name(value: Any) -> str | None:
    """Only explicitly approved registry labels, bounded without rewriting them."""
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 120
        or any(
            unicodedata.category(char).startswith("C")
            or unicodedata.category(char) in {"Zl", "Zp"}
            for char in value
        )
    ):
        return None
    return value


def collect_config_entries(
    hass: HomeAssistant, instance_id: str, *, include_names: bool = False
) -> dict[str, Any]:
    def project(entry):
        if entry.disabled_by is not None or entry.state.value == "loaded":
            return None
        if entry.state.value not in ENTRY_STATES:
            raise ValueError("unsupported_entry_state")
        return {
            "key": source_key(entry.entry_id, instance_id),
            "domain": _domain(entry.domain),
            "name": approved_name(entry.title) if include_names else None,
            "state": entry.state.value,
        }

    return _collection(hass.config_entries.async_entries(), project, 256, "all")


def collect_unavailable_entities(
    hass: HomeAssistant,
    instance_id: str,
    *,
    all_entities: bool = False,
    selected: Iterable[str] = (),
    include_devices: bool = False,
    include_names: bool = False,
) -> dict[str, Any]:
    registry = er.async_get(hass)
    devices = dr.async_get(hass) if include_devices else None
    scope = "all" if all_entities else "selected"

    def project(state):
        if state is None or state.state != STATE_UNAVAILABLE:
            return None
        entity_id = state.entity_id
        entry = registry.async_get(entity_id)
        if entry is not None and entry.disabled_by is not None:
            return None
        if (
            not re.fullmatch(r"[a-z_][a-z0-9_]*\.[a-z0-9_]+", entity_id)
            or len(entity_id) > 256
        ):
            raise ValueError("invalid_entity_id")
        device = (
            devices.async_get(entry.device_id)
            if devices and entry and entry.device_id
            else None
        )
        return {
            "entity_id": entity_id,
            "name": approved_name(entry.name or entry.original_name)
            if include_names and entry
            else None,
            "device": {
                "id": source_key(device.id, instance_id),
                "name": approved_name(device.name_by_user or device.name)
                if include_names
                else None,
            }
            if device
            else None,
        }

    # Missing/unknown states are not findings. Selection itself does not promise availability.
    sources = (
        hass.states.async_all()
        if all_entities
        else (hass.states.get(entity_id) for entity_id in dict.fromkeys(selected))
    )
    return _collection(sources, project, 1000, scope)


class BackupCollector:
    """Observe complete native backup lifecycles, without guessing past outcomes.

    HA 2026.10/11 CreateBackupEvent has no timestamp. The synchronous subscription
    timestamps start/completion as they happen. Stored automatic-backup dates
    cannot describe manual attempts, so they are deliberately not merged here.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        on_confirmed: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.hass = hass
        self._on_confirmed = on_confirmed
        self._manager = None
        self._unsubscribe = None
        self._data = neutral_backup()

    @callback
    def attach(self) -> str:
        try:
            from homeassistant.components.backup import async_get_manager
            from homeassistant.components.backup.manager import BackupManager
        except ImportError:
            self.stop()
            return "unsupported"
        try:
            manager = async_get_manager(self.hass)
        except Exception:
            self.stop()
            return (
                "unavailable"
                if "backup" in self.hass.config.components
                else "unsupported"
            )
        if not isinstance(manager, BackupManager) or not callable(
            getattr(manager, "async_subscribe_events", None)
        ):
            self.stop()
            return "unsupported"
        if manager is not self._manager:
            self.stop()
            # Publish attachment only after subscription succeeds. Otherwise a
            # transient registration error would suppress all later retries.
            unsubscribe = manager.async_subscribe_events(self._on_event)
            self._manager = manager
            self._unsubscribe = unsubscribe
        return "complete"

    @callback
    def _on_event(self, event) -> None:
        # A source callback must never propagate errors into the backup operation.
        try:
            self._observe(event)
        except Exception:
            self._data = neutral_backup("unavailable")

    def _observe(self, event) -> None:
        from homeassistant.components.backup.manager import (
            CreateBackupEvent,
            CreateBackupState,
        )

        if not isinstance(event, CreateBackupEvent):
            return
        now = dt_util.utcnow().isoformat()
        if event.state == CreateBackupState.IN_PROGRESS:
            if event.stage is None:
                self._data = {
                    "coverage": "complete",
                    "status": "running",
                    "last_attempt_at": now,
                    "last_success_at": self._data["last_success_at"],
                    "error_code": None,
                }
            elif self._data["status"] != "running":
                self._data = neutral_backup()
        elif event.state in {CreateBackupState.COMPLETED, CreateBackupState.FAILED}:
            if self._data["status"] != "running":
                # Subscribed mid-run: completion alone cannot recover the start time.
                self._data = neutral_backup()
                return
            success = event.state == CreateBackupState.COMPLETED
            self._data = {
                **self._data,
                "status": "ok" if success else "failed",
                "last_success_at": now if success else self._data["last_success_at"],
                "error_code": None if success else self._classify(event.reason),
            }
            # Reject clock rollback before retaining any confirmed history. I/O
            # failures must not erase a valid live observation or escape to HA.
            if self._snapshot(datetime.fromisoformat(now))["status"] in {
                "ok",
                "failed",
            }:
                if self._on_confirmed is not None:
                    try:
                        self._on_confirmed({**self._data, "observed_at": now})
                    except Exception:
                        pass

    @staticmethod
    def _classify(reason: str | None) -> str:
        # Exact native codes only. Generic agent/manager/upload errors have no safe
        # finer classification; never search raw messages or infer storage success.
        return {"backup_agent_unreachable": "connection"}.get(reason, "unknown")

    @callback
    def collect(self, observed_at: datetime) -> dict[str, Any]:
        coverage = self.attach()
        if coverage != "complete":
            return neutral_backup(coverage)
        return self._snapshot(observed_at)

    def _snapshot(self, observed_at: datetime) -> dict[str, Any]:
        data = self._data
        attempt, success = data["last_attempt_at"], data["last_success_at"]
        # Clock rollback cannot produce invalid/future or inverted backup times.
        if any(
            value and datetime.fromisoformat(value) > observed_at
            for value in (attempt, success)
        ) or (
            attempt
            and success
            and (
                (data["status"] == "ok" and success < attempt)
                or (data["status"] != "ok" and success > attempt)
            )
        ):
            self._data = neutral_backup()
        return dict(self._data)

    @callback
    def stop(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
        self._unsubscribe = self._manager = None
        self._data = neutral_backup()
