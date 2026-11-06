"""Small local collectors. No backend access and no System Monitor dependency."""

from __future__ import annotations

import math
from typing import Any

from homeassistant.core import CoreState, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
import psutil

from .const import INVENTORY_KEYS, METRIC_KEYS
from .updates import collect_updates as collect_updates


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


def unavailable_metrics() -> dict[str, Any]:
    return {key: metric("unavailable", None) for key in METRIC_KEYS}
