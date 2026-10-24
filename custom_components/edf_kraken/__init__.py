"""The EDF Energy (Kraken) integration."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import EdfClient
from .const import (
    CONF_ACCOUNT_NUMBER,
    CONF_API_KEY,
    CONF_BACKFILL_DAYS,
    CONF_TELEMETRY_INTERVAL,
    DEFAULT_BACKFILL_DAYS,
    DEFAULT_TELEMETRY_INTERVAL,
)
from .coordinator import (
    AccountCoordinator,
    ConsumptionCoordinator,
    RatesCoordinator,
    TelemetryCoordinator,
)

PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR]


@dataclass
class EdfRuntimeData:
    client: EdfClient
    account: AccountCoordinator
    rates: RatesCoordinator
    consumption: ConsumptionCoordinator
    telemetry: dict[str, TelemetryCoordinator] = field(default_factory=dict)


type EdfConfigEntry = ConfigEntry[EdfRuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: EdfConfigEntry) -> bool:
    client = EdfClient(
        async_get_clientsession(hass),
        api_key=entry.data.get(CONF_API_KEY),
        email=entry.data.get(CONF_EMAIL),
        password=entry.data.get(CONF_PASSWORD),
    )
    options = entry.options

    account = AccountCoordinator(hass, entry, client, entry.data[CONF_ACCOUNT_NUMBER])
    await account.async_config_entry_first_refresh()

    rates = RatesCoordinator(hass, entry, client, account)
    consumption = ConsumptionCoordinator(
        hass, entry, client, account, options.get(CONF_BACKFILL_DAYS, DEFAULT_BACKFILL_DAYS)
    )
    await rates.async_config_entry_first_refresh()
    # Consumption can take a while on the first backfill and isn't needed to
    # create entities, so let it load in the background.
    entry.async_create_background_task(
        hass, consumption.async_refresh(), "edf_kraken consumption first refresh"
    )

    runtime = EdfRuntimeData(client, account, rates, consumption)
    interval = timedelta(seconds=options.get(CONF_TELEMETRY_INTERVAL, DEFAULT_TELEMETRY_INTERVAL))
    for point in account.data.meter_points:
        if point.device_id and not point.is_export and point.device_id not in runtime.telemetry:
            coordinator = TelemetryCoordinator(hass, entry, client, point.device_id, interval)
            # One probe decides whether this home has live telemetry at all.
            await coordinator.async_refresh()
            if coordinator.last_update_success:
                runtime.telemetry[point.device_id] = coordinator

    # Pick up tariff changes as soon as the account shows them.
    known_tariffs = {p.mpan: p.tariff for p in account.data.meter_points}

    def _account_updated() -> None:
        current = {p.mpan: p.tariff for p in account.data.meter_points}
        if current != known_tariffs:
            known_tariffs.clear()
            known_tariffs.update(current)
            hass.async_create_task(rates.async_request_refresh())

    entry.async_on_unload(account.async_add_listener(_account_updated))
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    entry.runtime_data = runtime
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_options_updated(hass: HomeAssistant, entry: EdfConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: EdfConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
