"""Shared local observations and a separate, bounded atomic snapshot worker."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import random
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .client import AuthenticationError, ProtocolError, TransportError, encode_payload
from .collectors import (
    ResourceCollector,
    collect_inventory,
    metric,
    observation,
    unavailable_metrics,
)
from .const import (
    BACKOFF_CAP,
    CONF_AGENT_TOKEN,
    CONF_DISCOVER_UPDATES,
    CONF_ENABLED,
    CONF_EXPORT_ALL_UPDATES,
    CONF_EXPORT_METRICS,
    CONF_INSTANCE_ID,
    CONF_MONITORING_ENABLED,
    CONF_SYSTEM_UPDATES,
    CONF_UPDATE_ENTITIES,
    DOMAIN,
    INVENTORY_KEYS,
    MAX_MONITORING_BODY_BYTES,
    METRIC_KEYS,
    MONITORING_INTERVAL,
    MONITORING_SCHEMA_VERSION,
)
from .updates import UpdateInventory, collect_update_inventory

if TYPE_CHECKING:
    from . import AgentConfigEntry
    from .sender import HeartbeatSender

_LOGGER = logging.getLogger(__name__)


class MonitoringCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Collect even with no enabled entities, disabled uploads or a broken server."""

    def __init__(
        self, hass: HomeAssistant, entry: AgentConfigEntry, heartbeat: HeartbeatSender
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="HA Monitor local collection",
            config_entry=entry,
        )
        self._resources = ResourceCollector(hass.config.config_dir)
        self.update_inventory = UpdateInventory("unknown")
        self.export_update_inventory = UpdateInventory("unknown")
        self.uploader = MonitoringUploader(hass, entry, heartbeat)
        self.entry = entry
        self._unsubscribe = None
        self._collection_task: asyncio.Task[None] | None = None
        self._stopped = False
        entry.async_on_unload(self.async_stop)

    @callback
    def async_start(self) -> None:
        """Run the agent clock independently of HA's entity polling preference."""
        if self._stopped or self._unsubscribe is not None:
            return
        self._unsubscribe = async_track_time_interval(
            self.hass,
            self._async_collect,
            timedelta(seconds=MONITORING_INTERVAL),
            cancel_on_shutdown=True,
        )
        self._async_collect(dt_util.utcnow())

    @callback
    def _async_collect(self, _now: datetime) -> None:
        # Skip ticks during slow collection rather than queueing refresh tasks.
        if (
            self._stopped
            or self._collection_task is not None
            and not self._collection_task.done()
        ):
            return
        self._collection_task = self.entry.async_create_background_task(
            self.hass,
            self.async_refresh(),
            "HA Monitor local collection",
            eager_start=False,
        )

    @property
    def export_all(self) -> bool:
        return self.entry.options.get(CONF_EXPORT_ALL_UPDATES, True)

    def _project(self, inventory: UpdateInventory) -> dict[str, Any]:
        return inventory.as_protocol_v2("all" if self.export_all else "selected")

    def _issues(self, inventory: UpdateInventory) -> dict[str, int]:
        return inventory.v2_issues

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            metrics = await self.hass.async_add_executor_job(self._resources.collect)
        except Exception:
            # Third-party/platform errors must not disclose their exception text.
            metrics = unavailable_metrics()
        try:
            inventory = collect_inventory(self.hass)
        except Exception:
            inventory = {key: observation("unavailable") for key in INVENTORY_KEYS}
        # Export-all is independently actionable: it necessarily reads all sources
        # even if the local-only discovery preference was previously switched off.
        discover = (
            self.entry.options.get(CONF_DISCOVER_UPDATES, True) or self.export_all
        )
        self.update_inventory = self._collect_updates(discover=discover)
        # Local discovery must not expand the explicitly selected export set. Reading
        # the explicit scope separately also isolates failures in other sources.
        if self.export_all or not discover:
            self.export_update_inventory = self.update_inventory
        else:
            self.export_update_inventory = self._collect_updates(discover=False)
        self._async_update_export_issue()
        snapshot = {
            "snapshot_id": str(uuid4()),
            "observed_at": dt_util.utcnow().isoformat(),
            "metrics": metrics,
            "inventory": inventory,
            "updates": self._project(self.export_update_inventory),
        }
        await self.uploader.async_observed(snapshot)
        return snapshot

    @callback
    def _collect_updates(self, *, discover: bool) -> UpdateInventory:
        try:
            return collect_update_inventory(
                self.hass,
                self.entry.options.get(CONF_SYSTEM_UPDATES, True),
                self.entry.options.get(CONF_UPDATE_ENTITIES, []),
                discover=discover,
            )
        except Exception:
            return UpdateInventory(
                "unavailable", collection_issues=("collection_failed",)
            )

    @property
    def update_export_issue_id(self) -> str:
        return f"update_export_incomplete_{self.entry.entry_id}"

    def update_summary(self) -> dict[str, Any]:
        """Report local coverage separately from the actual configured wire scope."""
        local = self.update_inventory
        exported = self.export_update_inventory
        projection = self._project(exported)
        selected_ids = {record.entity_id for record in exported.records}
        return {
            **local.summary(),
            "schema_version": self.uploader.schema_version,
            "local_protocol_issues": self._issues(local),
            "export_scope": "all" if self.export_all else "selected",
            "export_sources": len(exported.records),
            "export_known_available": len(exported.offered),
            "export_item_count": len(projection["items"]),
            "blocked_known_available": len(exported.offered)
            - sum(
                item.get("availability", "available") in {"available", "skipped"}
                for item in projection["items"]
            ),
            "not_exported": sum(
                record.entity_id not in selected_ids for record in local.offered
            ),
            "protocol_status": projection.get("coverage", projection.get("status")),
            "protocol_issues": self._issues(exported),
        }

    @callback
    def _async_update_export_issue(self) -> None:
        """Make real export limitations visible without logging source metadata."""
        summary = self.update_summary()
        if self.uploader.enabled and (
            summary["not_exported"] or summary["protocol_issues"]
        ):
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                self.update_export_issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="update_export_incomplete",
                translation_placeholders={
                    "omitted": str(summary["not_exported"]),
                    "reason": ", ".join(sorted(summary["protocol_issues"]))
                    or "selected_sources_only",
                },
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, self.update_export_issue_id)

    async def async_stop(self) -> None:
        self._stopped = True
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        if self._collection_task is not None:
            self._collection_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._collection_task
            self._collection_task = None
        await self.async_shutdown()
        await self.uploader.async_stop()


@dataclass(frozen=True, slots=True)
class FrozenSnapshot:
    """Serialized once off the event loop; immutable contents and observation time."""

    body: bytes
    observed_at: datetime


class MonitoringUploader:
    """One in-flight snapshot plus the latest coalesced observation, in memory.

    Frozen serialized observations prevent accidental mutation on retry. A maximum
    of four monitoring requests/min stays below the v2 instance quota of six.
    """

    def __init__(
        self, hass: HomeAssistant, entry: AgentConfigEntry, heartbeat: HeartbeatSender
    ) -> None:
        self.hass, self.entry, self.heartbeat = hass, entry, heartbeat
        self.status = "paused"
        self.last_error: str | None = None
        self.last_upload: datetime | None = None
        self.failures = 0
        self.dropped_snapshots = 0
        self.schema_version = MONITORING_SCHEMA_VERSION
        self.max_body_bytes = MAX_MONITORING_BODY_BYTES
        self._pending: FrozenSnapshot | None = None
        self._latest: FrozenSnapshot | None = None
        self._send_lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._delay = 15.0
        self._stopped = False
        self._next_attempt: datetime | None = None

    @property
    def issue_id(self) -> str:
        return f"monitoring_blocked_{self.entry.entry_id}"

    @property
    def pending_snapshots(self) -> int:
        return int(self._pending is not None) + int(self._latest is not None)

    @property
    def enabled(self) -> bool:
        return self.entry.options.get(CONF_ENABLED, True) and self.entry.options.get(
            CONF_MONITORING_ENABLED, False
        )

    async def async_observed(self, snapshot: dict[str, Any]) -> None:
        if (
            not self.enabled
            or self._stopped
            or self.status in {"blocked", "reauth_required"}
        ):
            return
        self._pending = self._valid(self._pending, dt_util.utcnow())
        # Construct a separate export view; the local shared data stays complete.
        exported = {
            **snapshot,
            "metrics": {
                key: value
                if key in self.entry.options.get(CONF_EXPORT_METRICS, METRIC_KEYS)
                else metric("unsupported", None)
                for key, value in snapshot["metrics"].items()
            },
        }
        # Large collections must not perform JSON serialization on HA's event loop.
        frozen = FrozenSnapshot(
            await self.hass.async_add_executor_job(encode_payload, exported),
            datetime.fromisoformat(snapshot["observed_at"]),
        )
        if self._stopped or self.status in {"blocked", "reauth_required"}:
            return
        envelope = self._envelope(frozen)
        if len(envelope) > self.max_body_bytes:
            # No independent fragments or empty replacement: retain server state.
            self._block("payload_too_large")
            return
        self.dropped_snapshots += int(self._latest is not None)
        self._latest = frozen
        self._wake.set()

    @callback
    def async_start(self) -> None:
        if (
            self.enabled
            and not self._stopped
            and self.status not in {"blocked", "reauth_required"}
        ):
            self.status = "waiting"
            self._task = self.entry.async_create_background_task(
                self.hass,
                self._async_run(),
                "HA Monitor monitoring upload",
                eager_start=False,
            )

    async def _async_run(self) -> None:
        while not self._stopped and self.status not in {"blocked", "reauth_required"}:
            await self._wake.wait()
            self._wake.clear()
            await self.async_send_once()
            await asyncio.sleep(self._delay)
            if self.pending_snapshots:
                self._wake.set()

    def _envelope(self, snapshot: FrozenSnapshot) -> bytes:
        header = encode_payload(
            {
                "schema_version": self.schema_version,
                "instance_id": self.entry.data[CONF_INSTANCE_ID],
                "sent_at": dt_util.utcnow().isoformat(),
            }
        )
        return header[:-1] + b',"snapshots":[' + snapshot.body + b"]}"

    def _valid(
        self, frozen: FrozenSnapshot | None, now: datetime
    ) -> FrozenSnapshot | None:
        if frozen is not None:
            observed = frozen.observed_at
            if now - timedelta(hours=23) <= observed <= now:
                return frozen
            self.dropped_snapshots += 1
        return None

    async def async_send_once(self) -> None:
        if self._send_lock.locked():
            return
        async with self._send_lock:
            await self._async_send_once()
        # An older response must not undo revocation on the heartbeat path.
        if self.heartbeat.status == "reauth_required":
            self.status, self.last_error = "reauth_required", "invalid_auth"

    @callback
    def _block(self, reason: str) -> None:
        self.status, self.last_error = "blocked", reason
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self.issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="monitoring_blocked",
            translation_placeholders={"reason": reason},
        )

    async def _async_send_once(self) -> None:
        if (
            not self.enabled
            or self._stopped
            or self.status in {"blocked", "reauth_required"}
        ):
            return
        if self.heartbeat.status == "reauth_required":
            self.status = "reauth_required"
            return
        now = dt_util.utcnow()
        if self._next_attempt is not None and now < self._next_attempt:
            return
        self._pending = self._valid(self._pending, now)
        self._latest = self._valid(self._latest, now)
        if self._pending is None:
            self._pending, self._latest = self._latest, None
        if self._pending is None:
            return
        try:
            body = self._envelope(self._pending)
            if len(body) > self.max_body_bytes:
                raise ProtocolError("payload_too_large")
            received_at = await self.heartbeat.client.async_monitoring(
                body,
                self.entry.data[CONF_AGENT_TOKEN],
                1,
            )
        except AuthenticationError:
            self.status, self.last_error = "reauth_required", "invalid_auth"
            self.heartbeat.async_auth_failed()
            return
        except ProtocolError as err:
            if self.status != "blocked":
                self._block(str(err))
            return
        except TransportError as err:
            if self._stopped or self.status == "blocked":
                return
            self.failures += 1
            backoff = min(BACKOFF_CAP, 15 * 2 ** min(self.failures - 1, 10))
            self._delay = max(
                15, random.uniform(backoff / 2, backoff), err.retry_after or 0
            )
            self.status, self.last_error = "retrying", str(err)
        else:
            if (
                self._stopped
                or self.status == "blocked"
                or self.heartbeat.status == "reauth_required"
            ):
                return
            self.last_upload = received_at
            self._pending = None
            self.status, self.last_error = "online", None
            self.failures = 0
            self._delay = 15
            ir.async_delete_issue(self.hass, DOMAIN, self.issue_id)
        self._next_attempt = dt_util.utcnow() + timedelta(seconds=self._delay)

    async def async_stop(self) -> None:
        self._stopped = True
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self._pending = self._latest = None
