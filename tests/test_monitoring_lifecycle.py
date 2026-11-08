"""Periodic delivery through HA's actual setup, disable and re-enable lifecycle."""

import asyncio
from datetime import timedelta
from itertools import count
from types import SimpleNamespace
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryDisabler
import pytest
from pytest_homeassistant.common import async_fire_time_changed

from custom_components.ha_monitor_agent.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .test_collectors import CPU


async def wait_for_monitoring(hass, backend, entry):
    await asyncio.wait_for(backend.monitoring_started.wait(), 2)
    async with entry.runtime_data.monitoring.uploader._send_lock:
        pass
    await hass.async_block_till_done()
    if task := getattr(entry.runtime_data.monitoring, "_collection_task", None):
        await task


@pytest.mark.parametrize("disable_polling", [False, True])
async def test_periodic_resource_uploads_survive_disable_and_reenable(
    hass, backend, entry, freezer, disable_polling
):
    hass.config_entries.async_update_entry(
        entry,
        options={
            "monitoring_enabled": True,
            "export_metrics": ["cpu_percent", "memory_percent", "storage_percent"],
        },
        pref_disable_polling=disable_polling,
    )
    samples = count()

    def cpu_times():
        index = next(samples)
        return CPU(10 + index * 25, 0, 5, 85 + index * 75, 0, 0, 0)

    with (
        patch(
            "custom_components.ha_monitor_agent.collectors.psutil.cpu_times",
            side_effect=cpu_times,
        ),
        patch(
            "custom_components.ha_monitor_agent.collectors.psutil.virtual_memory",
            return_value=SimpleNamespace(total=1000, available=600),
        ),
        patch(
            "custom_components.ha_monitor_agent.collectors.psutil.disk_usage",
            return_value=SimpleNamespace(total=1000, used=300),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await wait_for_monitoring(hass, backend, entry)
        initial = backend.monitoring_requests[-1][0]["snapshots"][0]
        assert initial["metrics"]["cpu_percent"]["status"] == "unknown"
        for _ in range(2):
            backend.monitoring_started.clear()
            freezer.tick(timedelta(seconds=65))
            async_fire_time_changed(hass)
            await hass.async_block_till_done()
            if task := getattr(entry.runtime_data.monitoring, "_collection_task", None):
                await task
            assert (
                entry.runtime_data.monitoring.data["snapshot_id"]
                != initial["snapshot_id"]
            )
            await wait_for_monitoring(hass, backend, entry)
            snapshot = backend.monitoring_requests[-1][0]["snapshots"][0]
            assert snapshot["snapshot_id"] != initial["snapshot_id"]
            assert snapshot["metrics"] == {
                "cpu_percent": {"status": "ok", "scope": "host", "value": 25},
                "memory_percent": {"status": "ok", "scope": "host", "value": 40},
                "storage_percent": {"status": "ok", "scope": "data", "value": 30},
            }
            diagnostic = (await async_get_config_entry_diagnostics(hass, entry))[
                "monitoring"
            ]
            assert diagnostic["collection_success"] is True
            assert diagnostic["last_observed_at"] == snapshot["observed_at"]
            assert all(
                metric == {"status": "ok", "exported": True}
                for metric in diagnostic["metrics"].values()
            )
        previous = entry.runtime_data.monitoring
        await hass.config_entries.async_set_disabled_by(
            entry.entry_id, ConfigEntryDisabler.USER
        )
        sent = len(backend.monitoring_requests)
        freezer.tick(timedelta(seconds=130))
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert len(backend.monitoring_requests) == sent
        backend.monitoring_started.clear()
        await hass.config_entries.async_set_disabled_by(entry.entry_id, None)
        await wait_for_monitoring(hass, backend, entry)
        assert entry.runtime_data.monitoring is not previous
        assert (
            backend.monitoring_requests[-1][0]["snapshots"][0]["metrics"][
                "cpu_percent"
            ]["status"]
            == "unknown"
        )
        for _ in range(2):
            backend.monitoring_started.clear()
            freezer.tick(timedelta(seconds=65))
            async_fire_time_changed(hass)
            await wait_for_monitoring(hass, backend, entry)
            metrics = backend.monitoring_requests[-1][0]["snapshots"][0]["metrics"]
            assert all(item["status"] == "ok" for item in metrics.values())
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_slow_collection_skips_timer_ticks_and_unload_cancels_it(
    hass, backend, entry, freezer
):
    gate = asyncio.Event()
    started = asyncio.Event()
    resource_calls = 0
    real_executor = hass.async_add_executor_job

    async def executor(target, *args):
        nonlocal resource_calls
        if getattr(target, "__name__", None) == "collect":
            resource_calls += 1
            started.set()
            await gate.wait()
        return await real_executor(target, *args)

    hass.config_entries.async_update_entry(entry, options={"monitoring_enabled": True})
    with (
        patch.object(hass, "async_add_executor_job", side_effect=executor),
        # Drive heartbeat requests explicitly between clock jumps so mocked time
        # cannot expire an unrelated in-flight TLS handshake.
        patch("custom_components.ha_monitor_agent.sender.HeartbeatSender.async_start"),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await asyncio.wait_for(started.wait(), 2)
        monitor = entry.runtime_data.monitoring
        task = monitor._collection_task
        monitor.async_start()  # Repeated start must not add a second clock.
        for _ in range(3):
            freezer.tick(timedelta(seconds=65))
            async_fire_time_changed(hass)
            await hass.async_block_till_done()
            assert monitor._collection_task is task and resource_calls == 1
            await entry.runtime_data.async_send_once()
        assert entry.runtime_data.status == "online"
        assert not backend.monitoring_requests
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert task.done() and monitor._collection_task is None
        assert monitor._unsubscribe is None
        monitor.async_start()
        freezer.tick(timedelta(seconds=130))
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert resource_calls == 1
