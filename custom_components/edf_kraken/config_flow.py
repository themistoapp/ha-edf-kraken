"""Config flow for EDF Energy (Kraken)."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import EdfAuthError, EdfClient, EdfError, EdfRateLimitError
from .const import (
    AUTH_API_KEY,
    AUTH_PASSWORD,
    CONF_ACCOUNT_NUMBER,
    CONF_API_KEY,
    CONF_AUTH_METHOD,
    CONF_BACKFILL_DAYS,
    CONF_TELEMETRY_INTERVAL,
    DEFAULT_BACKFILL_DAYS,
    DEFAULT_TELEMETRY_INTERVAL,
    DOMAIN,
    MAX_BACKFILL_DAYS,
    MIN_TELEMETRY_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

PASSWORD_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
API_KEY_SCHEMA = vol.Schema({vol.Required(CONF_API_KEY): PASSWORD_SELECTOR})
PASSWORD_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): TextSelector(TextSelectorConfig(type=TextSelectorType.EMAIL)),
        vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
    }
)


async def _validate(hass, credentials: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
    """Log in and list the accounts. Returns (accounts, errors)."""
    client = EdfClient(
        async_get_clientsession(hass),
        api_key=credentials.get(CONF_API_KEY),
        email=credentials.get(CONF_EMAIL),
        password=credentials.get(CONF_PASSWORD),
    )
    try:
        accounts = await client.async_get_account_numbers()
    except EdfAuthError:
        return [], {"base": "invalid_auth"}
    except EdfRateLimitError:
        return [], {"base": "rate_limited"}
    except EdfError:
        _LOGGER.exception("Error connecting to EDF")
        return [], {"base": "cannot_connect"}
    if not accounts:
        return [], {"base": "no_accounts"}
    return accounts, {}


class EdfConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._credentials: dict[str, Any] = {}
        self._accounts: list[str] = []

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="user", menu_options=[AUTH_API_KEY, AUTH_PASSWORD])

    async def async_step_api_key(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._credentials_step(AUTH_API_KEY, API_KEY_SCHEMA, user_input)

    async def async_step_password(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._credentials_step(AUTH_PASSWORD, PASSWORD_SCHEMA, user_input)

    async def _credentials_step(self, method, schema, user_input) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            credentials = {CONF_AUTH_METHOD: method, **user_input}
            accounts, errors = await _validate(self.hass, credentials)
            if not errors:
                self._credentials = credentials
                self._accounts = accounts
                if len(accounts) == 1:
                    return await self._create(accounts[0])
                return await self.async_step_account()
        return self.async_show_form(step_id=method, data_schema=schema, errors=errors)

    async def async_step_account(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return await self._create(user_input[CONF_ACCOUNT_NUMBER])
        return self.async_show_form(
            step_id="account",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ACCOUNT_NUMBER): SelectSelector(
                        SelectSelectorConfig(options=self._accounts)
                    )
                }
            ),
        )

    async def _create(self, account_number: str) -> ConfigFlowResult:
        await self.async_set_unique_id(account_number)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=f"EDF {account_number}",
            data={**self._credentials, CONF_ACCOUNT_NUMBER: account_number},
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        method = entry.data.get(CONF_AUTH_METHOD, AUTH_API_KEY)
        schema = API_KEY_SCHEMA if method == AUTH_API_KEY else PASSWORD_SCHEMA
        errors: dict[str, str] = {}
        if user_input is not None:
            credentials = {CONF_AUTH_METHOD: method, **user_input}
            accounts, errors = await _validate(self.hass, credentials)
            if not errors and entry.data[CONF_ACCOUNT_NUMBER] not in accounts:
                errors = {"base": "wrong_account"}
            if not errors:
                return self.async_update_reload_and_abort(
                    entry, data={**entry.data, **credentials}
                )
        return self.async_show_form(step_id="reauth_confirm", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return EdfOptionsFlow()


class EdfOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                data={k: int(v) for k, v in user_input.items()}
            )
        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_TELEMETRY_INTERVAL,
                        default=options.get(CONF_TELEMETRY_INTERVAL, DEFAULT_TELEMETRY_INTERVAL),
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=MIN_TELEMETRY_INTERVAL,
                            max=3600,
                            step=10,
                            unit_of_measurement="s",
                            mode=NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Required(
                        CONF_BACKFILL_DAYS,
                        default=options.get(CONF_BACKFILL_DAYS, DEFAULT_BACKFILL_DAYS),
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=1, max=MAX_BACKFILL_DAYS, step=1, unit_of_measurement="days",
                            mode=NumberSelectorMode.BOX,
                        )
                    ),
                }
            ),
        )
