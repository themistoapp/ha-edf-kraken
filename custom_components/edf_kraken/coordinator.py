"""Data update coordinators for EDF Energy (Kraken)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import TELEMETRY_UNAVAILABLE_CODES, EdfApiError, EdfAuthError, EdfClient, EdfError
from .const import (
    ACCOUNT_INTERVAL,
    CONSUMPTION_INTERVAL,
    DOMAIN,
    RATES_INTERVAL,
    TELEMETRY_BACKOFF,
    TELEMETRY_MAX_FAILURES,
    UK_TZ,
)
from .models import AccountData, MeterPoint, Rate, parse_account, parse_rates, rate_at
from .statistics import async_import_statistics, async_last_imported

_LOGGER = logging.getLogger(__name__)


class EdfCoordinator[T](DataUpdateCoordinator[T]):
    """Coordinator that maps client errors onto Home Assistant's."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: EdfClient,
        name: str,
        interval: timedelta,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {name}",
            update_interval=interval,
        )
        self.client = client

    async def _async_update_data(self) -> T:
        try:
            return await self._fetch()
        except EdfAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except EdfError as err:
            raise UpdateFailed(str(err)) from err

    async def _fetch(self) -> T:
        raise NotImplementedError


class AccountCoordinator(EdfCoordinator[AccountData]):
    """Balance, meter points, agreements and the points allowance."""

    def __init__(self, hass, entry, client, account_number: str) -> None:
        super().__init__(hass, entry, client, "account", ACCOUNT_INTERVAL)
        self.account_number = account_number

    async def _fetch(self) -> AccountData:
        return parse_account(await self.client.async_get_account(self.account_number))


@dataclass
class TariffRates:
    unit_rates: list[Rate]
    standing_charges: list[Rate]


class RatesCoordinator(EdfCoordinator[dict[str, TariffRates]]):
    """Unit rates and standing charges for each meter point's current tariff.

    These come from the public product endpoints, so they cost no points.
    """

    def __init__(self, hass, entry, client, account: AccountCoordinator) -> None:
        super().__init__(hass, entry, client, "rates", RATES_INTERVAL)
        self.account = account

    async def _fetch(self) -> dict[str, TariffRates]:
        now = dt_util.now(UK_TZ)
        start = datetime.combine(now.date(), time(), tzinfo=UK_TZ) - timedelta(days=1)
        end = start + timedelta(days=3)
        result: dict[str, TariffRates] = {}
        for point in self.account.data.meter_points:
            tariff = point.tariff
            if not tariff or not tariff.product_code:
                continue
            result[point.mpan] = TariffRates(
                parse_rates(
                    await self.client.async_get_unit_rates(
                        tariff.product_code, tariff.tariff_code, start, end
                    )
                ),
                parse_rates(
                    await self.client.async_get_standing_charges(
                        tariff.product_code, tariff.tariff_code, start, end
                    )
                ),
            )
        return result


@dataclass
class ConsumptionSummary:
    latest_start: datetime | None = None
    latest_end: datetime | None = None
    latest_kwh: float | None = None
    previous_day: datetime | None = None
    previous_day_kwh: float | None = None
    previous_day_energy_cost: float | None = None  # GBP
    previous_day_standing_charge: float | None = None  # GBP


class ConsumptionCoordinator(EdfCoordinator[dict[str, ConsumptionSummary]]):
    """Half-hourly smart meter consumption.

    Each run asks only for data newer than the last hour already imported into
    long-term statistics (or the last two days for the summary sensors), so the
    REST endpoint sees one small request per meter every half hour.
    """

    def __init__(self, hass, entry, client, account: AccountCoordinator, backfill_days: int) -> None:
        super().__init__(hass, entry, client, "consumption", CONSUMPTION_INTERVAL)
        self.account = account
        self.backfill_days = backfill_days

    async def _fetch(self) -> dict[str, ConsumptionSummary]:
        result: dict[str, ConsumptionSummary] = {}
        for point in self.account.data.meter_points:
            if not point.serial:
                continue
            result[point.mpan] = await self._fetch_point(point)
        return result

    async def _fetch_point(self, point: MeterPoint) -> ConsumptionSummary:
        now = dt_util.now(UK_TZ)
        today = datetime.combine(now.date(), time(), tzinfo=UK_TZ)
        yesterday = today - timedelta(days=1)
        summary_from = yesterday - timedelta(days=1)

        stats_from = await async_last_imported(self.hass, point)
        if stats_from is None:
            stats_from = today - timedelta(days=self.backfill_days)
        period_from = min(stats_from, summary_from)

        rows = await self.client.async_get_consumption(point.mpan, point.serial, period_from)
        readings = [
            (dt_util.parse_datetime(r["interval_start"]), dt_util.parse_datetime(r["interval_end"]), float(r["consumption"]))
            for r in rows
            if r.get("interval_start") and r.get("consumption") is not None
        ]
        readings.sort(key=lambda r: r[0])
        rates = await self._historic_rates(point, period_from, now)

        if readings:
            await async_import_statistics(
                self.hass,
                point,
                [r for r in readings if r[0] >= stats_from],
                lambda moment: _rate_for(point, rates, moment),
            )

        summary = ConsumptionSummary()
        if readings:
            summary.latest_start, summary.latest_end, summary.latest_kwh = readings[-1]
        day = [r for r in readings if yesterday <= r[0] < today]
        if len(day) >= 46:  # allow for the short day when the clocks change
            summary.previous_day = yesterday
            summary.previous_day_kwh = round(sum(r[2] for r in day), 3)
            energy_cost = 0.0
            for start, _end, kwh in day:
                rate = _rate_for(point, rates, start)
                if rate is None:
                    energy_cost = None
                    break
                energy_cost += kwh * rate
            if energy_cost is not None:
                summary.previous_day_energy_cost = round(energy_cost / 100, 2)
            agreement = point.agreement_at(yesterday)
            standing = _standing_for(agreement, rates, yesterday)
            if standing is not None:
                summary.previous_day_standing_charge = round(standing / 100, 2)
        return summary

    async def _historic_rates(
        self, point: MeterPoint, period_from: datetime, period_to: datetime
    ) -> dict[str, dict[str, list[Rate]]]:
        """Unit rates and standing charges for every tariff in the window."""
        rates: dict[str, dict[str, list[Rate]]] = {}
        for agreement in point.agreements:
            tariff = agreement.tariff
            if not tariff or not tariff.product_code or tariff.tariff_code in rates:
                continue
            start = max(agreement.valid_from, period_from)
            end = min(agreement.valid_to or period_to, period_to)
            if start >= end:
                continue
            rates[tariff.tariff_code] = {
                "unit": parse_rates(
                    await self.client.async_get_unit_rates(tariff.product_code, tariff.tariff_code, start, end)
                ),
                "standing": parse_rates(
                    await self.client.async_get_standing_charges(tariff.product_code, tariff.tariff_code, start, end)
                ),
            }
        return rates


def _rate_for(point: MeterPoint, rates: dict[str, dict[str, list[Rate]]], moment: datetime) -> float | None:
    agreement = point.agreement_at(moment)
    if not agreement or not agreement.tariff or agreement.tariff.tariff_code not in rates:
        return None
    rate = rate_at(rates[agreement.tariff.tariff_code]["unit"], moment)
    return rate.value if rate else None


def _standing_for(agreement, rates: dict[str, dict[str, list[Rate]]], moment: datetime) -> float | None:
    if not agreement or not agreement.tariff:
        return None
    tariff_rates = rates.get(agreement.tariff.tariff_code)
    if tariff_rates:
        rate = rate_at(tariff_rates["standing"], moment)
        if rate:
            return rate.value
    return agreement.tariff.standing_charge


class TelemetryCoordinator(EdfCoordinator[dict[str, Any] | None]):
    """Live readings from the smart meter's in-home display.

    Not every home has a display that reports to EDF. After a few consecutive
    "telemetry unavailable" errors the poll interval drops to once every half
    hour so we don't spend points on a feed that isn't there.
    """

    def __init__(self, hass, entry, client, device_id: str, interval: timedelta) -> None:
        super().__init__(hass, entry, client, "telemetry", interval)
        self.device_id = device_id
        self._normal_interval = interval
        self._failures = 0

    async def _fetch(self) -> dict[str, Any] | None:
        try:
            reading = await self.client.async_get_telemetry(self.device_id)
        except EdfApiError as err:
            if err.code not in TELEMETRY_UNAVAILABLE_CODES:
                raise
            self._failures += 1
            if self._failures >= TELEMETRY_MAX_FAILURES and self.update_interval != TELEMETRY_BACKOFF:
                _LOGGER.info(
                    "Smart meter telemetry unavailable for %s; checking every %s instead",
                    self.device_id,
                    TELEMETRY_BACKOFF,
                )
                self.update_interval = TELEMETRY_BACKOFF
            raise
        if self._failures:
            self._failures = 0
            self.update_interval = self._normal_interval
        return reading
