"""Local diagnostic entities; these entities are never exported."""

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import AgentConfigEntry
from .const import CONF_INSTANCE_ID, DOMAIN, INVENTORY_KEYS, METRIC_KEYS, VERSION
from .monitoring import MonitoringCoordinator
from .sender import STATUSES

SENSORS = (
    SensorEntityDescription(
        key="status",
        translation_key="status",
        device_class=SensorDeviceClass.ENUM,
        options=STATUSES,
    ),
    SensorEntityDescription(
        key="last_heartbeat",
        translation_key="last_heartbeat",
        device_class=SensorDeviceClass.TIMESTAMP,
    ),
    SensorEntityDescription(key="pending_events", translation_key="pending_events"),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AgentConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(AgentSensor(entry, description) for description in SENSORS)
    async_add_entities(
        MonitoringSensor(entry, key)
        for key in (*METRIC_KEYS, *INVENTORY_KEYS, "available_updates")
    )


class AgentSensor(SensorEntity):
    """A listener-backed diagnostic value, without polling or I/O."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, entry: AgentConfigEntry, description: SensorEntityDescription
    ) -> None:
        self.entity_description = description
        self._sender = entry.runtime_data
        instance_id = entry.data[CONF_INSTANCE_ID]
        self._attr_unique_id = f"{instance_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, instance_id)},
            name=entry.title,
            manufacturer="HA Monitor",
            model="Outbound agent",
            sw_version=VERSION,
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._sender.async_add_listener(self.async_write_ha_state))

    @property
    def native_value(self) -> str | int | datetime | None:
        return getattr(self._sender, self.entity_description.key)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.key != "status":
            return None
        return {
            "last_error": self._sender.last_error,
            "next_attempt": self._sender.next_attempt,
            "consecutive_failures": self._sender.failures,
            "dropped_events": self._sender.dropped_events,
        }


class MonitoringSensor(CoordinatorEntity[MonitoringCoordinator], SensorEntity):
    """Optional local values from the same collection used for snapshots."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, entry: AgentConfigEntry, key: str) -> None:
        super().__init__(entry.runtime_data.monitoring)
        self._key = key
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry.data[CONF_INSTANCE_ID]}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.data[CONF_INSTANCE_ID])}
        )
        if key in METRIC_KEYS:
            self._attr_native_unit_of_measurement = PERCENTAGE

    @property
    def _observation(self) -> dict[str, Any]:
        data = self.coordinator.data
        if not data:
            return {"status": "unknown", "value": None}
        if self._key == "available_updates":
            updates = self.coordinator.update_inventory
            return {
                "status": updates.status,
                "value": len(updates.offered) if updates.status == "ok" else None,
            }
        section = "metrics" if self._key in METRIC_KEYS else "inventory"
        return data[section][self._key]

    @property
    def available(self) -> bool:
        return super().available and self._observation["status"] == "ok"

    @property
    def native_value(self) -> float | int | None:
        return self._observation["value"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        result = {"collection_status": self._observation["status"]}
        if self._key in METRIC_KEYS:
            result["scope"] = self._observation.get("scope")
        elif self._key == "available_updates":
            result.update(self.coordinator.update_summary())
        return result
