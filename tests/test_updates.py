"""HA update semantics, complete discovery and explicit Snapshot v1 limitations."""

from dataclasses import FrozenInstanceError
import json
from unittest.mock import patch

from homeassistant.components.update import UpdateEntity, UpdateEntityFeature
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
import pytest

from custom_components.ha_monitor_agent.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.ha_monitor_agent.updates import collect_update_inventory

from .test_monitoring import make_monitor


def source(hass, entity_id, platform="shelly", state="on", **attributes):
    registry = er.async_get(hass)
    entry = registry.async_get_or_create(
        "update",
        platform,
        entity_id,
        suggested_object_id=entity_id.removeprefix("update."),
    )
    hass.states.async_set(
        entry.entity_id,
        state,
        {
            "installed_version": "1.0",
            "latest_version": "1.1",
            "supported_features": int(UpdateEntityFeature.INSTALL),
            **attributes,
        },
    )
    return entry


async def test_discovery_all_products_and_read_only_updates_reach_tls_backend(
    hass, backend, entry
):
    registry = er.async_get(hass)
    products = [
        ("core", "hassio", "home_assistant_core_version_latest"),
        ("os", "hassio", "home_assistant_os_version_latest"),
        ("supervisor", "hassio", "home_assistant_supervisor_version_latest"),
        ("addon", "hassio", "example_addon_version_latest"),
        ("hacs", "hacs", "hacs_release"),
        ("firmware", "shelly", "shelly_one"),
        ("firmware", "shelly", "shelly_two"),
        ("other", "demo", "manual_one"),
        ("other", "demo", "manual_two"),
    ]
    for category, platform, unique in products:
        entity = registry.async_get_or_create("update", platform, unique)
        hass.states.async_set(
            entity.entity_id,
            "on",
            {
                "installed_version": "1.0",
                "latest_version": "1.1",
                "supported_features": 0
                if unique.startswith("manual")
                else UpdateEntityFeature.INSTALL,
                "device_class": "firmware" if category == "firmware" else None,
            },
        )
    monitor = await make_monitor(hass, backend, entry)
    assert [record.category for record in monitor.update_inventory.records].count(
        "firmware"
    ) == 2
    assert monitor.update_inventory.summary()["not_installable"] == 2
    await monitor.uploader.async_send_once()
    sent = backend.monitoring_requests[0][0]["snapshots"][0]["updates"]
    assert sent["status"] == "ok" and len(sent["items"]) == 9
    assert [item["kind"] for item in sent["items"][:2]] == ["core", "os"]
    assert len(backend.monitoring_stored) == 1
    assert {item["key"] for item in sent["items"]} == {
        record.entity_id for record in monitor.update_inventory.records
    }
    assert all(
        set(item) == {"key", "kind", "name", "installed_version", "available_version"}
        for item in sent["items"]
    )


class ReadOnlyUpdate(UpdateEntity):
    """Real HA entity; HA can detect/skip this update but cannot install it."""

    _attr_name = "Manual update"
    _attr_installed_version = "1.0"
    _attr_latest_version = "2.0"
    _attr_supported_features = UpdateEntityFeature(0)
    _attr_entity_picture = None
    _attr_should_poll = False


async def test_native_ha_state_on_and_skip_are_independent_of_install_feature(hass):
    assert await async_setup_component(hass, "update", {})
    entity = ReadOnlyUpdate()
    await hass.data["update"].async_add_entities([entity])
    assert hass.states.get(entity.entity_id).state == "on"
    inventory = collect_update_inventory(hass, False, [], discover=True)
    record = next(
        record for record in inventory.records if record.entity_id == entity.entity_id
    )
    assert record.availability == "available" and record.supports_install is False
    assert inventory.as_protocol()["items"][0]["available_version"] == "2.0"
    await entity.async_skip()
    assert hass.states.get(entity.entity_id).state == "off"
    skipped = collect_update_inventory(hass, False, [], discover=True)
    assert skipped.offered[0].availability == "skipped"
    assert skipped.offered[0].skipped_version == "2.0"
    assert skipped.as_protocol()["items"] == inventory.as_protocol()["items"]
    await entity.async_clear_skipped()
    assert (
        collect_update_inventory(hass, False, [], discover=True).offered[0].availability
        == "available"
    )


async def test_raw_shelly_firmware_blocks_only_wire_projection_preserving_metadata(
    hass, backend, entry
):
    registry = er.async_get(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("shelly", "fixture")},
        name="Shelly relay",
        manufacturer="Shelly",
        model="Shelly 1",
    )
    entity = registry.async_get_or_create(
        "update", "shelly", "firmware", device_id=device.id
    )
    installed, latest = (
        "20230913-112003/v1.14.0-gcb84623",
        "20241011-123000/v1.14.1-g1234567",
    )
    hass.states.async_set(
        entity.entity_id,
        "on",
        {
            "installed_version": installed,
            "latest_version": latest,
            "supported_features": 0,
            "device_class": "firmware",
            "friendly_name": "Shelly firmware",
            "release_notes": backend.token,
            "release_url": backend.code,
        },
    )
    monitor = await make_monitor(hass, backend, entry)
    inventory = monitor.update_inventory
    record = inventory.offered[0]
    assert inventory.status == "ok" and len(inventory.offered) == 1
    assert (record.installed_version, record.latest_version) == (installed, latest)
    assert (
        record.source,
        record.device_id,
        record.device_name,
        record.device_manufacturer,
        record.device_model,
    ) == ("shelly", device.id, "Shelly relay", "Shelly", "Shelly 1")
    assert record.supports_install is False
    assert inventory.as_protocol() == {"status": "unavailable", "items": []}
    assert inventory.protocol_issues == {
        "latest_version_not_representable": 1,
        "installed_version_not_representable": 1,
    }
    with pytest.raises(FrozenInstanceError):
        record.latest_version = "fake"
    await monitor.uploader.async_send_once()
    assert (
        backend.monitoring_requests[-1][0]["snapshots"][0]["updates"]
        == inventory.as_protocol()
    )
    diagnostic = await async_get_config_entry_diagnostics(hass, entry)
    serialized = json.dumps(diagnostic, default=str)
    assert diagnostic["monitoring"]["updates"]["known_available"] == 1
    assert diagnostic["monitoring"]["updates"]["protocol_status"] == "unavailable"
    assert all(
        secret not in serialized
        for secret in (
            backend.token,
            backend.code,
            installed,
            latest,
            device.id,
            "Shelly relay",
        )
    )


async def test_up_to_date_opaque_versions_do_not_hide_other_available_updates(hass):
    source(
        hass,
        "update.no_update",
        state="off",
        installed_version="20241011/v1.14.1",
        latest_version="20241011/v1.14.1",
    )
    available = source(hass, "update.available", supported_features=0)
    inventory = collect_update_inventory(hass, False, [], discover=True)
    assert inventory.status == "ok" and inventory.protocol_issues == {}
    assert [item["key"] for item in inventory.as_protocol()["items"]] == [
        available.entity_id
    ]


@pytest.mark.parametrize(
    ("latest", "skipped", "installed", "expected"),
    [
        ("2.0", "2.0", "1.0", "skipped"),
        ("2.0", "1.5", "1.0", "up_to_date"),
        ("2.0", "2.0", "2.0", "up_to_date"),
        (None, "2.0", "1.0", "up_to_date"),
    ],
)
async def test_only_current_skipped_version_counts_as_offered(
    hass, latest, skipped, installed, expected
):
    source(
        hass,
        "update.skipped",
        state="off",
        installed_version=installed,
        latest_version=latest,
        skipped_version=skipped,
    )
    record = collect_update_inventory(hass, False, [], discover=True).records[0]
    assert record.availability == expected


async def test_disabled_unknown_unavailable_hidden_and_stale_states(hass):
    registry = er.async_get(hass)
    enabled = source(hass, "update.visible")
    hidden = source(hass, "update.hidden")
    registry.async_update_entity(hidden.entity_id, hidden_by=er.RegistryEntryHider.USER)
    disabled = source(hass, "update.disabled")
    registry.async_update_entity(
        disabled.entity_id, disabled_by=er.RegistryEntryDisabler.USER
    )
    # Stale on state is deliberately left in the state machine.
    inventory = collect_update_inventory(hass, False, [], discover=True)
    assert inventory.status == "ok"
    assert {item["key"] for item in inventory.as_protocol()["items"]} == {
        enabled.entity_id,
        hidden.entity_id,
    }
    assert inventory.summary()["disabled"] == 1
    record = next(
        record for record in inventory.records if record.entity_id == disabled.entity_id
    )
    assert record.latest_version is None and record.supports_install is None
    assert (
        registry.async_get(disabled.entity_id).disabled_by
        == er.RegistryEntryDisabler.USER
    )
    selected = collect_update_inventory(
        hass, False, [disabled.entity_id], discover=True
    )
    assert (
        selected.status == "unavailable"
        and selected.protocol_issues["selected_source_disabled"] == 1
    )
    missing = source(hass, "update.offline", state="unavailable")
    unknown = source(hass, "update.unknown", state="unknown")
    incomplete = collect_update_inventory(hass, False, [], discover=True)
    assert incomplete.status == "unavailable" and len(incomplete.offered) == 2
    assert incomplete.protocol_issues["source_unavailable"] == 2
    assert incomplete.as_protocol() == {"status": "unavailable", "items": []}
    hass.states.async_remove(missing.entity_id)
    hass.states.async_remove(unknown.entity_id)
    assert (
        collect_update_inventory(hass, False, [], discover=True).status == "unavailable"
    )


async def test_limit_counts_available_records_not_up_to_date_sources(hass):
    for i in range(102):
        source(hass, f"update.source_{i}", state="off", installed_version="1.1")
    one = source(hass, "update.available")
    inventory = collect_update_inventory(hass, False, [], discover=True)
    assert len(inventory.records) == 103
    assert inventory.as_protocol()["status"] == "ok"
    assert inventory.as_protocol()["items"][0]["key"] == one.entity_id
    for i in range(100):
        source(hass, f"update.offered_{i}")
    inventory = collect_update_inventory(hass, False, [], discover=True)
    assert len(inventory.offered) == 101 and inventory.status == "ok"
    assert inventory.protocol_issues["item_limit"] == 1
    assert inventory.as_protocol() == {"status": "unavailable", "items": []}


async def test_source_cap_does_not_claim_complete_inventory(hass):
    for i in range(3):
        source(hass, f"update.source_{i}")
    with patch("custom_components.ha_monitor_agent.updates.MAX_UPDATE_SOURCES", 2):
        inventory = collect_update_inventory(hass, False, [], discover=True)
    assert inventory.status == "unavailable" and inventory.protocol_issues == {
        "source_limit": 1
    }


async def test_discovery_option_retains_explicit_selection_and_core_os_exclusion(hass):
    source(hass, "update.automatic")
    registry = er.async_get(hass)
    core = registry.async_get_or_create(
        "update", "hassio", "home_assistant_core_version_latest"
    )
    hass.states.async_set(
        core.entity_id, "on", {"installed_version": "1", "latest_version": "2"}
    )
    inventory = collect_update_inventory(hass, False, [], discover=True)
    assert core.entity_id not in {record.entity_id for record in inventory.records}
    assert (
        collect_update_inventory(hass, False, [], discover=False).status
        == "unsupported"
    )
    selected = collect_update_inventory(hass, False, [core.entity_id], discover=True)
    assert core.entity_id in {record.entity_id for record in selected.records}


async def test_local_count_and_coverage_remain_visible_when_v1_cannot_send_versions(
    hass, backend, entry
):
    from custom_components.ha_monitor_agent.const import DOMAIN

    registry = er.async_get(hass)
    sensor = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{backend.instance_id}_available_updates",
        config_entry=entry,
        disabled_by=None,
    )
    source(
        hass,
        "update.firmware",
        installed_version="20230913/v1.14.0",
        latest_version="20241011/v1.14.1",
        friendly_name=backend.token,
    )
    hass.config_entries.async_update_entry(entry, options={"enabled": False})
    assert await hass.config_entries.async_setup(entry.entry_id)
    monitor = entry.runtime_data.monitoring
    await monitor.async_refresh()
    await hass.async_block_till_done()
    state = hass.states.get(sensor.entity_id)
    assert state.state == "1" and state.attributes["collection_status"] == "ok"
    assert state.attributes["protocol_status"] == "unavailable"
    assert state.attributes["known_available"] == 1
    assert backend.token not in str(state.attributes)
    source(hass, "update.offline", state="unavailable")
    await monitor.async_refresh()
    await hass.async_block_till_done()
    state = hass.states.get(sensor.entity_id)
    assert state.state == "unavailable"
    diagnostic = await async_get_config_entry_diagnostics(hass, entry)
    summary = diagnostic["monitoring"]["updates"]
    assert summary["known_available"] == 1 and summary["unavailable"] == 1


@pytest.mark.parametrize(
    ("installed", "latest", "error"),
    [
        ("x" * 257, "2", "installed_version_not_representable"),
        (True, "2", "installed_version_not_representable"),
        ("1", None, "latest_version_not_representable"),
        ("1", "release candidate 2", "latest_version_not_representable"),
    ],
)
async def test_unrepresentable_metadata_never_turns_an_offered_update_into_zero(
    hass, installed, latest, error
):
    source(
        hass,
        "update.offered",
        installed_version=installed,
        latest_version=latest,
        supported_features=False,
    )
    inventory = collect_update_inventory(hass, False, [], discover=True)
    assert inventory.status == "ok" and len(inventory.offered) == 1
    assert inventory.offered[0].supports_install is None
    assert inventory.protocol_issues == {error: 1}
    assert inventory.as_protocol() == {"status": "unavailable", "items": []}
