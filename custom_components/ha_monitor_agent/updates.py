"""Read all configured update sources, then project onto the frozen v1 contract.

Availability comes from HA's update state, never from supported install actions.
The immutable local inventory retains metadata which Snapshot v1 cannot express.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import re
from typing import Any

from homeassistant.components.update import UpdateEntityFeature
from homeassistant.core import CoreState, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

MAX_UPDATE_SOURCES = 4096
_VERSION_V1 = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}")
_KEY_V1 = re.compile(r"[a-z0-9][a-z0-9_.-]{0,127}")
_SYSTEM_PRODUCTS = {
    "home_assistant_core_version_latest": "core",
    "home_assistant_os_version_latest": "os",
    "home_assistant_supervisor_version_latest": "supervisor",
}


def _text(value: Any) -> str | None:
    """Preserve bounded raw metadata locally; never log arbitrary source text."""
    return value if isinstance(value, str) and 0 < len(value) <= 256 else None


@dataclass(frozen=True, slots=True)
class UpdateObservation:
    """Allowlisted local metadata, never exported as arbitrary HA attributes."""

    entity_id: str
    kind: str
    category: str
    source: str | None
    device_id: str | None
    device_name: str | None
    device_manufacturer: str | None
    device_model: str | None
    name: str | None
    availability: str
    installed_version: str | None
    latest_version: str | None
    skipped_version: str | None
    supports_install: bool | None
    in_progress: bool | None
    disabled_by: str | None
    explicitly_selected: bool
    metadata_issues: tuple[str, ...]

    @property
    def offered(self) -> bool:
        return self.availability in {"available", "skipped"}

    @classmethod
    def failed(cls, entity_id: str, selected: bool) -> UpdateObservation:
        """Retain a failed source without propagating exception text or stale data."""
        return cls(
            entity_id=entity_id,
            kind="other",
            category="other",
            source=None,
            device_id=None,
            device_name=None,
            device_manufacturer=None,
            device_model=None,
            name=None,
            availability="unavailable",
            installed_version=None,
            latest_version=None,
            skipped_version=None,
            supports_install=None,
            in_progress=None,
            disabled_by=None,
            explicitly_selected=selected,
            metadata_issues=("source_collection_failed",),
        )


@dataclass(frozen=True, slots=True)
class UpdateInventory:
    """All observations survive locally even if v1 cannot send a complete list."""

    status: str
    records: tuple[UpdateObservation, ...] = ()
    collection_issues: tuple[str, ...] = ()

    @property
    def offered(self) -> tuple[UpdateObservation, ...]:
        return tuple(record for record in self.records if record.offered)

    @property
    def protocol_issues(self) -> dict[str, int]:
        """Only fixed error codes/counts; safe for diagnostics and entity attributes."""
        reasons = Counter(self.collection_issues)
        for record in self.records:
            if record.availability in {"unknown", "unavailable"}:
                reasons["source_unavailable"] += 1
                if "source_collection_failed" in record.metadata_issues:
                    reasons["source_collection_failed"] += 1
            elif record.availability == "disabled" and record.explicitly_selected:
                reasons["selected_source_disabled"] += 1
        if len(self.offered) > 100:
            reasons["item_limit"] += 1
        for record in self.offered:
            reasons.update(record.metadata_issues)
            if not _KEY_V1.fullmatch(record.entity_id):
                reasons["key_not_representable"] += 1
            if record.latest_version is None or not _VERSION_V1.fullmatch(
                record.latest_version
            ):
                reasons["latest_version_not_representable"] += 1
            if record.installed_version is not None and not _VERSION_V1.fullmatch(
                record.installed_version
            ):
                reasons["installed_version_not_representable"] += 1
        return dict(reasons)

    def as_protocol(self) -> dict[str, Any]:
        """Exactly Snapshot v1: never silently drop records or rewrite versions."""
        status = self.status
        if status == "ok" and self.protocol_issues:
            status = "unavailable"
        items = []
        if status == "ok":
            for record in self.offered:
                items.append(
                    {
                        "key": record.entity_id,
                        "kind": record.kind,
                        "name": {
                            "core": "Home Assistant Core",
                            "os": "Home Assistant OS",
                        }.get(record.kind, record.entity_id[:120]),
                        "installed_version": record.installed_version,
                        "available_version": record.latest_version,
                    }
                )
            items.sort(
                key=lambda item: (
                    {"core": 0, "os": 1, "other": 2}[item["kind"]],
                    item["key"],
                )
            )
        return {"status": status, "items": items}

    def summary(self) -> dict[str, Any]:
        """Expose coverage and protocol limitations without device/source metadata."""
        states = Counter(record.availability for record in self.records)
        return {
            "collection_status": self.status,
            "sources": len(self.records),
            "known_available": len(self.offered),
            "pending": states["available"],
            "skipped": states["skipped"],
            "disabled": states["disabled"],
            "unavailable": states["unknown"] + states["unavailable"],
            "failed": sum(
                "source_collection_failed" in record.metadata_issues
                for record in self.records
            ),
            "not_installable": sum(
                record.supports_install is False for record in self.offered
            ),
            "installability_unknown": sum(
                record.supports_install is None for record in self.offered
            ),
            "protocol_status": self.as_protocol()["status"],
            "protocol_issues": self.protocol_issues,
        }


def _observe(hass: HomeAssistant, entity_id: str, selected: bool) -> UpdateObservation:
    registry = er.async_get(hass).async_get(entity_id)
    source = registry.platform if registry else None
    product = (
        _SYSTEM_PRODUCTS.get(registry.unique_id)
        if registry and source == "hassio"
        else None
    )
    state = hass.states.get(entity_id)
    disabled = str(registry.disabled_by) if registry and registry.disabled_by else None
    # Disabled entries may have a stale state during unload. Never trust that state.
    attributes = state.attributes if state and not disabled else {}
    installed = _text(attributes.get("installed_version"))
    latest = _text(attributes.get("latest_version"))
    skipped = _text(attributes.get("skipped_version"))
    if disabled:
        availability = "disabled"
    elif state is None or state.state == "unavailable":
        availability = "unavailable"
    elif state.state == "on":
        availability = "available"
    elif state.state == "off":
        availability = (
            "skipped"
            if skipped
            and skipped == latest
            and installed is not None
            and installed != latest
            else "up_to_date"
        )
    else:
        availability = "unknown"
    features = attributes.get("supported_features")
    supports_install = (
        bool(features & UpdateEntityFeature.INSTALL)
        if isinstance(features, int)
        and not isinstance(features, bool)
        and features >= 0
        else None
    )
    progress = attributes.get("in_progress")
    device = (
        dr.async_get(hass).async_get(registry.device_id)
        if registry and registry.device_id
        else None
    )
    category = product or {"hassio": "addon", "hacs": "hacs"}.get(source)
    if category is None:
        device_class = attributes.get("device_class")
        if device_class is None and registry:
            device_class = registry.device_class or registry.original_device_class
        category = "firmware" if device_class == "firmware" else "other"
    return UpdateObservation(
        entity_id=entity_id,
        kind=product if product in {"core", "os"} else "other",
        category=category,
        source=source,
        device_id=registry.device_id if registry else None,
        device_name=_text(device.name_by_user or device.name) if device else None,
        device_manufacturer=_text(device.manufacturer) if device else None,
        device_model=_text(device.model) if device else None,
        name=_text(attributes.get("friendly_name"))
        or (_text(registry.name or registry.original_name) if registry else None),
        availability=availability,
        installed_version=installed,
        latest_version=latest,
        skipped_version=skipped,
        supports_install=supports_install,
        in_progress=progress if type(progress) is bool else None,
        disabled_by=disabled,
        explicitly_selected=selected,
        metadata_issues=("installed_version_not_representable",)
        if attributes.get("installed_version") is not None and installed is None
        else (),
    )


@callback
def collect_update_inventory(
    hass: HomeAssistant,
    system_updates: bool,
    selected: list[str],
    *,
    discover: bool = False,
) -> UpdateInventory:
    """Discover enabled/hidden states plus registry coverage without enabling entities.

    Disabled auto-discovered entries are reported locally and excluded from coverage;
    explicitly selected disabled entries fail coverage. No polling/install actions.
    """
    if hass.state is not CoreState.running:
        return UpdateInventory("unknown")
    if len(selected) > MAX_UPDATE_SOURCES:
        return UpdateInventory("unavailable", collection_issues=("source_limit",))
    registry = er.async_get(hass)
    sources = set(selected)
    core_os = {
        entry.entity_id
        for entry in registry.entities.values()
        if entry.domain == "update"
        and entry.platform == "hassio"
        and _SYSTEM_PRODUCTS.get(entry.unique_id) in {"core", "os"}
    }
    if discover:
        for entity_id in (state.entity_id for state in hass.states.async_all("update")):
            sources.add(entity_id)
            if len(sources) > MAX_UPDATE_SOURCES:
                return UpdateInventory(
                    "unavailable", collection_issues=("source_limit",)
                )
        for entry in registry.entities.values():
            if entry.domain == "update":
                sources.add(entry.entity_id)
                if len(sources) > MAX_UPDATE_SOURCES:
                    return UpdateInventory(
                        "unavailable", collection_issues=("source_limit",)
                    )
        if not system_updates:
            sources.difference_update(core_os - set(selected))
    issues = []
    if system_updates and "hassio" in hass.config.components:
        from homeassistant.components.hassio import get_info

        try:
            expected = (
                {"core", "os"} if get_info(hass).get("hassos") is not None else {"core"}
            )
        except Exception:
            issues.append("supervisor_unavailable")
        else:
            present = {
                _SYSTEM_PRODUCTS[registry.entities[entity_id].unique_id]
                for entity_id in core_os
            }
            if not expected.issubset(present):
                issues.append("system_source_missing")
        sources.update(core_os)
    if len(sources) > MAX_UPDATE_SOURCES:
        return UpdateInventory("unavailable", collection_issues=("source_limit",))
    if any(
        not isinstance(entity_id, str) or not entity_id.startswith("update.")
        for entity_id in sources
    ):
        return UpdateInventory("unavailable", collection_issues=("invalid_source",))
    explicit = set(selected) | (core_os if system_updates else set())
    observations = []
    for entity_id in sorted(sources):
        try:
            record = _observe(hass, entity_id, entity_id in explicit)
        except Exception:
            # A bad source must not discard successful observations of its peers.
            # Snapshot v1 still requires a non-ok *wire* collection if incomplete.
            record = UpdateObservation.failed(entity_id, entity_id in explicit)
        observations.append(record)
    records = tuple(observations)
    if issues or any(
        record.availability in {"unknown", "unavailable"}
        or record.availability == "disabled"
        and record.explicitly_selected
        for record in records
    ):
        status = "unavailable"
    elif any(record.availability != "disabled" for record in records):
        status = "ok"
    else:
        status = "unsupported"
    return UpdateInventory(status, records, tuple(issues))


@callback
def collect_updates(
    hass: HomeAssistant, system_updates: bool, selected: list[str]
) -> dict[str, Any]:
    """Project an explicitly selected source set for Snapshot v1 callers."""
    return collect_update_inventory(hass, system_updates, selected).as_protocol()
