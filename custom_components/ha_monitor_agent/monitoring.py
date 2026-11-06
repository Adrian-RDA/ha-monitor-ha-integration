"""Shared local observations and a separate, bounded Monitoring Snapshot v1 worker."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime, timedelta
import json
import logging
import random
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
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
    CONF_EXPORT_METRICS,
    CONF_INSTANCE_ID,
    CONF_MONITORING_ENABLED,
    CONF_SYSTEM_UPDATES,
    CONF_UPDATE_ENTITIES,
    DOMAIN,
    INVENTORY_KEYS,
    MAX_BODY_BYTES,
    METRIC_KEYS,
    MONITORING_INTERVAL,
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
            update_interval=timedelta(seconds=MONITORING_INTERVAL),
        )
        self._resources = ResourceCollector(hass.config.config_dir)
        self.update_inventory = UpdateInventory("unknown")
        self.uploader = MonitoringUploader(hass, entry, heartbeat)
        # Keep collection subscribed independently of optional local entities.
        self._unsubscribe = self.async_add_listener(self._async_observed)
        self.entry = entry

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
        try:
            self.update_inventory = collect_update_inventory(
                self.hass,
                self.entry.options.get(CONF_SYSTEM_UPDATES, True),
                self.entry.options.get(CONF_UPDATE_ENTITIES, []),
                discover=self.entry.options.get(CONF_DISCOVER_UPDATES, True),
            )
        except Exception:
            self.update_inventory = UpdateInventory(
                "unavailable", collection_issues=("collection_failed",)
            )
        return {
            "snapshot_id": str(uuid4()),
            "observed_at": dt_util.utcnow().isoformat(),
            "metrics": metrics,
            "inventory": inventory,
            "updates": self.update_inventory.as_protocol(),
        }

    @callback
    def _async_observed(self) -> None:
        if self.data:
            self.uploader.async_observed(self.data)

    async def async_stop(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        await self.async_shutdown()
        await self.uploader.async_stop()


class MonitoringUploader:
    """One in-flight snapshot plus the latest coalesced observation, in memory.

    Frozen serialized observations prevent accidental mutation on retry. A maximum
    of four monitoring requests/min leaves room for the existing heartbeat worker.
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
        self._pending: bytes | None = None
        self._latest: bytes | None = None
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

    @callback
    def async_observed(self, snapshot: dict[str, Any]) -> None:
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
        frozen = encode_payload(exported)
        envelope = self._envelope(frozen)
        if len(envelope) > MAX_BODY_BYTES:
            # Preserve a complete snapshot, explicitly marking excessive updates.
            exported["updates"] = {"status": "unavailable", "items": []}
            frozen = encode_payload(exported)
        self.dropped_snapshots += int(self._latest is not None)
        self._latest = frozen
        self._wake.set()

    @callback
    def async_start(self) -> None:
        if self.enabled:
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

    def _envelope(self, snapshot: bytes) -> bytes:
        return encode_payload(
            {
                "schema_version": 1,
                "instance_id": self.entry.data[CONF_INSTANCE_ID],
                "sent_at": dt_util.utcnow().isoformat(),
                "snapshots": [json.loads(snapshot)],
            }
        )

    def _valid(self, frozen: bytes | None, now: datetime) -> bytes | None:
        if frozen is not None:
            observed = datetime.fromisoformat(json.loads(frozen)["observed_at"])
            if now - timedelta(hours=23) <= observed <= now:
                return frozen
            self.dropped_snapshots += 1
        return None

    async def async_send_once(self) -> None:
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
            if len(body) > MAX_BODY_BYTES:
                raise ProtocolError("payload_too_large")
            self.last_upload = await self.heartbeat.client.async_monitoring(
                body, self.entry.data[CONF_AGENT_TOKEN], 1
            )
        except AuthenticationError:
            self.status, self.last_error = "reauth_required", "invalid_auth"
            self.heartbeat.async_auth_failed()
            return
        except ProtocolError as err:
            self.status, self.last_error = "blocked", str(err)
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                self.issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key="monitoring_blocked",
                translation_placeholders={"reason": str(err)},
            )
            return
        except TransportError as err:
            self.failures += 1
            backoff = min(BACKOFF_CAP, 15 * 2 ** min(self.failures - 1, 10))
            self._delay = max(
                15, random.uniform(backoff / 2, backoff), err.retry_after or 0
            )
            self.status, self.last_error = "retrying", str(err)
        else:
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
