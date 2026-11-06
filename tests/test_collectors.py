"""Local collectors use real HA registries/states and simulated OS readings."""

import asyncio
from collections import namedtuple
from types import SimpleNamespace
from unittest.mock import patch

from homeassistant.core import CoreState
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
import pytest

from custom_components.ha_monitor_agent.collectors import (
    ResourceCollector,
    collect_inventory,
    collect_updates,
    percentage,
)

CPU = namedtuple("CPU", "user nice system idle iowait guest guest_nice")


async def test_resources_executor_scope_and_exact_denominators(hass):
    collector = ResourceCollector(hass.config.config_dir)
    samples = iter([CPU(10, 0, 5, 85, 0, 0, 0), CPU(30, 0, 10, 155, 5, 0, 0)])

    def cpu_times():
        with pytest.raises(RuntimeError):
            asyncio.get_running_loop()
        return next(samples)

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
            return_value=SimpleNamespace(total=1000, used=300, percent=33.3),
        ) as disk,
    ):
        first = await hass.async_add_executor_job(collector.collect)
        second = await hass.async_add_executor_job(collector.collect)
    assert first["cpu_percent"] == {"status": "unknown", "value": None, "scope": "host"}
    assert second["cpu_percent"] == {"status": "ok", "value": 25, "scope": "host"}
    assert second["memory_percent"] == {"status": "ok", "value": 40, "scope": "host"}
    assert second["storage_percent"] == {"status": "ok", "value": 30, "scope": "data"}
    disk.assert_called_with(hass.config.config_dir)


async def test_resource_failures_isolate_and_reset_cpu(hass):
    collector = ResourceCollector("/config")
    with (
        patch(
            "custom_components.ha_monitor_agent.collectors.psutil.cpu_times",
            side_effect=[
                CPU(10, 0, 5, 85, 0, 0, 0),
                PermissionError(),
                CPU(40, 0, 20, 140, 0, 0, 0),
            ],
        ),
        patch(
            "custom_components.ha_monitor_agent.collectors.psutil.virtual_memory",
            side_effect=NotImplementedError(),
        ),
        patch(
            "custom_components.ha_monitor_agent.collectors.psutil.disk_usage",
            side_effect=OSError(),
        ),
    ):
        first = await hass.async_add_executor_job(collector.collect)
        failed = await hass.async_add_executor_job(collector.collect)
        recovered = await hass.async_add_executor_job(collector.collect)
    assert first["memory_percent"]["status"] == "unsupported"
    assert first["storage_percent"]["status"] == "unavailable"
    assert failed["cpu_percent"]["status"] == "unavailable"
    assert recovered["cpu_percent"]["status"] == "unknown"


@pytest.mark.parametrize(
    ("used", "total"),
    [(0, 0), (20, 10), (-1, 10), (float("nan"), 10), (10, float("inf"))],
)
def test_invalid_resource_readings_never_become_zero(used, total):
    assert percentage(used, total, "host") == {
        "status": "unavailable",
        "value": None,
        "scope": "host",
    }


async def test_inventory_contract_includes_disabled_and_yaml(hass, entry):
    hass.state = CoreState.starting
    assert all(v["status"] == "unknown" for v in collect_inventory(hass).values())
    hass.state = CoreState.running
    registry = er.async_get(hass)
    registry.async_get_or_create("sensor", "demo", "enabled")
    registry.async_get_or_create(
        "sensor", "demo", "disabled", disabled_by=er.RegistryEntryDisabler.USER
    )
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("demo", "one")}
    )
    hass.states.async_set("sensor.unregistered", "1")
    hass.states.async_set("automation.disabled", "off")
    hass.states.async_set("scene.example", "unknown")
    hass.config.components.update({"mqtt", "sensor.mqtt", "automation", "scene"})
    result = collect_inventory(hass)
    assert result["entities"]["value"] == 2
    assert result["devices"]["value"] == 1
    assert result["automations"]["value"] == result["scenes"]["value"] == 1
    assert result["integrations"]["value"] == len(
        {name.split(".")[0] for name in hass.config.components}
    )
    hass.states.async_remove("automation.disabled")
    assert collect_inventory(hass)["automations"] == {"status": "ok", "value": 0}


async def test_updates_selection_completeness_and_no_attributes_exported(hass, backend):
    hass.state = CoreState.starting
    assert collect_updates(hass, False, []) == {"status": "unknown", "items": []}
    hass.state = CoreState.running
    assert collect_updates(hass, False, []) == {"status": "unsupported", "items": []}
    hass.states.async_set(
        "update.firmware",
        "on",
        {
            "installed_version": "1.0",
            "latest_version": "1.1",
            "friendly_name": backend.token,
            "release_notes": backend.code,
        },
    )
    hass.states.async_set(
        "update.hacs",
        "off",
        {"installed_version": "2", "latest_version": "3", "skipped_version": "3"},
    )
    result = collect_updates(
        hass, False, ["update.hacs", "update.firmware", "update.firmware"]
    )
    assert result["status"] == "ok"
    assert [item["key"] for item in result["items"]] == [
        "update.firmware",
        "update.hacs",
    ]
    assert all(item["kind"] == "other" for item in result["items"])
    assert backend.token not in str(result) and backend.code not in str(result)
    assert collect_updates(hass, False, ["update.firmware", "update.missing"]) == {
        "status": "unavailable",
        "items": [],
    }
    hass.states.async_set("update.firmware", "unavailable")
    assert collect_updates(hass, False, ["update.firmware"])["status"] == "unavailable"
    hass.states.async_set(
        "update.firmware", "off", {"installed_version": "1.1", "latest_version": "1.1"}
    )
    assert collect_updates(hass, False, ["update.firmware"]) == {
        "status": "ok",
        "items": [],
    }


@pytest.mark.parametrize(
    "attributes",
    [
        {"latest_version": "not a version"},
        {"latest_version": "1.1", "installed_version": "private value"},
        {"latest_version": None},
        {"latest_version": 1},
    ],
)
async def test_invalid_update_metadata_marks_whole_collection_unavailable(
    hass, attributes
):
    hass.state = CoreState.running
    hass.states.async_set("update.source", "on", attributes)
    assert collect_updates(hass, False, ["update.source"]) == {
        "status": "unavailable",
        "items": [],
    }


async def test_update_source_bound(hass):
    hass.state = CoreState.running
    assert collect_updates(hass, False, [f"update.x{i}" for i in range(101)]) == {
        "status": "unavailable",
        "items": [],
    }


async def test_supervisor_core_os_identity_survives_renames(hass):
    from aiohasupervisor.models import RootInfo
    from homeassistant.components.hassio.const import DATA_INFO

    hass.state = CoreState.running
    hass.config.components.add("hassio")
    hass.data[DATA_INFO] = RootInfo.from_dict(
        {
            "hassos": "17.0",
            "homeassistant": "2026.11.0",
            "supervisor": "2026.11.0",
            "docker": "28.0.0",
            "hostname": "test",
            "operating_system": "Home Assistant OS 17.0",
            "features": [],
            "machine": "qemux86-64",
            "machine_id": "test",
            "arch": "amd64",
            "state": "running",
            "supported_arch": ["amd64"],
            "supported": True,
            "channel": "stable",
            "logging": "info",
            "timezone": "UTC",
        }
    )
    registry = er.async_get(hass)
    for kind in ("core", "os"):
        entity = registry.async_get_or_create(
            "update", "hassio", f"home_assistant_{kind}_version_latest"
        )
        registry.async_update_entity(
            entity.entity_id, new_entity_id=f"update.renamed_{kind}"
        )
        hass.states.async_set(
            f"update.renamed_{kind}",
            "on",
            {"installed_version": "1", "latest_version": "2"},
        )
    result = collect_updates(hass, True, [])
    assert result["status"] == "ok"
    assert [item["kind"] for item in result["items"]] == ["core", "os"]
    hass.states.async_remove("update.renamed_os")
    assert collect_updates(hass, True, []) == {"status": "unavailable", "items": []}
    registry.async_remove("update.renamed_os")
    assert collect_updates(hass, True, ["update.renamed_core"]) == {
        "status": "unavailable",
        "items": [],
    }
    hass.data.pop(DATA_INFO)
    assert collect_updates(hass, True, []) == {"status": "unavailable", "items": []}
