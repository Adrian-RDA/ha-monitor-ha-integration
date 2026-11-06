"""Enrollment, reauthentication, receiver migration and privacy options."""

from collections.abc import Mapping
from dataclasses import asdict
from typing import Any

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
import probatio

from .client import (
    AgentClient,
    AuthenticationError,
    ProtocolError,
    TransportError,
    normalize_server_url,
)
from .const import (
    CONF_DISCOVER_UPDATES,
    CONF_ENABLED,
    CONF_EXPORT_ALL_UPDATES,
    CONF_EXPORT_METRICS,
    CONF_INSTANCE_ID,
    CONF_MONITORING_ENABLED,
    CONF_PAIRING_CODE,
    CONF_SERVER_URL,
    CONF_SYSTEM_UPDATES,
    CONF_UPDATE_ENTITIES,
    DOMAIN,
    METRIC_KEYS,
)


def _pair_schema(server_url: str = "") -> probatio.Schema:
    return probatio.Schema(
        {
            probatio.Required(CONF_SERVER_URL, default=server_url): TextSelector(
                TextSelectorConfig(type=TextSelectorType.URL)
            ),
            probatio.Required(probatio.Secret(CONF_PAIRING_CODE)): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)
            ),
        }
    )


class AgentConfigFlow(ConfigFlow, domain=DOMAIN):
    """Store only validated enrollment data; never store the one-time code."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> AgentOptionsFlow:
        return AgentOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self._async_pair_step("user", user_input)

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self._async_pair_step("reauth_confirm", user_input)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self._async_pair_step("reconfigure", user_input)

    async def _async_pair_step(
        self, step_id: str, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        entry = None
        if step_id == "reauth_confirm":
            entry = self._get_reauth_entry()
        elif step_id == "reconfigure":
            entry = self._get_reconfigure_entry()
        server_url = entry.data[CONF_SERVER_URL] if entry else ""
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if user_input is not None:
            try:
                server_url = normalize_server_url(user_input[CONF_SERVER_URL])
                client = AgentClient(async_get_clientsession(self.hass), server_url)
                pairing = await client.async_pair(user_input[CONF_PAIRING_CODE].strip())
            except AuthenticationError:
                errors["base"] = "invalid_auth"
            except TransportError as err:
                errors["base"] = str(err)
                if err.retry_after is not None:
                    placeholders["retry_after"] = str(int(err.retry_after + 0.999))
            except ProtocolError as err:
                code = str(err)
                errors["base"] = (
                    code
                    if code
                    in {
                        "invalid_url",
                        "invalid_code",
                        "tls_error",
                        "forbidden",
                        "invalid_response",
                    }
                    else "protocol_error"
                )
            else:
                # Identity belongs to the receiver-issued instance, also after migration.
                # Re-pairing another instance must never silently replace this entry.
                if entry and pairing.instance_id != entry.data[CONF_INSTANCE_ID]:
                    return self.async_abort(reason="unique_id_mismatch")
                await self.async_set_unique_id(pairing.instance_id)
                if entry is None:
                    self._abort_if_unique_id_configured()
                data = {CONF_SERVER_URL: server_url, **asdict(pairing)}
                if entry is not None:
                    return self.async_update_reload_and_abort(entry, data=data)
                return self.async_create_entry(
                    title=f"HA Monitor {pairing.instance_id[:8]}", data=data
                )
        return self.async_show_form(
            step_id=step_id,
            data_schema=_pair_schema(server_url),
            errors=errors,
            description_placeholders={
                "retry_after": placeholders.get("retry_after", "60")
            },
        )


class AgentOptionsFlow(OptionsFlowWithReload):
    """Explicit monitoring consent, metric allowlist and selected update sources."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=probatio.Schema(
                {
                    probatio.Required(
                        CONF_ENABLED,
                        default=self.config_entry.options.get(CONF_ENABLED, True),
                    ): bool,
                    probatio.Required(
                        CONF_MONITORING_ENABLED,
                        default=self.config_entry.options.get(
                            CONF_MONITORING_ENABLED, False
                        ),
                    ): bool,
                    probatio.Required(
                        CONF_EXPORT_METRICS,
                        default=self.config_entry.options.get(
                            CONF_EXPORT_METRICS, list(METRIC_KEYS)
                        ),
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=list(METRIC_KEYS),
                            multiple=True,
                            translation_key="metrics",
                        )
                    ),
                    probatio.Required(
                        CONF_SYSTEM_UPDATES,
                        default=self.config_entry.options.get(
                            CONF_SYSTEM_UPDATES, True
                        ),
                    ): bool,
                    probatio.Required(
                        CONF_DISCOVER_UPDATES,
                        default=self.config_entry.options.get(
                            CONF_DISCOVER_UPDATES, True
                        ),
                    ): bool,
                    probatio.Required(
                        CONF_EXPORT_ALL_UPDATES,
                        default=self.config_entry.options.get(
                            CONF_EXPORT_ALL_UPDATES, False
                        ),
                    ): bool,
                    probatio.Required(
                        CONF_UPDATE_ENTITIES,
                        default=self.config_entry.options.get(CONF_UPDATE_ENTITIES, []),
                    ): EntitySelector(
                        EntitySelectorConfig(domain="update", multiple=True)
                    ),
                }
            ),
        )
