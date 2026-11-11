"""Config flow tests through HA's flow manager and a verified TLS backend."""

import asyncio
from unittest.mock import patch
from uuid import uuid4

from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_RECONFIGURE, SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType
import pytest

from custom_components.ha_monitor_agent.client import TransportError
from custom_components.ha_monitor_agent.const import DOMAIN


async def test_pair_and_setup(hass, backend):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"server_url": backend.url + "/", "pairing_code": backend.code},
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == backend.instance_id
    assert "pairing_code" not in result["data"]
    assert result["data"]["agent_token"] == backend.token
    assert result["data"]["server_url"] == backend.url
    assert len(backend.stored) == 1
    assert result["result"].runtime_data.status == "online"


@pytest.mark.parametrize(
    ("url", "code", "error"),
    [
        ("http://localhost", "valid", "invalid_url"),
        ("https://user:password@example.org", "valid", "invalid_url"),
        ("backend", "valid", "invalid_url"),
        ("valid", "wrong", "invalid_code"),
        ("valid", "0" * 32, "invalid_auth"),
    ],
)
async def test_flow_errors(hass, backend, url, code, error):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "server_url": backend.url if url == "valid" else url,
            "pairing_code": backend.code if code == "valid" else code,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_pair_once_no_automatic_retry(hass, backend):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with patch(
        "custom_components.ha_monitor_agent.client.AgentClient._async_post",
        side_effect=TransportError("rate_limited", 120),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"server_url": backend.url, "pairing_code": backend.code}
        )
    assert result["errors"] == {"base": "rate_limited"}
    assert result["description_placeholders"]["retry_after"] == "120"
    assert backend.pair_calls == 0


async def test_duplicate_entry(hass, backend, entry):
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
        data={"server_url": backend.url, "pairing_code": backend.code},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


@pytest.mark.parametrize("source", [SOURCE_REAUTH, SOURCE_RECONFIGURE])
async def test_update_existing_entry(hass, backend, entry, source):
    old_token = entry.data["agent_token"]
    backend.new_code()
    with patch.object(hass.config_entries, "async_reload", return_value=True) as reload:
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": source, "entry_id": entry.entry_id},
            data=dict(entry.data) if source == SOURCE_REAUTH else None,
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"server_url": backend.url, "pairing_code": backend.code}
        )
        await hass.async_block_till_done()
    assert result["reason"] == (
        "reauth_successful" if source == SOURCE_REAUTH else "reconfigure_successful"
    )
    assert entry.data["agent_token"] == backend.token != old_token
    assert "pairing_code" not in entry.data
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
    reload.assert_awaited_once_with(entry.entry_id)


@pytest.mark.parametrize("source", [SOURCE_REAUTH, SOURCE_RECONFIGURE])
async def test_identity_mismatch(hass, backend, entry, source):
    backend.instance_id = str(uuid4())
    old_data = dict(entry.data)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": source, "entry_id": entry.entry_id},
        data=old_data if source == SOURCE_REAUTH else None,
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"server_url": backend.url, "pairing_code": backend.code}
    )
    assert result["reason"] == "unique_id_mismatch"
    assert dict(entry.data) == old_data


async def test_options_pause_reload(hass, backend, entry):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"enabled": False}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["enabled"] is False
    assert entry.runtime_data.status == "paused"
    assert entry.runtime_data._task is None
    assert len(backend.requests) == 1


async def test_default_options_enable_v2_all_source_transmission(hass, backend, entry):
    hass.config_entries.async_update_entry(entry, options={"discover_updates": True})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"enabled": False}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["discover_updates"] is True
    assert entry.options["export_all_updates"] is True
    assert "monitoring_schema_version" not in entry.options


@pytest.mark.parametrize("legacy_version", [1, 2])
async def test_old_protocol_option_removed_without_losing_settings_or_pairing(
    hass, backend, entry, legacy_version
):
    original_data = dict(entry.data)
    options = {
        "monitoring_enabled": True,
        "enabled": True,
        "export_metrics": ["cpu_percent", "memory_percent", "storage_percent"],
        "export_all_updates": False,
        "update_entities": ["update.selected"],
        "monitoring_schema_version": legacy_version,
    }
    hass.config_entries.async_update_entry(entry, options=options)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await asyncio.wait_for(backend.monitoring_started.wait(), 2)
    async with entry.runtime_data.monitoring.uploader._send_lock:
        pass
    assert dict(entry.data) == original_data and backend.pair_calls == 0
    assert dict(entry.options) == {
        key: value
        for key, value in options.items()
        if key != "monitoring_schema_version"
    }
    assert backend.monitoring_requests[-1][0]["schema_version"] == 2
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert "monitoring_schema_version" not in {
        str(key) for key in result["data_schema"].schema
    }
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_health_options_explicit_consent_and_source_selection(
    hass, backend, entry
):
    original_data = dict(entry.data)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "enabled": False,
            "health_enabled": True,
            "health_sections": ["repairs", "unavailable_entities"],
            "health_all_entities": False,
            "health_entities": ["sensor.approved"],
            "health_devices": True,
            "health_names": True,
        },
    )
    assert result["type"] == "create_entry"
    assert entry.options["health_enabled"] is True
    assert entry.options["health_sections"] == ["repairs", "unavailable_entities"]
    assert entry.options["health_entities"] == ["sensor.approved"]
    assert entry.options["health_all_entities"] is False
    assert entry.options["health_devices"] is True
    assert entry.options["health_names"] is True
    assert entry.options["monitoring_enabled"] is False
    assert dict(entry.data) == original_data
