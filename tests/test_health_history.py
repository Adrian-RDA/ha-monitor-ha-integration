"""Confirmed backup evidence survives locally, without becoming current again."""

import asyncio
from datetime import timedelta
import json
from unittest.mock import patch

from homeassistant.components.backup.manager import CreateBackupState
from homeassistant.const import EVENT_HOMEASSISTANT_FINAL_WRITE
from homeassistant.core import CoreState
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant.common import async_fire_time_changed

from custom_components.ha_monitor_agent.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.ha_monitor_agent.health_history import BackupHistory

from .test_health import make_health, wait_health
from .test_health_collectors import emit
from .test_health_collectors import manager as manager


@pytest.mark.parametrize(
    "terminal", [CreateBackupState.COMPLETED, CreateBackupState.FAILED]
)
async def test_confirmed_metadata_persists_but_never_rehydrates_current_status(
    hass, backend, entry, manager, hass_storage, freezer, terminal
):
    health = await make_health(hass, backend, entry)
    emit(manager, CreateBackupState.IN_PROGRESS)
    assert health.backup_history.last_confirmed is None
    freezer.tick(5)
    emit(manager, terminal, reason=backend.token)
    confirmed = health.backup_history.last_confirmed
    assert confirmed["status"] == (
        "ok" if terminal == CreateBackupState.COMPLETED else "failed"
    )
    await health.async_stop()  # Flush without waiting for the delayed save.
    stored = hass_storage[health.backup_history.store.key]["data"]
    assert stored == confirmed
    assert set(stored) == {
        "status",
        "last_attempt_at",
        "last_success_at",
        "error_code",
        "observed_at",
    }
    assert backend.token not in json.dumps(stored) and backend.code not in json.dumps(
        stored
    )

    freezer.tick(60)
    # Emulate a new coordinator/process with no old in-memory evidence.
    reloaded = await make_health(hass, backend, entry)
    await reloaded.backup_history.async_load()
    assert reloaded.backup_history.last_confirmed == confirmed
    assert reloaded.data["backup"]["coverage"] == "unknown"
    assert reloaded.data["backup"]["last_attempt_at"] is None
    assert reloaded.data["backup"]["last_success_at"] is None
    await reloaded.uploader.async_send_once()
    wire = backend.health_requests[-1][0]["report"]["backup"]
    assert wire["status"] == "unknown"
    assert "observed_at" not in wire
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["health"]["backup_history"] == {
        "interpretation": "historical_only",
        "last_confirmed": confirmed,
    }
    emit(manager, terminal)  # A terminal event alone cannot confirm a new run.
    assert reloaded.backup_history.last_confirmed == confirmed
    emit(manager, CreateBackupState.IN_PROGRESS)
    freezer.tick(5)
    emit(manager, CreateBackupState.COMPLETED)
    await reloaded.async_collect()
    assert reloaded.data["backup"]["status"] == "ok"
    assert reloaded.data["backup"]["last_attempt_at"] != confirmed["last_attempt_at"]
    await reloaded.async_stop()
    assert hass_storage[reloaded.backup_history.store.key]["data"]["status"] == "ok"


async def test_real_entry_reload_and_remove_preserve_then_delete_private_history(
    hass, backend, entry, manager, hass_storage
):
    hass.config_entries.async_update_entry(entry, options={"health_enabled": True})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await wait_health(hass, backend, entry)
    health = entry.runtime_data.health
    emit(manager, CreateBackupState.IN_PROGRESS)
    emit(manager, CreateBackupState.COMPLETED)
    confirmed = health.backup_history.last_confirmed
    key = health.backup_history.store.key
    backend.health_started.clear()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await wait_health(hass, backend, entry)
    assert health.backup._unsubscribe is health._history_task is None
    health = entry.runtime_data.health
    assert health.backup_history.last_confirmed == confirmed
    assert health.data["backup"]["status"] == "unknown"
    assert key in hass_storage
    await hass.config_entries.async_remove(entry.entry_id)
    assert key not in hass_storage


async def test_older_success_remains_local_history_after_a_new_live_failure(
    hass, backend, entry, manager, freezer
):
    health = await make_health(hass, backend, entry)
    emit(manager, CreateBackupState.IN_PROGRESS)
    emit(manager, CreateBackupState.COMPLETED)
    previous = health.backup_history.last_confirmed
    await health.async_stop()
    freezer.tick(60)
    health = await make_health(hass, backend, entry)
    await health.backup_history.async_load()
    emit(manager, CreateBackupState.IN_PROGRESS)
    emit(manager, CreateBackupState.FAILED, reason="backup_agent_unreachable")
    await health.async_collect()
    assert health.data["backup"]["status"] == "failed"
    assert health.data["backup"]["last_success_at"] is None
    assert (
        health.backup_history.last_confirmed["last_success_at"]
        == previous["last_success_at"]
    )


async def test_persistence_failures_do_not_erase_live_evidence_or_stop_uploads(
    hass, backend, entry, manager
):
    health = await make_health(hass, backend, entry)
    with patch.object(
        health.backup_history.store, "async_load", side_effect=OSError("disk")
    ):
        await health.backup_history.async_load()
    with patch.object(
        health.backup_history.store, "async_delay_save", side_effect=OSError("disk")
    ):
        emit(manager, CreateBackupState.IN_PROGRESS)
        emit(manager, CreateBackupState.FAILED, reason="backup_agent_unreachable")
    await health.async_collect()
    await health.uploader.async_send_once()
    assert health.data["backup"]["status"] == "failed"
    assert health.data["backup"]["error_code"] == "connection"
    assert health.uploader.status == "online"
    with patch.object(
        health.backup_history.store, "async_save", side_effect=OSError("disk")
    ):
        await health.backup_history.async_flush()
    assert health.backup_history._dirty
    await health.backup_history.async_flush()
    assert not health.backup_history._dirty


async def test_slow_restore_does_not_gate_startup_or_unload(hass, backend, entry):
    gate, started = asyncio.Event(), asyncio.Event()

    async def slow_load(_self):
        started.set()
        await gate.wait()

    hass.config_entries.async_update_entry(entry, options={"health_enabled": True})
    with patch.object(BackupHistory, "async_load", slow_load):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await asyncio.wait_for(started.wait(), 2)
        await wait_health(hass, backend, entry)
        assert backend.requests
        load_task = entry.runtime_data.health._history_task
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert load_task.cancelled()


async def test_stalled_storage_write_cannot_hold_unload_indefinitely(hass):
    history = BackupHistory(hass, "isolated")
    history.observed(evidence())
    gate = asyncio.Event()

    async def stalled_write(_data):
        await gate.wait()

    with (
        patch.object(history.store, "async_save", stalled_write),
        patch(
            "custom_components.ha_monitor_agent.health_history.STORAGE_TIMEOUT", 0.01
        ),
    ):
        await asyncio.wait_for(history.async_flush(), 1)
    assert history._dirty
    await history.async_flush()
    assert not history._dirty


def evidence():
    now = dt_util.utcnow()
    return {
        "status": "ok",
        "error_code": None,
        "last_attempt_at": (now - timedelta(seconds=20)).isoformat(),
        "last_success_at": (now - timedelta(seconds=10)).isoformat(),
        "observed_at": (now - timedelta(seconds=10)).isoformat(),
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "running"},
        {"extra": "not allowed"},
        {"status": []},
        {"last_attempt_at": "invalid"},
        {"last_attempt_at": "2026-01-01T00:00:00"},
        {"last_success_at": None},
        {"observed_at": "2099-01-01T00:00:00+00:00"},
        {"error_code": "private raw exception"},
        {"status": "failed", "error_code": []},
        {
            "status": "failed",
            "error_code": "connection",
        },  # success after failed attempt
        {"last_attempt_at": "2099-01-01T00:00:00+00:00"},
    ],
)
async def test_corrupt_or_unconfirmed_storage_is_ignored(hass, changes):
    history = BackupHistory(hass, "isolated")
    with patch.object(
        history.store, "async_load", return_value={**evidence(), **changes}
    ):
        await history.async_load()
    assert history.last_confirmed is None


async def test_live_confirmation_wins_restore_race_and_writes_coalesce(
    hass, freezer, hass_storage
):
    history = BackupHistory(hass, "isolated")
    old = evidence()
    gate, started = asyncio.Event(), asyncio.Event()

    async def delayed_load():
        started.set()
        await gate.wait()
        return old

    with patch.object(history.store, "async_load", delayed_load):
        task = asyncio.create_task(history.async_load())
        await started.wait()
        freezer.tick(60)
        newest = evidence()
        for _ in range(10):
            history.observed(newest)
        gate.set()
        await task
    assert history.last_confirmed == newest
    # HA's delayed-save helper retains a single value, then atomically writes it.
    freezer.tick(2)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass_storage[history.store.key]["data"] == newest


async def test_final_write_flush_and_clock_rollback(hass, freezer, hass_storage):
    history = BackupHistory(hass, "isolated")
    history.observed(evidence())
    hass.set_state(CoreState.stopping)
    await history.async_flush()
    hass.bus.async_fire(EVENT_HOMEASSISTANT_FINAL_WRITE)
    await hass.async_block_till_done()
    assert hass_storage[history.store.key]["data"] == history.last_confirmed
    freezer.tick(-100)
    assert history.last_confirmed is None
    hass.set_state(CoreState.running)


@pytest.mark.parametrize(
    "options", [{}, {"health_enabled": True, "health_sections": ["repairs"]}]
)
async def test_no_history_io_or_subscription_without_backup_consent(
    hass, backend, entry, options
):
    hass.config_entries.async_update_entry(entry, options=options)
    with patch.object(BackupHistory, "async_load") as load:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert not load.called
        health = entry.runtime_data.health
        assert health._history_task is health.backup._unsubscribe is None
        assert not health.backup_history._dirty
