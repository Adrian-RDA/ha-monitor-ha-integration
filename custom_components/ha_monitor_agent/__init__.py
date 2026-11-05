"""Home Assistant lifecycle for the HA Monitor outbound agent."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .client import AgentClient
from .const import CONF_SERVER_URL, DOMAIN
from .sender import HeartbeatSender

type AgentConfigEntry = ConfigEntry[HeartbeatSender]

PLATFORMS = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: AgentConfigEntry) -> bool:
    """Create an isolated sender using HA's shared, verifying HTTP session."""
    sender = HeartbeatSender(
        hass,
        entry,
        AgentClient(async_get_clientsession(hass), entry.data[CONF_SERVER_URL]),
    )
    entry.runtime_data = sender
    await sender.async_setup()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    sender.async_start()
    # Boolean lifecycle results also support HA 2026.10 (HACS).
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AgentConfigEntry) -> bool:
    """Unload entities and cancel all pending network activity."""
    if unloaded := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.async_stop()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: AgentConfigEntry) -> None:
    """Remove stale local Repairs; backend revocation remains an operator action."""
    from homeassistant.helpers import issue_registry as ir

    ir.async_delete_issue(hass, DOMAIN, f"upload_blocked_{entry.entry_id}")
