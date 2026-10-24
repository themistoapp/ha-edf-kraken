"""The EDF Energy (Kraken) integration."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_utc_time_change
from homeassistant.helpers.typing import ConfigType
from homeassistant.loader import async_get_integration

from .api import EdfClient
from .circuits import grouped_ids
from .const import (
    CONF_ACCOUNT_NUMBER,
    CONF_API_KEY,
    CONF_BACKFILL_DAYS,
    CONF_MPAN,
    CONF_POWER_ENTITY,
    CONF_SOURCE_ENTITY,
    CONF_TELEMETRY_INTERVAL,
    DEFAULT_BACKFILL_DAYS,
    DEFAULT_TELEMETRY_INTERVAL,
    DOMAIN,
    SUBENTRY_TRACKED_DEVICE,
)
from .coordinator import (
    AccountCoordinator,
    ConsumptionCoordinator,
    RatesCoordinator,
    TelemetryCoordinator,
)
from .tracker import DeviceTracker
from .websocket import async_register as async_register_websocket

PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
CARD_URL = f"/{DOMAIN}/edf-usage-card.js"
LIVE_TOTAL_KEYS = ("peak_energy", "off_peak_energy", "peak_cost", "off_peak_cost", "total_cost")


@dataclass
class EdfRuntimeData:
    client: EdfClient
    account: AccountCoordinator
    rates: RatesCoordinator
    consumption: ConsumptionCoordinator
    telemetry: dict[str, TelemetryCoordinator] = field(default_factory=dict)
    trackers: dict[str, DeviceTracker] = field(default_factory=dict)


type EdfConfigEntry = ConfigEntry[EdfRuntimeData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async_register_websocket(hass)
    # The usage card ships with the integration, so it loads on every dashboard without a separate install.
    if hass.http is not None and "frontend" in hass.config.components:
        from homeassistant.components.frontend import add_extra_js_url
        from homeassistant.components.http import StaticPathConfig

        path = Path(__file__).parent / "frontend" / "edf-usage-card.js"
        await hass.http.async_register_static_paths([StaticPathConfig(CARD_URL, str(path), cache_headers=False)])
        version = (await async_get_integration(hass, DOMAIN)).version
        add_extra_js_url(hass, f"{CARD_URL}?v={version}")
    return True


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

    # Home Assistant compiles each hour's statistics just after the hour, so a
    # few minutes later tracked devices can catch up without asking EDF.
    @callback
    def _hourly(_now) -> None:
        entry.async_create_background_task(
            hass, consumption.async_update_devices(), "edf_kraken tracked devices"
        )

    entry.async_on_unload(async_track_utc_time_change(hass, _hourly, minute=5, second=0))
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    # The latest half-hour sensor was removed in 0.1.2.
    registry = er.async_get(hass)
    for point in account.data.meter_points:
        if entity_id := registry.async_get_entity_id(
            "sensor", DOMAIN, f"{account.account_number}_electricity_{point.mpan}_latest_reading"
        ):
            registry.async_remove(entity_id)

    # 0.2.0 gave each circuit a separate "Rest of" device, and circuits and the
    # loads inside them had live running totals; 0.2.1 drops both.
    devices = dr.async_get(hass)
    grouped = grouped_ids(entry.subentries.values())
    for device in dr.async_entries_for_config_entry(devices, entry.entry_id):
        if any(domain == DOMAIN and ident.startswith("tracked_remainder_") for domain, ident in device.identifiers):
            devices.async_remove_device(device.id)
    for subentry_id in grouped:
        for key in LIVE_TOTAL_KEYS:
            if entity_id := registry.async_get_entity_id("sensor", DOMAIN, f"tracked_{subentry_id}_{key}"):
                registry.async_remove(entity_id)

    for subentry in entry.get_subentries_of_type(SUBENTRY_TRACKED_DEVICE):
        tracker = DeviceTracker(
            hass,
            rates,
            subentry.subentry_id,
            subentry.data[CONF_SOURCE_ENTITY],
            subentry.data[CONF_MPAN],
            subentry.data.get(CONF_POWER_ENTITY),
        )
        entry.async_on_unload(await tracker.async_start())
        runtime.trackers[subentry.subentry_id] = tracker

    entry.runtime_data = runtime
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_options_updated(hass: HomeAssistant, entry: EdfConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_entry(hass: HomeAssistant, entry: EdfConfigEntry) -> None:
    for subentry in entry.subentries.values():
        await DeviceTracker.async_remove_storage(hass, subentry.subentry_id)


async def async_unload_entry(hass: HomeAssistant, entry: EdfConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
