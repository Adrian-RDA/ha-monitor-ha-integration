"""Health must wait for full HA startup without delaying the other workers."""

import asyncio
from unittest.mock import patch

from homeassistant.components.backup.manager import CreateBackupState
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_COMPONENT_LOADED, EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, Event
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant.common import MockConfigEntry, async_fire_time_changed

from .test_health import wait_health
from .test_health_collectors import emit, repair
from .test_health_collectors import manager as manager


@pytest.mark.parametrize("initial_state", [CoreState.not_running, CoreState.starting])
async def test_health_waits_for_real_ha_start_while_other_workers_run(
    hass, backend, entry, freezer, initial_state
):
    hass.set_state(initial_state)
    hass.config_entries.async_update_entry(
        entry, options={"health_enabled": True, "monitoring_enabled": True}
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    sender = entry.runtime_data
    health = sender.health
    await asyncio.wait_for(backend.monitoring_started.wait(), 2)
    await hass.async_block_till_done()
    assert backend.requests and backend.monitoring_requests
    health.async_start()  # Repeated calls must not add another startup listener.
    freezer.tick(601)
    async_fire_time_changed(hass)
    health._tick(dt_util.utcnow())
    await health.async_collect()
    await hass.async_block_till_done()
    assert health.data is health._unsubscribe is health.uploader._task is None
    assert health.uploader.pending_snapshots == 0 and not backend.health_requests

    repair(hass)
    source = MockConfigEntry(domain="cast", title="Not loaded")
    source.add_to_hass(hass)
    if initial_state is CoreState.starting:
        hass.set_state(CoreState.not_running)
    await hass.async_start()
    await wait_health(hass, backend, entry)
    report = backend.health_requests[0][0]["report"]
    assert report["repairs"]["coverage"] == "complete"
    assert report["repairs"]["total"] == 1
    finding = next(
        item
        for item in report["config_entries"]["items"]
        if item["key"] == source.entry_id
    )
    assert finding["state"] == "not_loaded"
    assert set(finding) == {"key", "domain", "name", "state"}
    assert health._unsubscribe is not None and len(backend.health_requests) == 1


async def test_reload_before_started_cancels_old_listener_and_unload_cancels_new_one(
    hass, backend, entry
):
    hass.set_state(CoreState.starting)
    hass.config_entries.async_update_entry(entry, options={"health_enabled": True})
    assert await hass.config_entries.async_setup(entry.entry_id)
    old = entry.runtime_data.health
    assert await hass.config_entries.async_reload(entry.entry_id)
    health = entry.runtime_data.health
    assert old is not health and old._start_listener is old._task is None
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await wait_health(hass, backend, entry)
    assert old.data is None and health.data is not None
    assert len(backend.health_requests) == 1
    assert await hass.config_entries.async_unload(entry.entry_id)
    await health.async_collect()
    assert health._task is health._start_listener is None


async def test_unload_before_start_has_no_late_capture(hass, backend, entry):
    hass.set_state(CoreState.starting)
    hass.config_entries.async_update_entry(entry, options={"health_enabled": True})
    assert await hass.config_entries.async_setup(entry.entry_id)
    health = entry.runtime_data.health
    assert await hass.config_entries.async_unload(entry.entry_id)
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    assert health.data is health._task is health._start_listener is None
    assert health.backup._unsubscribe is None and not backend.health_requests


async def test_failed_startup_collector_recovers_on_next_real_tick(
    hass, backend, entry, freezer
):
    hass.set_state(CoreState.starting)
    hass.config_entries.async_update_entry(entry, options={"health_enabled": True})
    repair(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    with patch(
        "custom_components.ha_monitor_agent.health.collect_repairs",
        side_effect=RuntimeError(backend.token),
    ):
        hass.set_state(CoreState.running)
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
        await wait_health(hass, backend, entry)
    report = backend.health_requests[0][0]["report"]
    assert report["repairs"]["coverage"] == "unavailable"
    assert report["config_entries"]["coverage"] == "complete"
    backend.health_started.clear()
    freezer.tick(301)
    async_fire_time_changed(hass)
    await wait_health(hass, backend, entry)
    recovered = backend.health_requests[-1][0]["report"]
    assert recovered["report_id"] != report["report_id"]
    assert recovered["repairs"]["coverage"] == "complete"
    assert recovered["repairs"]["total"] == 1
    # Loaded entries and deliberate disabling are covered separately by every
    # native ConfigEntryState test; no failure severity is fabricated here.
    assert entry.state is ConfigEntryState.LOADED


async def test_backup_api_failure_during_start_is_isolated_and_recovers(
    hass, backend, entry, manager, freezer
):
    hass.config_entries.async_update_entry(
        entry, options={"health_enabled": True, "monitoring_enabled": True}
    )
    with patch.object(
        manager, "async_subscribe_events", side_effect=RuntimeError(backend.token)
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await wait_health(hass, backend, entry)
    health = entry.runtime_data.health
    report = backend.health_requests[0][0]["report"]
    assert report["backup"]["coverage"] == "unavailable"
    assert report["repairs"]["coverage"] == "complete"
    await asyncio.wait_for(backend.monitoring_started.wait(), 2)
    assert backend.requests and backend.monitoring_requests
    health._component_loaded(Event(EVENT_COMPONENT_LOADED, {"component": "backup"}))
    emit(manager, CreateBackupState.IN_PROGRESS)
    emit(manager, CreateBackupState.COMPLETED)
    backend.health_started.clear()
    freezer.tick(301)
    async_fire_time_changed(hass)
    await wait_health(hass, backend, entry)
    assert backend.health_requests[-1][0]["report"]["backup"]["status"] == "ok"
