"""Independent collection, Snapshot v2 delivery and lifecycle through real HA."""

import asyncio
from datetime import timedelta
import json
from unittest.mock import patch

from aiohttp import web
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant.common import async_fire_time_changed

from custom_components.ha_monitor_agent.client import AgentClient
from custom_components.ha_monitor_agent.const import DOMAIN
from custom_components.ha_monitor_agent.monitoring import MonitoringCoordinator
from custom_components.ha_monitor_agent.sender import HeartbeatSender


async def make_monitor(hass, backend, entry, **options):
    """Construct a sender with an explicitly selected export scope for tests."""
    hass.config_entries.async_update_entry(
        entry,
        options={
            "monitoring_enabled": True,
            "export_all_updates": False,
            **options,
        },
    )
    heartbeat = HeartbeatSender(hass, entry, AgentClient(backend.session, backend.url))
    heartbeat.status = "online"
    monitor = MonitoringCoordinator(hass, entry, heartbeat)
    entry.runtime_data = heartbeat
    heartbeat.monitoring = monitor
    await monitor.async_refresh()
    return monitor


async def test_snapshot_complete_allowlist_and_separate_endpoint(hass, backend, entry):
    hass.states.async_set("sensor.private", backend.code, {"secret": backend.token})
    monitor = await make_monitor(
        hass, backend, entry, export_metrics=["memory_percent"]
    )
    await monitor.uploader.async_send_once()
    payload, headers, size = backend.monitoring_requests[0]
    assert headers["Authorization"] == f"Bearer {backend.token}"
    assert size <= 65536
    assert not backend.requests
    assert monitor.uploader.status == "online"
    assert monitor.uploader.pending_snapshots == 0
    snapshot = payload["snapshots"][0]
    assert snapshot["metrics"]["cpu_percent"] == {
        "status": "unsupported",
        "value": None,
        "scope": None,
    }
    assert snapshot["metrics"]["storage_percent"]["status"] == "unsupported"
    assert monitor.data["metrics"]["storage_percent"]["scope"] == "data"
    assert snapshot["metrics"]["memory_percent"]["scope"] == "host"
    assert backend.token not in json.dumps(payload) and backend.code not in json.dumps(
        payload
    )


async def test_lost_ack_freezes_uuid_microseconds_content_and_coalesces(
    hass, backend, entry, freezer
):
    freezer.move_to("2026-11-06T12:00:00.123456+00:00")
    backend.monitoring_commit_then_fail = True
    monitor = await make_monitor(hass, backend, entry)
    uploader = monitor.uploader
    await uploader.async_send_once()
    original = backend.monitoring_requests[0][0]["snapshots"][0]
    assert uploader.status == "retrying"
    # Mutating the local view cannot mutate a frozen retry.
    monitor.data["metrics"]["memory_percent"]["value"] = 99
    for _ in range(4):
        freezer.tick(timedelta(seconds=60))
        await monitor.async_refresh()
    assert uploader.pending_snapshots == 2
    assert uploader.dropped_snapshots == 3
    latest = monitor.data.copy()
    await uploader.async_send_once()
    assert uploader.status == "online"
    assert backend.monitoring_requests[-1][0]["snapshots"] == [original]
    assert len(backend.monitoring_stored) == 1
    assert original["observed_at"] == "2026-11-06T12:00:00.123456+00:00"
    freezer.tick(timedelta(seconds=15))
    await uploader.async_send_once()
    assert uploader.pending_snapshots == 0
    assert backend.monitoring_requests[-1][0]["snapshots"] == [latest]
    assert len(backend.monitoring_stored) == 2


@pytest.mark.parametrize("status", [400, 403, 404, 409, 413, 415, 422, 307])
async def test_monitoring_permanent_failure_does_not_block_heartbeat(
    hass, backend, entry, status
):
    backend.monitoring_responses.append(
        web.Response(status=status, headers={"Location": "https://example.org/steal"})
    )
    monitor = await make_monitor(hass, backend, entry)
    uploader = monitor.uploader
    await uploader.async_send_once()
    assert uploader.status == "blocked"
    assert ir.async_get(hass).async_get_issue(DOMAIN, uploader.issue_id)
    await uploader.heartbeat.async_send_once()
    assert uploader.heartbeat.status == "online"
    assert len(backend.stored) == 1
    await monitor.async_refresh()
    await uploader.async_send_once()
    assert len(backend.monitoring_requests) == 1
    assert monitor.data is not None


async def test_invalid_ack_never_discards_snapshot(hass, backend, entry):
    backend.monitoring_responses.append(
        web.json_response(
            {
                "accepted": 0,
                "duplicates": 0,
                "received_at": dt_util.utcnow().isoformat(),
            }
        )
    )
    monitor = await make_monitor(hass, backend, entry)
    await monitor.uploader.async_send_once()
    assert monitor.uploader.status == "blocked"
    assert monitor.uploader.pending_snapshots == 1


async def test_retry_after_minimum_and_backoff_cap(hass, backend, entry, freezer):
    backend.monitoring_responses.append(
        web.Response(status=429, headers={"Retry-After": "120"})
    )
    monitor = await make_monitor(hass, backend, entry)
    await monitor.uploader.async_send_once()
    assert monitor.uploader._delay >= 120
    freezer.tick(timedelta(seconds=119))
    await monitor.uploader.async_send_once()
    assert len(backend.monitoring_requests) == 1
    freezer.tick(timedelta(seconds=1))
    backend.monitoring_responses.append(web.Response(status=503))
    monitor.uploader.failures = 1000
    await monitor.uploader.async_send_once()
    assert 450 <= monitor.uploader._delay <= 900


async def test_credential_failure_stops_both_uploads_but_keeps_collection(
    hass, backend, entry
):
    monitor = await make_monitor(hass, backend, entry)
    backend.revoked = True
    await monitor.uploader.async_send_once()
    await hass.async_block_till_done()
    assert (
        monitor.uploader.status
        == monitor.uploader.heartbeat.status
        == "reauth_required"
    )
    assert any(
        flow["step_id"] == "reauth_confirm"
        for flow in hass.config_entries.flow.async_progress()
    )
    first_id = monitor.data["snapshot_id"]
    await monitor.async_refresh()
    assert monitor.data["snapshot_id"] != first_id


@pytest.mark.parametrize("clock_change", [timedelta(hours=24), timedelta(minutes=-10)])
async def test_expiry_and_clock_rollback_drop_old_snapshot(
    hass, backend, entry, freezer, clock_change
):
    backend.monitoring_responses.append(web.Response(status=503))
    monitor = await make_monitor(hass, backend, entry)
    await monitor.uploader.async_send_once()
    old_id = monitor.data["snapshot_id"]
    freezer.tick(clock_change)
    await monitor.async_refresh()
    # Backwards clock must not bypass a server-imposed Retry-After deadline.
    if clock_change.total_seconds() < 0:
        freezer.tick(timedelta(minutes=11))
        await monitor.async_refresh()
    await monitor.uploader.async_send_once()
    assert old_id not in backend.monitoring_stored
    assert monitor.uploader.dropped_snapshots >= 1
    assert len(backend.monitoring_stored) == 1


async def test_paused_transmission_collects_periodically_without_local_entities(
    hass, backend, entry, freezer
):
    hass.config_entries.async_update_entry(entry, options={"enabled": False})
    assert await hass.config_entries.async_setup(entry.entry_id)
    monitor = entry.runtime_data.monitoring
    await monitor.async_refresh()
    initial = monitor.data["snapshot_id"]
    assert monitor.uploader._task is None
    assert not backend.requests and not backend.monitoring_requests
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{backend.instance_id}_memory_percent"
    )
    assert (
        registry.async_get(entity_id).disabled_by
        == er.RegistryEntryDisabler.INTEGRATION
    )
    freezer.tick(timedelta(seconds=65))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert monitor.data["snapshot_id"] != initial
    assert not backend.requests and not backend.monitoring_requests
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_optional_local_sensor_uses_shared_data_and_scope(hass, backend, entry):
    registry = er.async_get(hass)
    entity = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{backend.instance_id}_memory_percent",
        config_entry=entry,
        disabled_by=None,
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    monitor = entry.runtime_data.monitoring
    await monitor.async_refresh()
    await hass.async_block_till_done()
    state = hass.states.get(entity.entity_id)
    assert state is not None
    assert state.state == str(monitor.data["metrics"]["memory_percent"]["value"])
    assert state.attributes["scope"] == "host"
    assert state.attributes["collection_status"] == "ok"
    with patch.object(
        monitor._resources, "collect", side_effect=PermissionError(backend.token)
    ):
        await monitor.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(entity.entity_id).state == "unavailable"
    assert monitor.data["metrics"]["memory_percent"]["value"] is None


async def test_monitoring_inflight_does_not_delay_heartbeats_and_unload_cancels(
    hass, backend, entry
):
    hass.config_entries.async_update_entry(entry, options={"monitoring_enabled": True})
    backend.monitoring_gate = asyncio.Event()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await asyncio.wait_for(backend.monitoring_started.wait(), timeout=2)
    heartbeat = entry.runtime_data
    await heartbeat.async_send_once()
    assert heartbeat.status == "online"
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert heartbeat.monitoring.uploader._task is None
    assert not backend.session.closed
    backend.monitoring_gate.set()


async def test_collector_executor_can_wait_while_heartbeat_succeeds(
    hass, backend, entry
):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    heartbeat = entry.runtime_data
    await heartbeat.monitoring.async_refresh()
    gate = asyncio.Event()
    started = asyncio.Event()
    real_executor = hass.async_add_executor_job

    async def blocked_executor(target, *args):
        if target == heartbeat.monitoring._resources.collect:
            started.set()
            await gate.wait()
        return await real_executor(target, *args)

    with patch.object(hass, "async_add_executor_job", side_effect=blocked_executor):
        collecting = asyncio.create_task(heartbeat.monitoring.async_refresh())
        await asyncio.wait_for(started.wait(), timeout=2)
        await heartbeat.async_send_once()
        assert heartbeat.status == "online"
        gate.set()
        await collecting


@pytest.mark.parametrize("late_status", [200, 422, 503])
async def test_monitoring_revocation_wins_over_older_heartbeat_response(
    hass, backend, entry, late_status
):
    monitor = await make_monitor(hass, backend, entry)
    heartbeat = monitor.uploader.heartbeat
    backend.gate = asyncio.Event()
    request = asyncio.create_task(heartbeat.async_send_once())
    try:
        await asyncio.wait_for(backend.request_started.wait(), timeout=2)
        backend.monitoring_responses.append(web.Response(status=401))
        await monitor.uploader.async_send_once()
        assert heartbeat.status == "reauth_required"
        if late_status != 200:
            backend.responses.append(web.Response(status=late_status))
    finally:
        backend.gate.set()
        await request
    assert heartbeat.status == "reauth_required"
    assert heartbeat.next_attempt is None
    calls = len(backend.requests)
    await heartbeat.async_send_once()
    assert len(backend.requests) == calls


async def test_local_collection_starts_before_initial_backend_response(
    hass, backend, entry
):
    backend.gate = asyncio.Event()
    setup = asyncio.create_task(hass.config_entries.async_setup(entry.entry_id))
    try:
        await asyncio.wait_for(backend.request_started.wait(), timeout=2)
        monitor = entry.runtime_data.monitoring
        # Serialized refresh waits for initial local I/O, independently of HTTP.
        await asyncio.wait_for(monitor.async_refresh(), timeout=2)
        assert monitor.data["metrics"]["memory_percent"]["status"] == "ok"
        assert not setup.done()
    finally:
        backend.gate.set()
        assert await setup
