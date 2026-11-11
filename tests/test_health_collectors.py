"""Health findings from actual HA registries and native backup manager events."""

from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

from homeassistant.components.backup import async_get_manager
from homeassistant.components.backup.const import DATA_MANAGER
from homeassistant.components.backup.manager import (
    BackupManager,
    CoreBackupReaderWriter,
    CreateBackupEvent,
    CreateBackupStage,
    CreateBackupState,
    IdleEvent,
    ReceiveBackupEvent,
    ReceiveBackupState,
)
from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant.common import MockConfigEntry

from custom_components.ha_monitor_agent.health_collectors import (
    BackupCollector,
    collect_config_entries,
    collect_repairs,
    collect_unavailable_entities,
    neutral_backup,
    source_key,
)


def repair(hass, issue_id="broken", severity=ir.IssueSeverity.WARNING):
    ir.async_create_issue(
        hass,
        "test",
        issue_id,
        is_fixable=False,
        severity=severity,
        translation_key="test",
        translation_placeholders={"secret": "private"},
    )


async def test_open_ignored_inactive_repairs_and_privacy(hass, entry):
    repair(hass)
    repair(hass, "ignored", ir.IssueSeverity.CRITICAL)
    ir.async_get(hass).async_ignore("test", "ignored", True)
    repair(hass, "resolved")
    ir.async_delete_issue(hass, "test", "resolved")
    repair(hass, "inactive")
    registry = ir.async_get(hass)
    registry.issues[("test", "inactive")] = replace(
        registry.issues[("test", "inactive")], active=False
    )
    assert collect_repairs(hass, entry.unique_id) == {
        "coverage": "complete",
        "scope": "all",
        "total": 2,
        "items": [
            {
                "key": "test:broken",
                "domain": "test",
                "name": None,
                "status": "open",
                "severity": "warning",
            },
            {
                "key": "test:ignored",
                "domain": "test",
                "name": None,
                "status": "ignored",
                "severity": "critical",
            },
        ],
    }


@pytest.mark.parametrize("state", list(ConfigEntryState))
async def test_each_native_config_entry_state_and_deliberately_disabled(
    hass, entry, state
):
    entry._async_set_state(hass, state, None)
    result = collect_config_entries(hass, entry.unique_id)
    assert result["coverage"] == "complete"
    assert result["total"] == int(state != ConfigEntryState.LOADED)
    if result["items"]:
        assert result["items"][0]["state"] == state.value
        assert result["items"][0]["name"] is None
    entry._async_set_state(hass, ConfigEntryState.NOT_LOADED, None)
    await hass.config_entries.async_set_disabled_by(
        entry.entry_id, ConfigEntryDisabler.USER
    )
    assert collect_config_entries(hass, entry.unique_id)["total"] == 0


async def test_entities_only_actual_unavailable_enabled_and_optional_device(
    hass, entry
):
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("test", "secret-mac")},
        name="private",
    )
    registry = er.async_get(hass)
    ids = []
    for name, state in (
        ("bad", "unavailable"),
        ("good", "on"),
        ("unknown", "unknown"),
        ("off", "off"),
        ("absent", None),
        ("disabled", "unavailable"),
    ):
        entity = registry.async_get_or_create(
            "sensor",
            "test",
            name,
            suggested_object_id=name,
            config_entry=entry,
            device_id=device.id,
        )
        ids.append(entity.entity_id)
        if state:
            hass.states.async_set(
                entity.entity_id,
                state,
                {"secret": "private", "friendly_name": "private"},
            )
        if name == "disabled":
            registry.async_update_entity(
                entity.entity_id, disabled_by=er.RegistryEntryDisabler.USER
            )
    result = collect_unavailable_entities(
        hass, entry.unique_id, all_entities=True, include_devices=True
    )
    assert result["coverage"] == "complete" and result["total"] == 1
    assert result["items"] == [
        {"entity_id": ids[0], "name": None, "device": {"id": device.id, "name": None}}
    ]
    assert collect_unavailable_entities(hass, entry.unique_id)["total"] == 0
    result = collect_unavailable_entities(
        hass, entry.unique_id, selected=[ids[0], ids[0], ids[2], "sensor.missing"]
    )
    assert result["scope"] == "selected" and result["total"] == 1
    assert result["items"][0]["device"] is None


@pytest.mark.parametrize(
    "label",
    [
        "",
        "x" * 121,
        "private\nlabel",
        "hidden\u200bformat",
        "private\ue000use",
        "line\u2028break",
    ],
)
async def test_invalid_approved_names_stay_null_without_losing_findings(
    hass, entry, label
):
    hass.config_entries.async_update_entry(entry, title=label)
    result = collect_config_entries(hass, entry.unique_id, include_names=True)
    assert result["coverage"] == "complete" and result["total"] == 1
    assert result["items"][0]["name"] is None


@pytest.mark.parametrize(
    "section, count, cap",
    [
        ("repairs", 101, 100),
        ("config_entries", 257, 256),
        ("unavailable_entities", 1001, 1000),
    ],
)
async def test_caps_with_known_totals_and_deterministic_subsets(
    hass, entry, section, count, cap
):
    for i in reversed(range(count)):
        if section == "repairs":
            repair(hass, f"issue_{i:04}")
        elif section == "config_entries":
            if i:
                MockConfigEntry(domain="test", entry_id=f"entry_{i:04}").add_to_hass(
                    hass
                )
        else:
            hass.states.async_set(f"sensor.bad_{i:04}", "unavailable")
    collector = {
        "repairs": collect_repairs,
        "config_entries": collect_config_entries,
        "unavailable_entities": lambda hass, id: collect_unavailable_entities(
            hass, id, all_entities=True
        ),
    }[section]
    result = collector(hass, entry.unique_id)
    assert result["coverage"] == "partial" and result["total"] == count
    assert len(result["items"]) == cap
    keys = [item.get("key", item.get("entity_id")) for item in result["items"]]
    assert keys == sorted(keys)
    assert collector(hass, entry.unique_id) == result


async def test_bad_record_isolated_and_instance_local_digest(hass, entry):
    repair(hass)
    repair(hass, "invalid")
    registry = ir.async_get(hass)
    registry.issues[("test", "invalid")] = replace(
        registry.issues[("test", "invalid")], severity=None
    )
    result = collect_repairs(hass, entry.unique_id)
    assert result["coverage"] == "partial" and result["total"] is None
    assert len(result["items"]) == 1
    assert source_key("valid:identity", "a") == "valid:identity"
    assert source_key("invalid / identity", "a") != source_key(
        "invalid / identity", "b"
    )
    assert len(source_key("ä" * 300, "a")) <= 128


@pytest.fixture
def manager(hass):
    # Real native object and its public dispatch/subscription API; no backups/files are created.
    manager = BackupManager(hass, CoreBackupReaderWriter(hass))
    hass.data[DATA_MANAGER] = manager
    manager.async_on_backup_event(IdleEvent())
    assert async_get_manager(hass) is manager
    return manager


def emit(manager, state, *, stage=None, reason=None):
    manager.async_on_backup_event(
        CreateBackupEvent(state=state, stage=stage, reason=reason)
    )


async def test_backup_no_source_or_unavailable_source(hass):
    collector = BackupCollector(hass)
    assert collector.collect(dt_util.utcnow()) == neutral_backup("unsupported")
    hass.config.components.add("backup")
    assert collector.collect(dt_util.utcnow()) == neutral_backup("unavailable")
    collector.stop()


async def test_backup_full_lifecycle_stages_success_failure_and_restart(
    hass, manager, freezer
):
    collector = BackupCollector(hass)
    assert collector.collect(dt_util.utcnow()) == neutral_backup()
    freezer.tick(1)
    emit(manager, CreateBackupState.IN_PROGRESS)
    start = dt_util.utcnow().isoformat()
    freezer.tick(3)
    emit(manager, CreateBackupState.IN_PROGRESS, stage=CreateBackupStage.HOME_ASSISTANT)
    assert collector.collect(dt_util.utcnow())["last_attempt_at"] == start
    assert collector.collect(dt_util.utcnow())["status"] == "running"
    freezer.tick(10)
    emit(manager, CreateBackupState.COMPLETED)
    completed = dt_util.utcnow().isoformat()
    manager.async_on_backup_event(IdleEvent())
    assert collector.collect(dt_util.utcnow()) == {
        "coverage": "complete",
        "status": "ok",
        "last_attempt_at": start,
        "last_success_at": completed,
        "error_code": None,
    }
    freezer.tick(2)
    emit(manager, CreateBackupState.IN_PROGRESS)
    freezer.tick(5)
    emit(manager, CreateBackupState.FAILED, reason="upload_failed private text")
    data = collector.collect(dt_util.utcnow())
    assert (
        data["status"] == "failed"
        and data["error_code"] == "unknown"
        and data["last_success_at"] == completed
    )
    collector.stop()
    reloaded = BackupCollector(hass)
    assert reloaded.collect(dt_util.utcnow()) == neutral_backup()
    reloaded.stop()


@pytest.mark.parametrize(
    "reason, code",
    [
        ("backup_agent_unreachable", "connection"),
        ("upload_failed", "unknown"),
        ("backup_reader_writer_error", "unknown"),
        (None, "unknown"),
        ("password=private", "unknown"),
    ],
)
async def test_backup_safe_native_error_classification(hass, manager, reason, code):
    collector = BackupCollector(hass)
    collector.attach()
    emit(manager, CreateBackupState.IN_PROGRESS)
    emit(manager, CreateBackupState.FAILED, reason=reason)
    assert collector.collect(dt_util.utcnow())["error_code"] == code
    collector.stop()


async def test_backup_midrun_historical_automatic_dates_and_other_operations_are_unknown(
    hass, manager
):
    manager.config.data.last_completed_automatic_backup = dt_util.utcnow() - timedelta(
        days=1
    )
    manager.config.data.last_attempted_automatic_backup = dt_util.utcnow() - timedelta(
        hours=1
    )
    emit(manager, CreateBackupState.IN_PROGRESS)
    collector = BackupCollector(hass)
    collector.attach()
    emit(
        manager, CreateBackupState.IN_PROGRESS, stage=CreateBackupStage.UPLOAD_TO_AGENTS
    )
    emit(manager, CreateBackupState.COMPLETED)
    assert collector.collect(dt_util.utcnow()) == neutral_backup()
    manager.async_on_backup_event(
        ReceiveBackupEvent(state=ReceiveBackupState.COMPLETED, stage=None, reason=None)
    )
    assert collector.collect(dt_util.utcnow()) == neutral_backup()
    collector.stop()


async def test_backup_clock_rollback_replacement_and_callback_isolation(
    hass, manager, freezer
):
    collector = BackupCollector(hass)
    collector.attach()
    emit(manager, CreateBackupState.IN_PROGRESS)
    freezer.tick(5)
    emit(manager, CreateBackupState.COMPLETED)
    freezer.tick(-10)
    assert collector.collect(dt_util.utcnow()) == neutral_backup()
    with patch.object(collector, "_observe", side_effect=RuntimeError("private")):
        emit(manager, CreateBackupState.IN_PROGRESS)
    assert collector.collect(dt_util.utcnow()) == neutral_backup("unavailable")
    replacement = BackupManager(hass, CoreBackupReaderWriter(hass))
    hass.data[DATA_MANAGER] = replacement
    assert collector.collect(dt_util.utcnow()) == neutral_backup()
    emit(manager, CreateBackupState.IN_PROGRESS)
    assert collector.collect(dt_util.utcnow()) == neutral_backup()
    collector.stop()


async def test_native_manager_public_creation_failure_is_observed(hass, manager):
    from homeassistant.components.backup.models import BackupReaderWriterError

    collector = BackupCollector(hass)
    collector.attach()
    with patch.object(
        manager,
        "_async_create_backup",
        side_effect=BackupReaderWriterError("private path"),
    ):
        with pytest.raises(BackupReaderWriterError):
            await manager.async_create_backup(
                agent_ids=[],
                extra_metadata={},
                include_addons=None,
                include_all_addons=False,
                include_database=False,
                include_folders=None,
                include_homeassistant=True,
                name=None,
                password=None,
            )
    data = collector.collect(dt_util.utcnow())
    assert data["status"] == "failed" and data["error_code"] == "unknown"
    assert data["last_attempt_at"] is not None and data["last_success_at"] is None
    collector.stop()


async def test_backup_api_shape_changes_are_unsupported(hass, manager):
    collector = BackupCollector(hass)
    with patch.object(manager, "async_subscribe_events", None):
        assert collector.collect(dt_util.utcnow()) == neutral_backup("unsupported")
    collector.stop()
