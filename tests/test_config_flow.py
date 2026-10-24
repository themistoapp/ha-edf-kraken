"""Config flow tests."""

from unittest.mock import AsyncMock, patch

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType

from custom_components.edf_kraken.api import EdfAuthError
from custom_components.edf_kraken.const import CONF_ACCOUNT_NUMBER, CONF_API_KEY, DOMAIN

CLIENT = "custom_components.edf_kraken.config_flow.EdfClient"


async def _start_api_key(hass):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.MENU
    return await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "api_key"})


async def test_api_key_single_account(hass):
    result = await _start_api_key(hass)
    with (
        patch(f"{CLIENT}.async_get_account_numbers", AsyncMock(return_value=["A-1"])),
        patch("custom_components.edf_kraken.async_setup_entry", AsyncMock(return_value=True)),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_API_KEY: "sk_live_x"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_ACCOUNT_NUMBER] == "A-1"
    assert result["result"].unique_id == "A-1"


async def test_multiple_accounts_asks_which(hass):
    result = await _start_api_key(hass)
    with patch(f"{CLIENT}.async_get_account_numbers", AsyncMock(return_value=["A-1", "A-2"])):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_API_KEY: "sk_live_x"})
    assert result["step_id"] == "account"
    with patch("custom_components.edf_kraken.async_setup_entry", AsyncMock(return_value=True)):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_ACCOUNT_NUMBER: "A-2"})
    assert result["data"][CONF_ACCOUNT_NUMBER] == "A-2"


async def test_invalid_auth(hass):
    result = await _start_api_key(hass)
    with patch(f"{CLIENT}.async_get_account_numbers", AsyncMock(side_effect=EdfAuthError("bad"))):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_API_KEY: "bad"})
    assert result["errors"] == {"base": "invalid_auth"}
