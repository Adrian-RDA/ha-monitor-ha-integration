"""Explicit allowlist of local diagnostics; no credentials or receiver URL."""

from typing import Any

from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant

from . import AgentConfigEntry
from .const import (
    CONF_ENABLED,
    CONF_EXPORT_METRICS,
    CONF_HEARTBEAT_INTERVAL,
    METRIC_KEYS,
    VERSION,
)


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: AgentConfigEntry
) -> dict[str, Any]:
    sender = entry.runtime_data
    result = {
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
    if sender.monitoring is not None:
        monitor = sender.monitoring
        uploader = monitor.uploader
        result["monitoring"] = {
            "enabled": uploader.enabled,
            "collection_success": monitor.last_update_success,
            "last_observed_at": monitor.data["observed_at"] if monitor.data else None,
            "metrics": {
                key: {
                    "status": monitor.data["metrics"][key]["status"]
                    if monitor.data
                    else "unknown",
                    "exported": key
                    in entry.options.get(CONF_EXPORT_METRICS, METRIC_KEYS),
                }
                for key in METRIC_KEYS
            },
            "status": uploader.status,
            "last_error": uploader.last_error,
            "validation_fields": list(uploader.validation_fields),
            "pending_snapshots": uploader.pending_snapshots,
            "dropped_snapshots": uploader.dropped_snapshots,
            "last_upload": uploader.last_upload,
            "updates": sender.monitoring.update_summary(),
        }
    if sender.health is not None:
        health = sender.health
        result["health"] = {
            "enabled": health.enabled,
            "status": health.uploader.status,
            "last_error": health.uploader.last_error,
            "last_upload": health.uploader.last_upload,
            "pending_reports": health.uploader.pending_snapshots,
            "dropped_reports": health.uploader.dropped_snapshots,
            "last_observed_at": health.data["observed_at"] if health.data else None,
            "backup_history": {
                "interpretation": "historical_only",
                "last_confirmed": health.backup_history.last_confirmed,
            },
            "coverage": {
                key: health.data[key]["coverage"] if health.data else "unknown"
                for key in (
                    "repairs",
                    "config_entries",
                    "unavailable_entities",
                    "backup",
                )
            },
        }
    return result
