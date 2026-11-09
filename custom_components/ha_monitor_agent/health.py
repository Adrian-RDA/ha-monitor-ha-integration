"""Independent five-minute Health observations and bounded Health v1 export."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from copy import deepcopy
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from homeassistant.const import EVENT_COMPONENT_LOADED
from homeassistant.core import CoreState, HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .client import encode_payload
from .const import (
    CONF_AGENT_TOKEN,
    CONF_ENABLED,
    CONF_HEALTH_ALL_ENTITIES,
    CONF_HEALTH_DEVICES,
    CONF_HEALTH_ENABLED,
    CONF_HEALTH_ENTITIES,
    CONF_HEALTH_SECTIONS,
    CONF_INSTANCE_ID,
    HEALTH_INTERVAL,
    HEALTH_SECTIONS,
    MAX_HEALTH_BODY_BYTES,
)
from .health_collectors import (
    BackupCollector,
    collect_config_entries,
    collect_repairs,
    collect_unavailable_entities,
    neutral_backup,
    neutral_collection,
)
from .monitoring import FrozenSnapshot, MonitoringUploader

if TYPE_CHECKING:
    from . import AgentConfigEntry
    from .sender import HeartbeatSender


def fit_report(
    report: dict[str, Any], instance_id: str, max_bytes: int
) -> dict[str, Any]:
    """Deterministic subset, measured with the actual wire encoder and envelope.

    Reserve the maximum serialized sent_at length (UTC microseconds). Do not
    mutate collector results or split a replacement into multiple requests.
    """
    exported = deepcopy(report)

    def size():
        return len(
            encode_payload(
                {
                    "schema_version": 1,
                    "instance_id": instance_id,
                    "sent_at": "9999-12-31T23:59:59.999999+00:00",
                    "report": exported,
                }
            )
        )

    current = size()
    # Drop the lexically last retained entity first, then entries, then repairs.
    for section in ("unavailable_entities", "config_entries", "repairs"):
        collection = exported[section]
        while current > max_bytes and collection["items"]:
            removed = collection["items"].pop()
            current -= len(encode_payload(removed))
            collection["coverage"] = "partial"
        current = size()
    return exported


class HealthUploader(MonitoringUploader):
    """Reuse the verified worker mechanics with separate state, quota and endpoint."""

    issue_translation_key = "health_blocked"

    def __init__(self, hass, entry, heartbeat) -> None:
        super().__init__(hass, entry, heartbeat)
        self.schema_version = 1
        self.max_body_bytes = MAX_HEALTH_BODY_BYTES

    @property
    def enabled(self) -> bool:
        return self.entry.options.get(CONF_ENABLED, True) and self.entry.options.get(
            CONF_HEALTH_ENABLED, False
        )

    async def async_observed(self, report: dict[str, Any]) -> None:
        if (
            not self.enabled
            or self._stopped
            or self.status in {"blocked", "reauth_required"}
        ):
            return
        self._pending = self._valid(self._pending, dt_util.utcnow())
        exported = await self.hass.async_add_executor_job(
            fit_report, report, self.entry.data[CONF_INSTANCE_ID], self.max_body_bytes
        )
        await self._async_queue(exported)

    def _envelope(self, snapshot: FrozenSnapshot) -> bytes:
        header = encode_payload(
            {
                "schema_version": 1,
                "instance_id": self.entry.data[CONF_INSTANCE_ID],
                "sent_at": dt_util.utcnow().isoformat(),
            }
        )
        return header[:-1] + b',"report":' + snapshot.body + b"}"

    async def _async_upload(self, body: bytes) -> datetime:
        return await self.heartbeat.client.async_health(
            body, self.entry.data[CONF_AGENT_TOKEN]
        )


class HealthCoordinator:
    """No sensors or network calls in collectors; at most one active collection."""

    def __init__(
        self, hass: HomeAssistant, entry: AgentConfigEntry, heartbeat: HeartbeatSender
    ) -> None:
        self.hass, self.entry = hass, entry
        self.uploader = HealthUploader(hass, entry, heartbeat)
        self.backup = BackupCollector(hass)
        self.data: dict[str, Any] | None = None
        self._unsubscribe = None
        self._component_listener = None
        self._task: asyncio.Task | None = None
        self._stopped = False
        entry.async_on_unload(self.async_stop)

    @property
    def enabled(self) -> bool:
        return self.entry.options.get(CONF_HEALTH_ENABLED, False)

    @callback
    def async_start(self) -> None:
        if not self.enabled or self._stopped or self._unsubscribe is not None:
            return
        if "backup" in self.entry.options.get(CONF_HEALTH_SECTIONS, HEALTH_SECTIONS):
            self._attach_backup()
            self._component_listener = self.hass.bus.async_listen(
                EVENT_COMPONENT_LOADED, self._component_loaded
            )
        self._unsubscribe = async_track_time_interval(
            self.hass,
            self._tick,
            timedelta(seconds=HEALTH_INTERVAL),
            cancel_on_shutdown=True,
        )
        self._tick(dt_util.utcnow())
        self.uploader.async_start()

    @callback
    def _attach_backup(self) -> None:
        try:
            self.backup.attach()
        except Exception:
            # Collector compatibility must never gate entry setup or another worker.
            self.backup.stop()

    @callback
    def _component_loaded(self, event) -> None:
        if event.data.get("component") == "backup":
            self._attach_backup()

    @callback
    def _tick(self, _now: datetime) -> None:
        if self._stopped or self._task is not None and not self._task.done():
            return
        self._task = self.entry.async_create_background_task(
            self.hass,
            self.async_collect(),
            "HA Monitor health collection",
            eager_start=False,
        )

    async def async_collect(self) -> None:
        instance = self.entry.data[CONF_INSTANCE_ID]
        sections = self.entry.options.get(CONF_HEALTH_SECTIONS, HEALTH_SECTIONS)
        scope = (
            "all"
            if self.entry.options.get(CONF_HEALTH_ALL_ENTITIES, False)
            else "selected"
        )
        report: dict[str, Any] = {"report_id": str(uuid4())}
        collectors = {
            "repairs": lambda: collect_repairs(self.hass, instance),
            "config_entries": lambda: collect_config_entries(self.hass, instance),
            "unavailable_entities": lambda: collect_unavailable_entities(
                self.hass,
                instance,
                all_entities=scope == "all",
                selected=self.entry.options.get(CONF_HEALTH_ENTITIES, []),
                include_devices=self.entry.options.get(CONF_HEALTH_DEVICES, False),
            ),
        }
        for section, collect in collectors.items():
            section_scope = scope if section == "unavailable_entities" else "all"
            if section not in sections:
                report[section] = neutral_collection("unsupported", section_scope)
            elif self.hass.state is not CoreState.running:
                report[section] = neutral_collection("unknown", section_scope)
            else:
                try:
                    report[section] = collect()
                except Exception:
                    report[section] = neutral_collection("unavailable", section_scope)
        observed = dt_util.utcnow()
        try:
            report["backup"] = (
                self.backup.collect(observed)
                if "backup" in sections
                else neutral_backup("unsupported")
            )
        except Exception:
            report["backup"] = neutral_backup("unavailable")
        report["observed_at"] = observed.isoformat()
        self.data = report
        # Export failures do not stop future local observations or other workers.
        try:
            await self.uploader.async_observed(report)
        except Exception:
            self.uploader._block("collection_export_failed")

    async def async_stop(self) -> None:
        self._stopped = True
        for unsubscribe in (self._unsubscribe, self._component_listener):
            if unsubscribe is not None:
                unsubscribe()
        self._unsubscribe = self._component_listener = None
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self.backup.stop()
        await self.uploader.async_stop()
