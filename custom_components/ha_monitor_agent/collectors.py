"""Small local collectors. No backend access and no System Monitor dependency."""

from __future__ import annotations

import math
import re
from typing import Any

from homeassistant.core import CoreState, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
import psutil

from .const import INVENTORY_KEYS, METRIC_KEYS


def observation(status: str, value: int | float | None = None) -> dict[str, Any]:
    """An explicit availability value; missing observations never become zero."""
    return {"status": status, "value": value}


def metric(
    status: str, scope: str | None, value: float | None = None
) -> dict[str, Any]:
    return {**observation(status, value), "scope": scope}


def percentage(numerator: float, denominator: float, scope: str) -> dict[str, Any]:
    """Reject invalid readings rather than clamping or inventing a denominator."""
    if denominator <= 0 or not all(map(math.isfinite, (numerator, denominator))):
        return metric("unavailable", scope)
    value = numerator / denominator * 100
    if not 0 <= value <= 100:
        return metric("unavailable", scope)
    return metric("ok", scope, round(value, 3))


class ResourceCollector:
    """psutil OS-view CPU/RAM and one HA data filesystem, entirely in an executor.

    CPU counters describe the visible OS, not HA's process or cgroup consumption.
    Keep our own baseline: psutil.cpu_percent(None) has per-thread baselines and
    HA's executor may choose a different thread for the next sample.
    """

    def __init__(self, data_path: str) -> None:
        self._data_path = data_path
        self._cpu_previous: tuple[float, float] | None = None

    def collect(self) -> dict[str, Any]:
        result = {}
        for key, collect, scope in (
            ("cpu_percent", self._cpu, "host"),
            ("memory_percent", self._memory, "host"),
            ("storage_percent", self._storage, "data"),
        ):
            try:
                result[key] = collect()
            except AttributeError, NotImplementedError:
                result[key] = metric("unsupported", scope)
                if key == "cpu_percent":
                    self._cpu_previous = None
            except OSError, psutil.Error, ValueError, OverflowError:
                result[key] = metric("unavailable", scope)
                if key == "cpu_percent":
                    self._cpu_previous = None
        return result

    def _cpu(self) -> dict[str, Any]:
        times = psutil.cpu_times()
        # Linux guest time is already included in user/nice. I/O wait is idle.
        total = (
            sum(times) - getattr(times, "guest", 0) - getattr(times, "guest_nice", 0)
        )
        busy = total - times.idle - getattr(times, "iowait", 0)
        previous, self._cpu_previous = self._cpu_previous, (total, busy)
        if previous is None or total <= previous[0] or busy < previous[1]:
            return metric("unknown", "host")
        return percentage(busy - previous[1], total - previous[0], "host")

    def _memory(self) -> dict[str, Any]:
        memory = psutil.virtual_memory()
        return percentage(memory.total - memory.available, memory.total, "host")

    def _storage(self) -> dict[str, Any]:
        disk = psutil.disk_usage(self._data_path)
        # disk.percent can use a different denominator (reserved blocks).
        return percentage(disk.used, disk.total, "data")


@callback
def collect_inventory(hass: HomeAssistant) -> dict[str, Any]:
    """Count the contract's registries, states and successfully loaded domains."""
    if hass.state is not CoreState.running:
        return {key: observation("unknown") for key in INVENTORY_KEYS}
    counts = {
        "entities": len(er.async_get(hass).entities),
        "devices": len(dr.async_get(hass).devices),
        "automations": len(hass.states.async_all("automation")),
        "scenes": len(hass.states.async_all("scene")),
        # config.components also contains platform names such as sensor.mqtt.
        "integrations": len({name.split(".")[0] for name in hass.config.components}),
    }
    return {
        key: observation("ok", value)
        if value <= 1_000_000
        else observation("unavailable")
        for key, value in counts.items()
    }


@callback
def collect_updates(
    hass: HomeAssistant, system_updates: bool, selected: list[str]
) -> dict[str, Any]:
    """Only configured sources; any incomplete source invalidates the collection.

    Core/OS identity uses HA's stable Supervisor unique IDs, never display names.
    Only the five protocol fields are constructed; arbitrary attributes stay local.
    """

    def missing(status: str) -> dict[str, Any]:
        return {"status": status, "items": []}

    if hass.state is not CoreState.running:
        return missing("unknown")
    registry = er.async_get(hass)
    kinds = {}
    for kind in ("core", "os"):
        entity_id = registry.async_get_entity_id(
            "update", "hassio", f"home_assistant_{kind}_version_latest"
        )
        if entity_id:
            kinds[entity_id] = kind
    sources = set(selected)
    if system_updates and "hassio" in hass.config.components:
        # These public helpers read the Supervisor integration's existing cache.
        from homeassistant.components.hassio import get_info

        try:
            info = get_info(hass)
        except Exception:
            return missing("unavailable")
        expected = {"core", "os"} if info.get("hassos") is not None else {"core"}
        if not expected.issubset(kinds.values()):
            return missing("unavailable")
        sources.update(kinds)
    if not sources:
        return missing("unsupported")
    if len(sources) > 100:
        return missing("unavailable")
    items = []
    for entity_id in sorted(sources):
        if not re.fullmatch(
            r"[a-z0-9][a-z0-9_.-]{0,127}", entity_id
        ) or not entity_id.startswith("update."):
            return missing("unavailable")
        state = hass.states.get(entity_id)
        if state is None or state.state not in {"on", "off"}:
            return missing("unavailable")
        # Skipped updates are still available: UpdateEntity reports them as off.
        available = state.attributes.get("latest_version")
        installed = state.attributes.get("installed_version")
        if not isinstance(available, str) or not re.fullmatch(
            r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}", available
        ):
            return missing("unavailable")
        if installed is not None and (
            not isinstance(installed, str)
            or not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}", installed)
        ):
            return missing("unavailable")
        if state.state == "off" and (
            not state.attributes.get("skipped_version") or installed == available
        ):
            continue
        kind = kinds.get(entity_id, "other")
        # Fixed product names and selected source IDs avoid exporting user names.
        name = {"core": "Home Assistant Core", "os": "Home Assistant OS"}.get(
            kind, entity_id[:120]
        )
        items.append(
            {
                "key": entity_id,
                "kind": kind,
                "name": name,
                "installed_version": installed,
                "available_version": available,
            }
        )
    items.sort(
        key=lambda item: ({"core": 0, "os": 1, "other": 2}[item["kind"]], item["key"])
    )
    return {"status": "ok", "items": items}


def unavailable_metrics() -> dict[str, Any]:
    return {key: metric("unavailable", None) for key in METRIC_KEYS}
