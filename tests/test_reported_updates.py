"""Regression cases reported through HA Privat: Core, Shelly, Immich, Proxmox."""

import json
from unittest.mock import patch

from homeassistant.components.update import UpdateEntity, UpdateEntityFeature
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
import pytest

from custom_components.ha_monitor_agent import updates
from custom_components.ha_monitor_agent.const import DOMAIN
from custom_components.ha_monitor_agent.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.ha_monitor_agent.updates import collect_update_inventory

from .test_monitoring import make_monitor
from .test_updates import source
from .test_updates import supervisor_updates as supervisor_updates

CORE = "update.home_assistant_core_update"
SHELLY = "update.carport_licht_firmware"
IMMICH = "update.jonas_lindemann_version"
PROXMOX = "update.node_pxmx01_software_update"
REPORTED = {CORE, SHELLY, IMMICH, PROXMOX}


@pytest.fixture
def reported_updates(hass, supervisor_updates):
    from homeassistant.helpers import entity_registry as er

    core_id, os_id = supervisor_updates
    er.async_get(hass).async_update_entity(core_id, new_entity_id=CORE)
    hass.states.async_remove(core_id)
    hass.states.async_set(
        CORE,
        "on",
        {
            "installed_version": "2026.11.0",
            "latest_version": "2026.11.1",
            "supported_features": 15,
        },
    )
    hass.states.async_set(
        os_id,
        "off",
        {
            "installed_version": "17.0",
            "latest_version": "17.0",
            "supported_features": 3,
        },
    )
    source(
        hass,
        SHELLY,
        installed_version="1.6.2",
        latest_version="1.7.0",
        supported_features=5,
        device_class="firmware",
    )
    source(
        hass,
        IMMICH,
        platform="immich",
        installed_version="v2.3.0",
        latest_version="v2.3.1",
        supported_features=0,
    )
    # The official Proxmox integration reports a version ID with package counts.
    # This is deliberately not parsed/reordered as SemVer by the agent.
    source(
        hass,
        PROXMOX,
        platform="proxmoxve",
        installed_version="8.4.1",
        latest_version="8.4.1-p0-d17",
        supported_features=16,
    )
    return REPORTED


async def test_reported_running_options_discover_four_but_export_only_core(
    hass, backend, entry, reported_updates
):
    monitor = await make_monitor(
        hass,
        backend,
        entry,
        discover_updates=True,
        system_updates=True,
        export_all_updates=False,
        update_entities=[],
    )
    assert {record.entity_id for record in monitor.update_inventory.offered} == REPORTED
    await monitor.uploader.async_send_once()
    sent = backend.monitoring_requests[-1][0]["snapshots"][0]["updates"]
    assert sent["status"] == "ok" and {item["key"] for item in sent["items"]} == {CORE}
    summary = monitor.update_summary()
    assert summary["known_available"] == 4
    assert summary["not_installable"] == 2
    assert summary["not_exported"] == 3
    assert summary["export_item_count"] == 1
    assert summary["blocked_known_available"] == 0
    assert ir.async_get(hass).async_get_issue(DOMAIN, monitor.update_export_issue_id)


@pytest.mark.parametrize("discover", [True, False])
async def test_export_all_exports_reported_four_independently_of_local_discovery(
    hass, backend, entry, reported_updates, discover
):
    monitor = await make_monitor(
        hass,
        backend,
        entry,
        discover_updates=discover,
        export_all_updates=True,
        update_entities=[],
    )
    await monitor.uploader.async_send_once()
    sent = backend.monitoring_requests[-1][0]["snapshots"][0]["updates"]
    assert sent["status"] == "ok" and len(sent["items"]) == 4
    assert {item["key"] for item in sent["items"]} == REPORTED
    assert sent["items"][0]["kind"] == "core"
    records = {record.entity_id: record for record in monitor.update_inventory.offered}
    assert records[CORE].supports_install is records[SHELLY].supports_install is True
    assert (
        records[IMMICH].supports_install is records[PROXMOX].supports_install is False
    )
    assert records[IMMICH].source == "immich" and records[PROXMOX].source == "proxmoxve"
    assert records[IMMICH].latest_version == "v2.3.1"
    assert records[PROXMOX].latest_version == "8.4.1-p0-d17"
    # Every reported state has no title; fallback names remain valid and nonempty.
    for item in sent["items"]:
        assert item["name"]
        assert set(item) == {
            "key",
            "kind",
            "name",
            "installed_version",
            "available_version",
        }
    summary = monitor.update_summary()
    assert summary["export_item_count"] == 4 and summary["not_exported"] == 0
    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, monitor.update_export_issue_id)
        is None
    )


async def test_explicitly_selecting_reported_sources_exports_without_export_all(
    hass, backend, entry, reported_updates
):
    monitor = await make_monitor(
        hass,
        backend,
        entry,
        export_all_updates=False,
        update_entities=[IMMICH, PROXMOX, SHELLY],
    )
    await monitor.uploader.async_send_once()
    assert {
        item["key"]
        for item in backend.monitoring_requests[-1][0]["snapshots"][0]["updates"][
            "items"
        ]
    } == REPORTED


@pytest.mark.parametrize(
    ("features", "supports_install"),
    [
        (0, False),
        (1, True),
        (4, False),
        (5, True),
        (15, True),
        (16, False),
        (17, True),
        (UpdateEntityFeature.RELEASE_NOTES, False),
        (UpdateEntityFeature.INSTALL | UpdateEntityFeature.PROGRESS, True),
        (True, None),
        (False, None),
        (-1, None),
        ("15", None),
        (None, None),
    ],
)
async def test_install_flag_is_metadata_and_never_an_availability_filter(
    hass, features, supports_install
):
    entity = source(hass, "update.feature_test", supported_features=features)
    inventory = collect_update_inventory(hass, False, [], discover=True)
    record = inventory.offered[0]
    assert (
        record.entity_id == entity.entity_id
        and record.supports_install is supports_install
    )
    assert inventory.as_protocol()["items"][0]["key"] == entity.entity_id


class PackageUpdate(UpdateEntity):
    """Native HA state logic with Proxmox-like package count version identifiers."""

    _attr_name = "Package update"
    _attr_entity_picture = None
    _attr_should_poll = False
    _attr_installed_version = "8.4.1"
    _attr_latest_version = "8.4.1-p0-d17"
    _attr_supported_features = UpdateEntityFeature.RELEASE_NOTES
    _attr_title = None

    @property
    def has_update(self):
        return True


async def test_native_package_update_without_title_or_install_is_exportable(hass):
    assert await async_setup_component(hass, "update", {})
    entity = PackageUpdate()
    await hass.data["update"].async_add_entities([entity])
    assert hass.states.get(entity.entity_id).state == "on"
    inventory = collect_update_inventory(hass, False, [], discover=True)
    assert inventory.offered[0].supports_install is False
    assert inventory.as_protocol()["items"][0]["available_version"] == "8.4.1-p0-d17"


async def test_single_entity_exception_keeps_all_reported_observations_and_heartbeat(
    hass, backend, entry, reported_updates, caplog
):
    broken = source(hass, "update.broken")
    real_observe = updates._observe

    def observe(hass, entity_id, selected):
        if entity_id == broken.entity_id:
            raise RuntimeError(backend.token + backend.code)
        return real_observe(hass, entity_id, selected)

    with patch(
        "custom_components.ha_monitor_agent.updates._observe", side_effect=observe
    ):
        monitor = await make_monitor(hass, backend, entry, export_all_updates=True)
    inventory = monitor.update_inventory
    assert {record.entity_id for record in inventory.offered} == REPORTED
    failed = next(
        record for record in inventory.records if record.entity_id == broken.entity_id
    )
    assert failed.availability == "unavailable" and failed.latest_version is None
    assert inventory.summary()["failed"] == 1
    assert inventory.protocol_issues == {
        "source_unavailable": 1,
        "source_collection_failed": 1,
    }
    await monitor.uploader.async_send_once()
    # v1 does not allow falsely labelling these four as a complete inventory.
    assert backend.monitoring_requests[-1][0]["snapshots"][0]["updates"] == {
        "status": "unavailable",
        "items": [],
    }
    summary = monitor.update_summary()
    assert summary["known_available"] == summary["blocked_known_available"] == 4
    assert summary["export_item_count"] == 0
    await monitor.uploader.heartbeat.async_send_once()
    assert monitor.uploader.heartbeat.status == "online"
    diagnostic = await async_get_config_entry_diagnostics(hass, entry)
    output = json.dumps(diagnostic, default=str) + caplog.text
    assert backend.token not in output and backend.code not in output


async def test_broken_entity_before_other_sources_does_not_abort_remaining_collection(
    hass, backend
):
    broken = source(hass, "update.aaa_broken")
    healthy = source(hass, "update.zzz_healthy", supported_features=0)
    real_observe = updates._observe

    def observe(hass, entity_id, selected):
        if entity_id == broken.entity_id:
            raise ValueError(backend.token)
        return real_observe(hass, entity_id, selected)

    with patch(
        "custom_components.ha_monitor_agent.updates._observe", side_effect=observe
    ):
        inventory = collect_update_inventory(hass, False, [], discover=True)
    assert [record.entity_id for record in inventory.offered] == [healthy.entity_id]
    assert len(inventory.records) == 2 and inventory.summary()["failed"] == 1


@pytest.mark.parametrize(
    "raw_version",
    ["20261021-083500/v1.14.0-g1234", "9.0.3 (17 packages)", "release/2026.11"],
)
async def test_unusual_unsupported_versions_are_retained_without_hiding_peers_locally(
    hass, backend, entry, reported_updates, raw_version
):
    unusual = source(hass, "update.unusual", latest_version=raw_version, title=None)
    monitor = await make_monitor(hass, backend, entry, export_all_updates=True)
    assert len(monitor.update_inventory.offered) == 5
    record = next(
        record
        for record in monitor.update_inventory.offered
        if record.entity_id == unusual.entity_id
    )
    assert record.latest_version == raw_version
    assert {record.entity_id for record in monitor.update_inventory.offered} >= REPORTED
    await monitor.uploader.async_send_once()
    assert monitor.update_summary()["protocol_issues"] == {
        "latest_version_not_representable": 1
    }
    assert monitor.update_summary()["blocked_known_available"] == 5
    assert (
        backend.monitoring_requests[-1][0]["snapshots"][0]["updates"]["status"]
        == "unavailable"
    )
