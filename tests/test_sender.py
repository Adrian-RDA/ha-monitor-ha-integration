"""Delivery semantics, bounded retries, lifecycle and diagnostic entities."""

import asyncio
from datetime import timedelta
import json
from unittest.mock import patch

from aiohttp import web
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant.common import async_fire_time_changed

from custom_components.ha_monitor_agent.client import AgentClient
from custom_components.ha_monitor_agent.const import BACKOFF_CAP, DOMAIN, VERSION
from custom_components.ha_monitor_agent.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.ha_monitor_agent.sender import HeartbeatSender


async def make_sender(hass, backend, entry):
    sender = HeartbeatSender(hass, entry, AgentClient(backend.session, backend.url))
    await sender.async_setup()
    return sender


async def test_setup_entities_unload_restart(hass, backend, entry):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    sender = entry.runtime_data
    assert sender.status == "online"
    assert sender.pending_events == 0
    first_id = next(iter(backend.stored))
    registry = er.async_get(hass)
    for key, expected in [("status", "online"), ("pending_events", "0")]:
        entity_id = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{backend.instance_id}_{key}"
        )
        assert hass.states.get(entity_id).state == expected
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert sender._stopped and sender._task is None
    assert not backend.session.closed
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert len(backend.stored) == 2
    assert first_id in backend.stored
    assert entry.data["agent_token"] == backend.token
    assert backend.pair_calls == 0


async def test_regular_background_heartbeats(hass, backend, entry, freezer):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await asyncio.sleep(0)  # Allow the entry-scoped worker to enter its first sleep.
    backend.request_finished.clear()
    freezer.tick(timedelta(seconds=61))
    async_fire_time_changed(hass)
    await backend.request_finished.wait()
    assert len(backend.stored) == 2
    events = list(backend.stored.values())
    assert events[0]["event_id"] != events[1]["event_id"]
    assert backend.requests[-1][0]["integration_version"] == VERSION


async def test_durable_commit_lost_ack_deduplicates(hass, backend, entry, freezer):
    backend.commit_then_fail = True
    sender = await make_sender(hass, backend, entry)
    assert sender.status == "retrying" and sender.pending_events == 1
    original = list(backend.stored.values())[0]
    freezer.tick(timedelta(seconds=5))
    await sender.async_send_once()
    assert sender.status == "online" and sender.pending_events == 0
    assert len(backend.stored) == 1
    assert backend.requests[-1][0]["events"] == [original]


async def test_retry_preserves_observation_microseconds(hass, backend, entry, freezer):
    freezer.move_to("2026-11-05T12:00:00.123456+00:00")
    backend.responses.append(web.Response(status=503))
    sender = await make_sender(hass, backend, entry)
    first = backend.requests[0][0]
    freezer.tick(timedelta(microseconds=1))
    await sender.async_send_once()
    retried = backend.requests[1][0]
    assert sender.status == "online"
    assert first["events"] == retried["events"]
    assert first["events"][0]["occurred_at"] == "2026-11-05T12:00:00.123456+00:00"
    assert retried["sent_at"] == "2026-11-05T12:00:00.123457+00:00"


@pytest.mark.parametrize("status", [429, 500, 503, 599])
async def test_temporary_http_errors_retry_without_acknowledging(
    hass, backend, entry, status
):
    backend.responses.append(web.Response(status=status))
    sender = await make_sender(hass, backend, entry)
    assert sender.status == "retrying" and sender.pending_events == 1
    assert sender.last_heartbeat is None
    assert sender.next_attempt is not None


async def test_offline_recovery_keeps_ids_and_new_observations(
    hass, backend, entry, freezer, caplog
):
    backend.responses.extend(web.json_response({}, status=503) for _ in range(3))
    sender = await make_sender(hass, backend, entry)
    for _ in range(2):
        freezer.tick(timedelta(seconds=60))
        await sender.async_send_once()
    assert sender.failures == 3 and sender.pending_events == 3
    assert sum("receiver unavailable" in message for message in caplog.messages) == 1
    freezer.tick(timedelta(seconds=60))
    await sender.async_send_once()
    assert sender.status == "online" and sender.failures == 0
    assert sender.pending_events == 3 and len(backend.stored) == 1
    assert all(
        payload["events"] == backend.requests[0][0]["events"]
        for payload, _, _ in backend.requests[:4]
    )
    for _ in range(3):
        freezer.tick(timedelta(seconds=3))
        await sender.async_send_once()
    assert sender.pending_events == 0 and len(backend.stored) == 4


async def test_backoff_cap_and_retry_after(hass, backend, entry, freezer):
    backend.responses.append(
        web.json_response({}, status=429, headers={"Retry-After": "120"})
    )
    sender = await make_sender(hass, backend, entry)
    assert sender._delay >= 120
    sender.failures = 1000
    backend.responses.append(web.json_response({}, status=503))
    freezer.tick(timedelta(seconds=120))
    await sender.async_send_once()
    assert BACKOFF_CAP / 2 <= sender._delay <= BACKOFF_CAP


async def test_buffer_bound_and_expiry(hass, backend, entry, freezer):
    backend.responses.extend(web.json_response({}, status=503) for _ in range(105))
    sender = await make_sender(hass, backend, entry)
    for _ in range(104):
        freezer.tick(timedelta(seconds=60))
        await sender.async_send_once()
    assert sender.pending_events == 100
    assert sender.dropped_events == 5
    freezer.tick(timedelta(hours=24))
    await sender.async_send_once()
    assert sender.pending_events == 0
    assert sender.dropped_events == 105
    assert len(backend.stored) == 1


@pytest.mark.parametrize(
    ("max_events", "max_bytes"), [(1, 65536), (2, 65536), (100, 400)]
)
async def test_one_heartbeat_per_request_even_with_larger_advertised_limits(
    hass, backend, entry, freezer, max_events, max_bytes
):
    backend.limits.update(max_batch_events=max_events, max_body_bytes=max_bytes)
    hass.config_entries.async_update_entry(entry, data={**entry.data, **backend.limits})
    backend.responses.extend(web.json_response({}, status=503) for _ in range(3))
    sender = await make_sender(hass, backend, entry)
    for _ in range(2):
        freezer.tick(timedelta(seconds=60))
        await sender.async_send_once()
    for _ in range(3):
        freezer.tick(timedelta(seconds=3))
        await sender.async_send_once()
        if not sender.pending_events:
            break
    assert len(backend.stored) == 3 and sender.pending_events == 0
    assert all(size <= max_bytes for _, _, size in backend.requests)
    assert all(len(payload["events"]) == 1 for payload, _, _ in backend.requests)


@pytest.mark.parametrize("limit_adjustment", [0, -1])
async def test_actual_uncompressed_body_limit(
    hass, backend, entry, freezer, limit_adjustment
):
    backend.responses.append(web.json_response({}, status=503))
    sender = await make_sender(hass, backend, entry)
    size = backend.requests[0][2]
    backend.limits["max_body_bytes"] = size + limit_adjustment
    hass.config_entries.async_update_entry(entry, data={**entry.data, **backend.limits})
    await sender.async_send_once()
    if limit_adjustment == 0:
        assert sender.status == "online" and sender.pending_events == 0
        assert backend.requests[-1][2] == size
        assert len(backend.requests) == 2
    else:
        assert sender.status == "blocked" and sender.pending_events == 1
        assert len(backend.requests) == 1


async def test_worker_waits_for_retry_after(hass, backend, entry, freezer):
    backend.responses.append(web.Response(status=429, headers={"Retry-After": "120"}))
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await asyncio.sleep(0)
    freezer.tick(timedelta(seconds=119))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert len(backend.requests) == 1
    freezer.tick(timedelta(seconds=2))
    async_fire_time_changed(hass)
    await backend.request_finished.wait()
    assert len(backend.requests) == 2
    assert backend.requests[1][0]["events"] == backend.requests[0][0]["events"]


async def test_single_oversize_heartbeat_stops_without_post(hass, backend, entry):
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "max_body_bytes": 100}
    )
    sender = await make_sender(hass, backend, entry)
    assert sender.status == "blocked"
    assert sender.last_error == "payload_too_large"
    assert not backend.requests


async def test_unsupported_version_stops_without_post(hass, backend, entry):
    with patch("custom_components.ha_monitor_agent.sender.HA_VERSION", "unsupported"):
        sender = await make_sender(hass, backend, entry)
    assert sender.status == "blocked"
    assert sender.last_error == "invalid_version"
    assert not backend.requests


@pytest.mark.parametrize(
    "status", [202, 204, 400, 403, 404, 413, 415, 422, 302, 307, 600]
)
async def test_permanent_errors_stop_worker(hass, backend, entry, status):
    backend.responses.append(
        web.json_response(
            {}, status=status, headers={"Location": "https://example.org/steal"}
        )
    )
    sender = await make_sender(hass, backend, entry)
    sender.async_start()
    assert sender.status == "blocked" and sender._task is None
    assert ir.async_get(hass).async_get_issue(DOMAIN, sender.issue_id) is not None
    assert len(backend.requests) == 1


async def test_invalid_ack_is_not_removed(hass, backend, entry):
    backend.responses.append(
        web.json_response(
            {
                "accepted": 0,
                "duplicates": 0,
                "received_at": dt_util.utcnow().isoformat(),
            }
        )
    )
    sender = await make_sender(hass, backend, entry)
    assert sender.status == "blocked" and sender.pending_events == 1


async def test_setup_auth_failure_starts_reauth(hass, backend, entry):
    backend.revoked = True
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert any(flow["step_id"] == "reauth_confirm" for flow in flows)
    assert len(backend.requests) == 1


async def test_runtime_revocation_stops_and_reauths(hass, backend, entry, freezer):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    backend.revoked = True
    freezer.tick(timedelta(seconds=60))
    await entry.runtime_data.async_send_once()
    await hass.async_block_till_done()
    assert entry.runtime_data.status == "reauth_required"
    assert entry.runtime_data.next_attempt is None
    assert any(
        flow["step_id"] == "reauth_confirm"
        for flow in hass.config_entries.flow.async_progress()
    )


async def test_runtime_reauth_recovers_with_new_token(hass, backend, entry, freezer):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    old_sender = entry.runtime_data
    old_token = entry.data["agent_token"]
    backend.new_code()
    freezer.tick(timedelta(seconds=60))
    await old_sender.async_send_once()
    await hass.async_block_till_done()
    flow = next(
        flow
        for flow in hass.config_entries.flow.async_progress()
        if flow["step_id"] == "reauth_confirm"
    )
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {"server_url": backend.url, "pairing_code": backend.code}
    )
    await hass.async_block_till_done()
    assert result["reason"] == "reauth_successful"
    assert old_sender._stopped
    assert entry.runtime_data is not old_sender
    assert entry.runtime_data.status == "online"
    assert entry.data["agent_token"] == backend.token != old_token
    assert backend.requests[-1][1]["Authorization"] == f"Bearer {backend.token}"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_remove_cleans_up_repairs(hass, backend, entry):
    backend.responses.append(web.json_response({}, status=422))
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    issue_id = entry.runtime_data.issue_id
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
    assert await hass.config_entries.async_remove(entry.entry_id)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_unload_cancels_inflight_request(hass, backend, entry):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    sender = entry.runtime_data
    await sender.async_stop()
    # Start a real entry-scoped upload blocked at the simulated receiver.
    sender._stopped = False
    sender._last_observation = None
    backend.gate = asyncio.Event()
    backend.request_started.clear()
    sender._task = entry.async_create_background_task(
        hass, sender.async_send_once(), "blocked test upload"
    )
    await asyncio.wait_for(backend.request_started.wait(), timeout=2)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert sender._task is None and sender._stopped
    backend.gate.set()


async def test_diagnostics_and_logs_never_include_secrets(hass, backend, entry, caplog):
    backend.responses.append(
        web.Response(status=503, text=backend.token + backend.code)
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    output = json.dumps(diagnostics, default=str) + caplog.text
    assert backend.token not in output
    assert backend.code not in output
    assert backend.url not in json.dumps(diagnostics, default=str)
    assert backend.instance_id not in json.dumps(diagnostics, default=str)


async def test_clock_rollback_uses_fresh_observation(hass, backend, entry, freezer):
    backend.responses.append(web.json_response({}, status=503))
    sender = await make_sender(hass, backend, entry)
    first_id = backend.requests[0][0]["events"][0]["event_id"]
    freezer.tick(timedelta(minutes=-10))
    await sender.async_send_once()
    assert sender.status == "online"
    assert sender.dropped_events == 1
    assert first_id not in backend.stored
