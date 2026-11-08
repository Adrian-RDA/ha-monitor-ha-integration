"""Snapshot v2 acceptance through real HA APIs and a strict verified TLS receiver."""

import asyncio
from contextlib import nullcontext
from datetime import timedelta
import json
from unittest.mock import patch

from aiohttp import web
from homeassistant.core import CoreState
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
import pytest

from custom_components.ha_monitor_agent import updates
from custom_components.ha_monitor_agent.const import DOMAIN
from custom_components.ha_monitor_agent.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.ha_monitor_agent.updates import collect_update_inventory

from .conftest import validate_updates_v2
from .test_monitoring import make_monitor
from .test_reported_updates import IMMICH, PROXMOX, REPORTED, SHELLY
from .test_reported_updates import reported_updates as reported_updates
from .test_updates import disabled_source, source
from .test_updates import supervisor_updates as supervisor_updates


async def make_v2(hass, backend, entry, **options):
    return await make_monitor(
        hass,
        backend,
        entry,
        export_all_updates=options.pop("export_all_updates", True),
        **options,
    )


def sent_updates(backend):
    return backend.monitoring_requests[-1][0]["snapshots"][0]["updates"]


@pytest.mark.parametrize("reason", list(er.RegistryEntryDisabler))
@pytest.mark.parametrize("selected", [False, True])
async def test_all_native_disabled_reasons_are_safe_without_hiding_healthy_sources(
    hass, backend, entry, reason, selected
):
    disabled = disabled_source(hass, reason)
    good = source(hass, "update.healthy", latest_version="release/2026:11")
    monitor = await make_v2(
        hass,
        backend,
        entry,
        update_entities=[disabled.entity_id] if selected else [],
    )
    await monitor.uploader.async_send_once()
    sent = sent_updates(backend)
    assert sent["coverage"] == ("partial" if selected else "complete")
    assert sent["counts"]["disabled"] == 1 and sent["counts"]["available"] == 1
    records = {item["key"]: item for item in sent["items"]}
    assert records[good.entity_id]["available_version"] == "release/2026:11"
    assert records[disabled.entity_id]["availability"] == "disabled"
    assert records[disabled.entity_id]["disabled_by"] == (
        str(reason)
        if reason
        in {er.RegistryEntryDisabler.USER, er.RegistryEntryDisabler.INTEGRATION}
        else None
    )
    assert (
        records[disabled.entity_id]["installed_version"]
        is records[disabled.entity_id]["available_version"]
        is None
    )
    assert records[disabled.entity_id]["required"] is selected


async def test_default_upgrade_sends_all_four_raw_versions_and_metadata(
    hass, backend, entry, reported_updates, caplog
):
    """An existing entry needs neither pairing nor a new export-all option."""
    original_data = dict(entry.data)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("shelly", "private-device-identifier")},
        name="Carport <Licht>",
        manufacturer="Shelly",
        model="Plus 1PM",
        serial_number="private-serial",
        configuration_url="http://192.0.2.1",
    )
    er.async_get(hass).async_update_entity(SHELLY, device_id=device.id)
    raw_installed = "20241011-123000/v1.14.1-g1234567"
    raw_latest = " 20261101-160000/v1.15.0-gabcdef0 / β: RC "
    hass.states.async_set(
        SHELLY,
        "on",
        {
            "installed_version": raw_installed,
            "latest_version": raw_latest,
            "friendly_name": "Carport-Licht Firmware",
            "device_class": "firmware",
            "supported_features": 5,
            "in_progress": 42.5,
            "release_url": "https://example.org/private-release",
            "secret": backend.token,
        },
    )
    hass.config_entries.async_update_entry(
        entry, options={"monitoring_enabled": True, "discover_updates": False}
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await asyncio.wait_for(backend.monitoring_started.wait(), 2)
    await hass.async_block_till_done()
    monitor = entry.runtime_data.monitoring
    async with monitor.uploader._send_lock:
        pass
    assert monitor.uploader.status == "online"
    payload, _, _ = backend.monitoring_requests[0]
    assert payload["schema_version"] == 2
    sent = payload["snapshots"][0]["updates"]
    assert sent["coverage"] == "complete" and sent["scope"] == "all"
    assert sent["counts"] == {
        "total": 5,
        "active": 5,
        "disabled": 0,
        "unknown": 0,
        "unavailable": 0,
        "available": 4,
        "skipped": 0,
    }
    offers = {
        item["key"]: item
        for item in sent["items"]
        if item["availability"] == "available"
    }
    assert set(offers) == REPORTED
    assert (
        sent["items"][0]["category"] == "core" and sent["items"][1]["category"] == "os"
    )
    shelly = offers[SHELLY]
    assert (
        shelly["installed_version"] == raw_installed
        and shelly["available_version"] == raw_latest
    )
    assert (
        shelly["name"] == "Carport-Licht Firmware" and shelly["category"] == "firmware"
    )
    assert shelly["device"] == {
        "id": device.id,
        "name": "Carport <Licht>",
        "manufacturer": "Shelly",
        "model": "Plus 1PM",
    }
    assert shelly["in_progress"] == 42.5 and shelly["supports_install"] is True
    assert (
        offers[IMMICH]["source"] == "immich"
        and offers[IMMICH]["supports_install"] is False
    )
    assert offers[IMMICH]["available_version"] == "v2.3.1"
    assert (
        offers[PROXMOX]["source"] == "proxmoxve"
        and offers[PROXMOX]["supports_install"] is False
    )
    assert offers[PROXMOX]["available_version"] == "8.4.1-p0-d17"
    assert backend.requests[0][0]["schema_version"] == 1
    assert dict(entry.data) == original_data
    assert monitor.update_summary()["blocked_known_available"] == 0
    wire = json.dumps(payload)
    for private in (
        "private-device-identifier",
        "private-serial",
        "192.0.2.1",
        "private-release",
        backend.token,
        backend.code,
    ):
        assert private not in wire
    diagnostics = json.dumps(
        await async_get_config_entry_diagnostics(hass, entry), default=str
    )
    for private in (
        raw_latest,
        raw_installed,
        "Carport",
        device.id,
        backend.token,
        backend.code,
    ):
        assert private not in diagnostics + caplog.text
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert not backend.session.closed


@pytest.mark.parametrize(
    "failure", ["exception", "unknown", "unavailable", "missing", "disabled"]
)
async def test_failed_selected_source_preserves_all_healthy_offers(
    hass, backend, entry, reported_updates, failure
):
    bad = source(
        hass,
        "update.aaa_bad",
        state=failure if failure in {"unknown", "unavailable"} else "on",
    )
    if failure == "missing":
        hass.states.async_remove(bad.entity_id)
    if failure == "disabled":
        er.async_get(hass).async_update_entity(
            bad.entity_id, disabled_by=er.RegistryEntryDisabler.USER
        )
    real_observe = updates._observe

    def observe(hass, entity_id, selected):
        if failure == "exception" and entity_id == bad.entity_id:
            raise RuntimeError(backend.token + backend.code)
        return real_observe(hass, entity_id, selected)

    with patch.object(updates, "_observe", side_effect=observe):
        monitor = await make_v2(
            hass,
            backend,
            entry,
            export_all_updates=False,
            update_entities=[SHELLY, IMMICH, PROXMOX, bad.entity_id],
        )
    await monitor.uploader.async_send_once()
    sent = sent_updates(backend)
    assert sent["coverage"] == "partial" and sent["scope"] == "selected"
    assert {
        item["key"] for item in sent["items"] if item["availability"] == "available"
    } == REPORTED
    assert sent["counts"]["available"] == 4 and sent["counts"]["total"] == 6
    assert all(item["required"] for item in sent["items"])
    bad_item = next(item for item in sent["items"] if item["key"] == bad.entity_id)
    assert bad_item["availability"] != "available"
    if failure == "disabled":
        assert bad_item["installed_version"] is bad_item["available_version"] is None
        assert bad_item["disabled_by"] == "user"
    assert monitor.update_summary()["blocked_known_available"] == 0
    await monitor.uploader.heartbeat.async_send_once()
    assert monitor.uploader.heartbeat.status == "online"


async def test_auto_disabled_hidden_sources_and_all_seven_categories(
    hass, backend, entry, supervisor_updates
):
    registry = er.async_get(hass)
    supervisor = registry.async_get_or_create(
        "update", "hassio", "home_assistant_supervisor_version_latest"
    )
    hass.states.async_set(
        supervisor.entity_id, "on", {"installed_version": "1", "latest_version": "2"}
    )
    source(hass, "update.app", platform="hassio")
    source(hass, "update.hacs", platform="hacs")
    firmware = source(hass, "update.firmware", device_class="firmware")
    registry.async_update_entity(
        firmware.entity_id, hidden_by=er.RegistryEntryHider.USER
    )
    source(hass, "update.manual", platform="immich", supported_features=0)
    disabled = source(hass, "update.disabled")
    registry.async_update_entity(
        disabled.entity_id, disabled_by=er.RegistryEntryDisabler.INTEGRATION
    )
    monitor = await make_v2(hass, backend, entry)
    await monitor.uploader.async_send_once()
    sent = sent_updates(backend)
    assert sent["coverage"] == "complete"
    assert {item["category"] for item in sent["items"]} == {
        "core",
        "os",
        "supervisor",
        "addon",
        "hacs",
        "firmware",
        "other",
    }
    record = next(item for item in sent["items"] if item["key"] == disabled.entity_id)
    assert record["required"] is False and record["disabled_by"] == "integration"
    assert record["availability"] == "disabled" and record["available_version"] is None
    assert sent["counts"]["available"] == 7 and sent["counts"]["disabled"] == 1


async def test_native_skip_unskip_install_transitions_keep_original_metadata(
    hass, backend, entry, freezer
):
    from homeassistant.setup import async_setup_component

    from .test_updates import ReadOnlyUpdate

    assert await async_setup_component(hass, "update", {})
    entity = ReadOnlyUpdate()
    await hass.data["update"].async_add_entities([entity])
    await entity.async_skip()
    monitor = await make_v2(hass, backend, entry)
    await monitor.uploader.async_send_once()
    assert sent_updates(backend)["counts"]["skipped"] == 1
    assert sent_updates(backend)["items"][0]["skipped_version"] == "2.0"
    assert sent_updates(backend)["items"][0]["supports_install"] is False
    await entity.async_clear_skipped()
    freezer.tick(timedelta(seconds=60))
    await monitor.async_refresh()
    await monitor.uploader.async_send_once()
    assert sent_updates(backend)["counts"]["available"] == 1
    entity._attr_installed_version = "2.0"
    entity.async_write_ha_state()
    freezer.tick(timedelta(seconds=60))
    await monitor.async_refresh()
    await monitor.uploader.async_send_once()
    assert sent_updates(backend)["items"][0]["availability"] == "up_to_date"
    assert (
        sent_updates(backend)["counts"]["available"]
        == sent_updates(backend)["counts"]["skipped"]
        == 0
    )
    assert len(backend.monitoring_stored) == 3


@pytest.mark.parametrize(
    "progress",
    [False, True, 0, 100, 27.5, None, "50", -1, 101, float("nan"), float("inf")],
)
async def test_progress_and_available_without_version_are_representable(
    hass, backend, entry, progress
):
    source(
        hass,
        "update.manual",
        latest_version=None,
        installed_version=None,
        supported_features=None,
        in_progress=progress,
    )
    monitor = await make_v2(hass, backend, entry)
    await monitor.uploader.async_send_once()
    sent = sent_updates(backend)
    assert sent["coverage"] == "complete" and sent["counts"]["available"] == 1
    item = sent["items"][0]
    assert item["available_version"] is None and item["supports_install"] is None
    valid_progress = (
        type(progress) is bool
        or type(progress) in {int, float}
        and 0 <= progress <= 100
    )
    assert item["in_progress"] == (progress if valid_progress else None)
    if valid_progress:
        assert type(item["in_progress"]) is type(progress)


@pytest.mark.parametrize(
    "bad", ["x\n2", "x\u200b2", "x\u20282", "x\u20292", "x" * 257, True, 12, ""]
)
async def test_invalid_optional_text_cannot_block_other_observations(
    hass, backend, entry, bad
):
    source(
        hass,
        "update.bad_metadata",
        installed_version=bad,
        latest_version=" valid / β: 2 ",
    )
    source(hass, "update.good", latest_version="🛠 release/2026.11")
    monitor = await make_v2(hass, backend, entry)
    await monitor.uploader.async_send_once()
    sent = sent_updates(backend)
    assert sent["coverage"] == "partial" and sent["counts"]["available"] == 2
    assert sent["items"][0]["installed_version"] is None
    assert sent["items"][0]["available_version"] == " valid / β: 2 "
    assert sent["items"][1]["available_version"] == "🛠 release/2026.11"


@pytest.mark.parametrize("number", [101, 4096])
async def test_large_inventory_is_one_atomic_request_and_stable_retry(
    hass, backend, entry, freezer, number
):
    for index in range(number):
        source(
            hass,
            f"update.package_{index:04d}",
            latest_version="release/2: stable β",
            friendly_name="界" * 256 if number == 101 else "Package",
            supported_features=0,
        )
    backend.monitoring_commit_then_fail = True
    monitor = await make_v2(hass, backend, entry)
    await monitor.uploader.async_send_once()
    original, _, size = backend.monitoring_requests[-1]
    assert 65536 < size <= 2_097_152
    assert len(original["snapshots"]) == 1
    assert original["snapshots"][0]["updates"]["counts"]["available"] == number
    assert len(backend.monitoring_stored) == 1
    # Mutation and a newer observation cannot alter the in-flight collection.
    monitor.data["updates"]["items"][-1]["available_version"] = "mutated"
    freezer.tick(timedelta(seconds=60))
    await monitor.async_refresh()
    assert monitor.uploader.pending_snapshots == 2
    await monitor.uploader.async_send_once()
    assert monitor.uploader.status == "online"
    assert backend.monitoring_requests[-1][0]["snapshots"] == original["snapshots"]
    assert len(backend.monitoring_stored) == 1
    freezer.tick(timedelta(seconds=15))
    await monitor.uploader.async_send_once()
    assert (
        len(backend.monitoring_stored) == 2 and monitor.uploader.pending_snapshots == 0
    )


@pytest.mark.parametrize("percentage", [0, 42.5, 100])
async def test_native_ha_update_percentage_preserved(hass, backend, entry, percentage):
    from homeassistant.components.update import UpdateEntityFeature
    from homeassistant.setup import async_setup_component

    from .test_updates import ReadOnlyUpdate

    assert await async_setup_component(hass, "update", {})
    entity = ReadOnlyUpdate()
    entity._attr_supported_features = UpdateEntityFeature.PROGRESS
    entity._attr_in_progress = True
    entity._attr_update_percentage = percentage
    await hass.data["update"].async_add_entities([entity])
    monitor = await make_v2(hass, backend, entry)
    await monitor.uploader.async_send_once()
    progress = sent_updates(backend)["items"][0]["in_progress"]
    assert progress == percentage and type(progress) is type(percentage)


async def test_source_cap_retains_known_partial_records(hass, backend, entry):
    for index in range(4):
        source(hass, f"update.source_{index}")
    with patch.object(updates, "MAX_UPDATE_SOURCES", 2):
        monitor = await make_v2(hass, backend, entry)
    await monitor.uploader.async_send_once()
    sent = sent_updates(backend)
    assert sent["coverage"] == "partial" and sent["counts"]["available"] == 2
    assert monitor.update_summary()["protocol_issues"] == {"source_limit": 1}


async def test_oversize_preserves_previous_projection_and_heartbeats(
    hass, backend, entry, freezer
):
    source(hass, "update.first")
    monitor = await make_v2(hass, backend, entry)
    await monitor.uploader.async_send_once()
    previous = dict(backend.monitoring_stored)
    for index in range(600):
        source(
            hass,
            f"update.large_{index}",
            installed_version="界" * 256,
            latest_version="語" * 256,
            friendly_name="名" * 256,
        )
    freezer.tick(timedelta(seconds=60))
    await monitor.async_refresh()
    assert (
        monitor.uploader.status == "blocked"
        and monitor.uploader.last_error == "payload_too_large"
    )
    assert ir.async_get(hass).async_get_issue(DOMAIN, monitor.uploader.issue_id)
    await monitor.uploader.async_send_once()
    assert (
        backend.monitoring_stored == previous and len(backend.monitoring_requests) == 1
    )
    assert len(monitor.data["updates"]["items"]) == 601
    await monitor.uploader.heartbeat.async_send_once()
    assert monitor.uploader.heartbeat.status == "online"


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("empty", "complete"),
        ("startup", "unknown"),
        ("failure", "unknown"),
        ("unselected", "unsupported"),
    ],
)
async def test_empty_unknown_unsupported_coverage_never_fabricates_updates(
    hass, backend, entry, mode, expected
):
    if mode == "startup":
        hass.set_state(CoreState.starting)
    context = (
        patch(
            "custom_components.ha_monitor_agent.monitoring.collect_update_inventory",
            side_effect=RuntimeError("private"),
        )
        if mode == "failure"
        else nullcontext()
    )
    with context:
        monitor = await make_v2(
            hass,
            backend,
            entry,
            system_updates=False,
            export_all_updates=mode != "unselected",
        )
    await monitor.uploader.async_send_once()
    sent = sent_updates(backend)
    assert sent["coverage"] == expected
    assert not sent["items"] and not any(sent["counts"].values())


async def test_discovery_failure_keeps_explicit_peers_partial(hass, backend, entry):
    good = source(hass, "update.good")
    with patch.object(
        type(hass.states), "async_all", side_effect=RuntimeError("private")
    ):
        inventory = collect_update_inventory(
            hass, False, [good.entity_id], discover=True
        )
    sent = inventory.as_protocol_v2("all")
    validate_updates_v2(sent)
    assert sent["coverage"] == "partial" and sent["counts"]["available"] == 1


@pytest.mark.parametrize("status", [400, 403, 404, 409, 413, 415, 422, 307])
async def test_v2_permanent_errors_never_fall_back_to_v1(hass, backend, entry, status):
    backend.monitoring_responses.append(
        web.Response(status=status, headers={"Location": "https://example.org/private"})
    )
    monitor = await make_v2(hass, backend, entry)
    await monitor.uploader.async_send_once()
    await monitor.uploader.async_send_once()
    assert monitor.uploader.status == "blocked"
    assert (
        len(backend.monitoring_requests) == 1
        and backend.monitoring_requests[0][0]["schema_version"] == 2
    )
    await monitor.uploader.heartbeat.async_send_once()
    assert monitor.uploader.heartbeat.status == "online"


async def test_v2_rate_limit_coalesces_without_affecting_heartbeat(
    hass, backend, entry, freezer
):
    backend.monitoring_responses.append(
        web.Response(status=429, headers={"Retry-After": "120"})
    )
    monitor = await make_v2(hass, backend, entry)
    await monitor.uploader.async_send_once()
    original = backend.monitoring_requests[0][0]["snapshots"]
    for _ in range(3):
        freezer.tick(timedelta(seconds=30))
        await monitor.async_refresh()
        await monitor.uploader.async_send_once()
    assert (
        len(backend.monitoring_requests) == 1
        and monitor.uploader.pending_snapshots == 2
    )
    await monitor.uploader.heartbeat.async_send_once()
    assert monitor.uploader.heartbeat.status == "online"
    freezer.tick(timedelta(seconds=30))
    await monitor.uploader.async_send_once()
    assert backend.monitoring_requests[-1][0]["snapshots"] == original


@pytest.mark.parametrize("status", [200, 422, 503])
async def test_heartbeat_revocation_wins_over_inflight_v2_response(
    hass, backend, entry, status
):
    monitor = await make_v2(hass, backend, entry)
    backend.monitoring_gate = asyncio.Event()
    request = asyncio.create_task(monitor.uploader.async_send_once())
    try:
        await asyncio.wait_for(backend.monitoring_started.wait(), 2)
        await monitor.uploader.async_send_once()
        assert len(backend.monitoring_requests) == 1
        backend.responses.append(web.Response(status=401))
        await monitor.uploader.heartbeat.async_send_once()
        if status != 200:
            backend.monitoring_responses.append(web.Response(status=status))
    finally:
        backend.monitoring_gate.set()
        await request
    assert monitor.uploader.status == "reauth_required"
    await monitor.async_refresh()
    await monitor.uploader.async_send_once()
    assert len(backend.monitoring_requests) == 1
