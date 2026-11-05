"""Explicit allowlist of local diagnostics; no credentials or receiver URL."""

from typing import Any

from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant

from . import AgentConfigEntry
from .const import CONF_ENABLED, CONF_HEARTBEAT_INTERVAL, VERSION


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: AgentConfigEntry
) -> dict[str, Any]:
    sender = entry.runtime_data
    return {
        "integration_version": VERSION,
        "ha_version": HA_VERSION,
        "enabled": entry.options.get(CONF_ENABLED, True),
        "heartbeat_interval_seconds": entry.data[CONF_HEARTBEAT_INTERVAL],
        "status": sender.status,
        "last_error": sender.last_error,
        "pending_events": sender.pending_events,
        "dropped_events": sender.dropped_events,
        "consecutive_failures": sender.failures,
        "last_contact": sender.last_contact,
        "last_heartbeat": sender.last_heartbeat,
        "next_attempt": sender.next_attempt,
    }
