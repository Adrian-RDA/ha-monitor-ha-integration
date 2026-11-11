"""Health v1 TLS delivery, privacy, isolation, retry and actual HA lifecycle."""

import asyncio
import json
from unittest.mock import patch

from homeassistant.components.backup.manager import CreateBackupState
from homeassistant.config_entries import ConfigEntryDisabler
from homeassistant.core import CoreState
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant.common import async_fire_time_changed

from custom_components.ha_monitor_agent.client import AgentClient
from custom_components.ha_monitor_agent.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.ha_monitor_agent.health import HealthCoordinator, fit_report
from custom_components.ha_monitor_agent.sender import HeartbeatSender

from .test_health_collectors import emit, repair
from .test_health_collectors import manager as manager


async def make_health(hass, backend, entry, **options):
    hass.config_entries.async_update_entry(
        entry, options={"health_enabled": True, **options}
    )
    sender = HeartbeatSender(hass, entry, AgentClient(backend.session, backend.url))
    sender.status = "online"
    entry.runtime_data = sender
    health = sender.health = HealthCoordinator(hass, entry, sender)
    await health.async_collect()
    return health


async def wait_health(hass, backend, entry):
    await asyncio.wait_for(backend.health_started.wait(), 2)
    async with entry.runtime_data.health.uploader._send_lock:
        pass
    await hass.async_block_till_done()


async def test_full_report_allowlist_privacy_and_independent_endpoint(
    hass, backend, entry, manager
):
    repair(hass)
    hass.states.async_set(
        "sensor.private",
        "unavailable",
        {"friendly_name": backend.token, "password": backend.code},
    )
    health = await make_health(hass, backend, entry, health_all_entities=True)
    emit(manager, CreateBackupState.IN_PROGRESS)
    emit(manager, CreateBackupState.FAILED, reason=backend.token)
    await health.async_collect()
    await health.uploader.async_send_once()
    payload, headers, size = backend.health_requests[0]
    assert size <= 262144 and headers["Authorization"] == f"Bearer {backend.token}"
    assert health.uploader.status == "online"
    assert not backend.requests and not backend.monitoring_requests
    assert payload["report"]["backup"]["status"] == "failed"
    assert payload["report"]["backup"]["error_code"] == "unknown"
    assert payload["report"]["repairs"]["items"][0]["key"] == "test:broken"
    assert payload["report"]["unavailable_entities"]["items"] == [
        {"entity_id": "sensor.private", "name": None, "device": None}
    ]
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    for text in (json.dumps(payload), json.dumps(diagnostics, default=str)):
        assert backend.token not in text and backend.code not in text
    assert "test:broken" not in json.dumps(diagnostics, default=str)


@pytest.mark.parametrize("sections", [[], ["repairs"], ["backup"]])
async def test_opt_in_domains_and_default_empty_entity_selection(
    hass, backend, entry, sections
):
    hass.states.async_set("sensor.private", "unavailable")
    health = await make_health(hass, backend, entry, health_sections=sections)
    for section in ("repairs", "config_entries", "unavailable_entities", "backup"):
        assert health.data[section]["coverage"] == (
            "complete" if section in sections and section != "backup" else "unsupported"
        )
    assert health.data["unavailable_entities"]["items"] == []
    await health.uploader.async_send_once()


async def test_default_no_health_collection_or_requests(hass, backend, entry, freezer):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    health = entry.runtime_data.health
    assert (
        health.data is None
        and health._unsubscribe is None
        and health.backup._unsubscribe is None
    )
    await entry.runtime_data.async_stop()
    freezer.tick(301)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert not backend.health_requests


@pytest.mark.parametrize("disable_polling", [False, True])
async def test_five_minute_clock_reload_and_unload_without_sensors(
    hass, backend, entry, freezer, disable_polling
):
    hass.config_entries.async_update_entry(
        entry, options={"health_enabled": True}, pref_disable_polling=disable_polling
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await wait_health(hass, backend, entry)
    health = entry.runtime_data.health
    health.async_start()  # Idempotent clock startup.
    first = health.data["report_id"]
    backend.health_started.clear()
    freezer.tick(299)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert len(backend.health_requests) == 1
    freezer.tick(2)
    async_fire_time_changed(hass)
    await wait_health(hass, backend, entry)
    assert health.data["report_id"] != first
    assert len(backend.health_requests) == 2
    await hass.config_entries.async_set_disabled_by(
        entry.entry_id, ConfigEntryDisabler.USER
    )
    assert health._task is health._unsubscribe is health._component_listener is None
    backend.health_started.clear()
    freezer.tick(301)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert len(backend.health_requests) == 2
    await hass.config_entries.async_set_disabled_by(entry.entry_id, None)
    await wait_health(hass, backend, entry)
    assert entry.runtime_data.health is not health
    assert len(backend.health_requests) == 3


async def test_collection_failure_does_not_reissue_stale_findings_or_affect_peers(
    hass, backend, entry
):
    repair(hass)
    health = await make_health(hass, backend, entry)
    first = health.data
    with patch(
        "custom_components.ha_monitor_agent.health.collect_repairs",
        side_effect=RuntimeError(backend.token),
    ):
        await health.async_collect()
    assert health.data["repairs"] == {
        "coverage": "unavailable",
        "scope": "all",
        "total": None,
        "items": [],
    }
    assert health.data["config_entries"]["coverage"] == "complete"
    assert first["report_id"] != health.data["report_id"]
    previous = health.data
    queued = health.uploader._latest
    hass.set_state(CoreState.starting)
    await health.async_collect()
    assert health.data is previous
    assert health.uploader._latest is queued
    hass.set_state(CoreState.running)
    await health.async_collect()
    assert health.data["repairs"]["coverage"] == "complete"
    assert health.data["repairs"]["total"] == 1


async def test_names_require_consent_and_device_ids_stay_stable_on_reload(
    hass, backend, entry
):
    hass.config_entries.async_update_entry(entry, title="Freigegebene Integration")
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("test", backend.code)},
        name="Originalgerät",
    )
    dr.async_get(hass).async_update_device(device.id, name_by_user="Wohnzimmer")
    registry = er.async_get(hass)
    entity = registry.async_get_or_create(
        "sensor",
        "test",
        "approved",
        config_entry=entry,
        device_id=device.id,
        original_name="Originalname",
    )
    registry.async_update_entity(entity.entity_id, name="Temperatur")
    hass.states.async_set(
        entity.entity_id, "unavailable", {"friendly_name": backend.token}
    )
    hass.states.async_set(
        "sensor.unregistered", "unavailable", {"friendly_name": backend.code}
    )
    health = await make_health(
        hass, backend, entry, health_all_entities=True, health_devices=True
    )
    default = health.data["unavailable_entities"]["items"]
    assert all(item["name"] is None for item in default)
    assert all(
        item["device"] is None or item["device"]["name"] is None for item in default
    )
    assert health.data["config_entries"]["items"][0]["name"] is None
    await health.async_stop()
    health = await make_health(
        hass,
        backend,
        entry,
        health_all_entities=True,
        health_devices=True,
        health_names=True,
    )
    named = next(
        item for item in health.data["unavailable_entities"]["items"] if item["device"]
    )
    assert named["name"] == "Temperatur"
    assert named["device"] == {"id": device.id, "name": "Wohnzimmer"}
    assert (
        named["device"]["id"]
        == next(item for item in default if item["device"])["device"]["id"]
    )
    assert (
        health.data["config_entries"]["items"][0]["name"] == "Freigegebene Integration"
    )
    unregistered = next(
        item
        for item in health.data["unavailable_entities"]["items"]
        if item["entity_id"] == "sensor.unregistered"
    )
    assert unregistered["name"] is unregistered["device"] is None
    await health.uploader.async_send_once()
    payload = json.dumps(backend.health_requests[-1][0])
    assert backend.token not in payload and backend.code not in payload
    # Name approval alone cannot opt in to device association.
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "health_devices": False}
    )
    await health.async_collect()
    assert all(
        item["device"] is None for item in health.data["unavailable_entities"]["items"]
    )


async def test_retry_frozen_report_id_time_coalescing_and_quota(
    hass, backend, entry, freezer
):
    backend.health_commit_then_fail = True
    health = await make_health(hass, backend, entry)
    await health.uploader.async_send_once()
    original = backend.health_requests[0][0]["report"]
    assert health.uploader.status == "retrying"
    # Retry spacing remains independent of new observations and direct sends.
    for _ in range(10):
        await health.async_collect()
        await health.uploader.async_send_once()
    assert health.uploader.pending_snapshots == 2 and len(backend.health_requests) == 1
    assert health.uploader.dropped_snapshots == 9
    freezer.tick(16)
    await health.uploader.async_send_once()
    assert backend.health_requests[1][0]["report"] == original
    assert len(backend.health_stored) == 1 and health.uploader.status == "online"
    await health.uploader.async_send_once()
    assert len(backend.health_requests) == 2
    freezer.tick(16)
    await health.uploader.async_send_once()
    assert len(backend.health_stored) == 2


async def test_rate_limit_retry_after_and_bounded_backoff(
    hass, backend, entry, freezer
):
    health = await make_health(hass, backend, entry)
    backend.health_responses.append((429, {"Retry-After": "120"}))
    await health.uploader.async_send_once()
    freezer.tick(119)
    await health.uploader.async_send_once()
    assert len(backend.health_requests) == 1
    freezer.tick(2)
    await health.uploader.async_send_once()
    assert len(backend.health_requests) == 2


@pytest.mark.parametrize("status", [400, 403, 404, 409, 413, 415, 422, 302])
async def test_permanent_health_errors_stop_only_health(hass, backend, entry, status):
    health = await make_health(hass, backend, entry)
    backend.health_responses.append((status, {}))
    await health.uploader.async_send_once()
    assert health.uploader.status == "blocked"
    assert ir.async_get(hass).async_get_issue(
        "ha_monitor_agent", health.uploader.issue_id
    )
    await health.uploader.async_send_once()
    await entry.runtime_data.async_send_once()
    assert len(backend.health_requests) == 1 and backend.requests


async def test_health_auth_rejection_stops_all_uploads(hass, backend, entry):
    health = await make_health(hass, backend, entry)
    backend.revoked = True
    await health.uploader.async_send_once()
    assert entry.runtime_data.status == health.uploader.status == "reauth_required"
    await entry.runtime_data.async_send_once()
    await health.uploader.async_send_once()
    assert len(backend.health_requests) == 1 and not backend.requests


async def test_health_network_wait_cannot_block_heartbeat_or_snapshot_and_unload_cancels(
    hass, backend, entry, freezer
):
    backend.health_gate = asyncio.Event()
    hass.config_entries.async_update_entry(
        entry, options={"health_enabled": True, "monitoring_enabled": True}
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await asyncio.wait_for(backend.health_started.wait(), 2)
    await asyncio.wait_for(backend.monitoring_started.wait(), 2)
    health = entry.runtime_data.health
    assert backend.requests and backend.monitoring_requests
    freezer.tick(61)
    await entry.runtime_data.async_send_once()
    assert len(backend.requests) >= 2
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert health.uploader._task is None and health._task is None
    backend.health_gate.set()


async def test_slow_health_export_skips_ticks_without_blocking_heartbeat(
    hass, backend, entry, freezer
):
    started, gate = asyncio.Event(), asyncio.Event()
    original = hass.async_add_executor_job

    async def executor(target, *args):
        if target is fit_report:
            started.set()
            await gate.wait()
        return await original(target, *args)

    hass.config_entries.async_update_entry(entry, options={"health_enabled": True})
    with patch.object(hass, "async_add_executor_job", side_effect=executor):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await asyncio.wait_for(started.wait(), 2)
        health = entry.runtime_data.health
        task = health._task
        freezer.tick(301)
        health._tick(dt_util.utcnow())
        assert health._task is task
        await entry.runtime_data.async_send_once()
        assert len(backend.requests) >= 2
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert task.cancelled()


@pytest.mark.parametrize("seconds", [23 * 3600 + 1, -1])
async def test_expired_or_future_reports_are_discarded(
    hass, backend, entry, freezer, seconds
):
    health = await make_health(hass, backend, entry)
    freezer.tick(seconds)
    await health.uploader.async_send_once()
    assert not backend.health_requests
    assert health.uploader.dropped_snapshots == 1


async def test_actual_payload_byte_budget_marks_partial_preserving_known_total(
    hass, backend, entry
):
    for i in range(1000):
        hass.states.async_set(f"sensor.{'x' * 235}{i:04}", "unavailable")
    health = await make_health(hass, backend, entry, health_all_entities=True)
    assert health.data["unavailable_entities"]["coverage"] == "complete"
    assert health.data["unavailable_entities"]["total"] == 1000
    await health.uploader.async_send_once()
    payload, _, size = backend.health_requests[0]
    section = payload["report"]["unavailable_entities"]
    assert (
        size <= 262144 and section["coverage"] == "partial" and section["total"] == 1000
    )
    assert 0 < len(section["items"]) < 1000
    assert (
        health.data["unavailable_entities"]["total"]
        == len(health.data["unavailable_entities"]["items"])
        == 1000
    )


async def test_backup_late_load_attach_and_unload_removes_subscription(
    hass, backend, entry, manager
):
    from homeassistant.components.backup.const import DATA_MANAGER
    from homeassistant.const import EVENT_COMPONENT_LOADED
    from homeassistant.core import Event

    hass.data.pop(DATA_MANAGER)
    health = await make_health(hass, backend, entry)
    health.async_start()
    assert health.backup._unsubscribe is None
    hass.data[DATA_MANAGER] = manager
    health._component_loaded(Event(EVENT_COMPONENT_LOADED, {"component": "backup"}))
    await wait_health(hass, backend, entry)
    emit(manager, CreateBackupState.IN_PROGRESS)
    emit(manager, CreateBackupState.COMPLETED)
    await health.async_collect()
    assert health.data["backup"]["status"] == "ok"
    await health.async_stop()
    assert health.backup._unsubscribe is None
    health._component_loaded(Event(EVENT_COMPONENT_LOADED, {"component": "backup"}))
    assert health.backup._unsubscribe is None
    emit(manager, CreateBackupState.IN_PROGRESS)
    assert health.backup._data["status"] == "unknown"


async def test_older_health_ack_cannot_undo_credential_rejection(hass, backend, entry):
    health = await make_health(hass, backend, entry)
    backend.health_gate = asyncio.Event()
    sending = asyncio.create_task(health.uploader.async_send_once())
    await asyncio.wait_for(backend.health_started.wait(), 2)
    entry.runtime_data.async_auth_failed()
    backend.health_gate.set()
    await sending
    assert health.uploader.status == entry.runtime_data.status == "reauth_required"
    assert health.uploader.last_upload is None


async def test_collection_export_error_isolated_without_secret_details(
    hass, backend, entry
):
    health = await make_health(hass, backend, entry)
    with patch.object(
        health.uploader, "async_observed", side_effect=RuntimeError(backend.token)
    ):
        await health.async_collect()
    assert health.uploader.status == "blocked"
    assert health.uploader.last_error == "collection_export_failed"
    await entry.runtime_data.async_send_once()
    assert backend.requests
