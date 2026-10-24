"""Diagnostics for EDF Energy (Kraken)."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant

from . import EdfConfigEntry
from .const import CONF_API_KEY

TO_REDACT = {CONF_API_KEY, CONF_EMAIL, CONF_PASSWORD, "address", "mpan", "serial", "device_id", "number", "account_number"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: EdfConfigEntry
) -> dict[str, Any]:
    data = entry.runtime_data
    return async_redact_data(
        {
            "entry": dict(entry.data),
            "options": dict(entry.options),
            "account": asdict(data.account.data) if data.account.data else None,
            "rates": {
                f"meter_{i}": asdict(r) for i, r in enumerate((data.rates.data or {}).values())
            },
            "consumption": {
                f"meter_{i}": asdict(s) for i, s in enumerate((data.consumption.data or {}).values())
            },
            "telemetry": {f"device_{i}": t.data for i, t in enumerate(data.telemetry.values())},
        },
        TO_REDACT,
    )
