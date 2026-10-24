"""Data update coordinators for EDF Energy (Kraken)."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import circuits, device_history
from .api import TELEMETRY_UNAVAILABLE_CODES, EdfApiError, EdfAuthError, EdfClient, EdfError
from .const import (
    ACCOUNT_INTERVAL,
    CONF_MPAN,
    CONF_SOURCE_ENTITY,
    CONSUMPTION_INTERVAL,
    DOMAIN,
    MAX_BACKFILL_DAYS,
    RATES_INTERVAL,
    SUBENTRY_TRACKED_DEVICE,
    SUMMARY_LOOKBACK_DAYS,
    TELEMETRY_BACKOFF,
    TELEMETRY_MAX_FAILURES,
    UK_TZ,
)
from .models import (
    OFF_PEAK,
    PEAK,
    AccountData,
    MeterPoint,
    Rate,
    band_at,
    parse_account,
    parse_rates,
    rate_at,
)
from .statistics import all_statistic_ids, async_import_statistics, async_last_imported
from .statistics import statistic_id as meter_statistic_id

_LOGGER = logging.getLogger(__name__)
# Bump to clear and re-import every statistic this integration writes.
STATISTICS_VERSION = 2


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
    latest_end: datetime | None = None
    previous_day: datetime | None = None
    previous_day_kwh: float | None = None
    previous_day_energy_cost: float | None = None  # GBP
    previous_day_standing_charge: float | None = None  # GBP
    # Split by band; None on single-rate tariffs.
    previous_day_peak_kwh: float | None = None
    previous_day_off_peak_kwh: float | None = None
    previous_day_peak_cost: float | None = None  # GBP
    previous_day_off_peak_cost: float | None = None  # GBP


@dataclass
class TrackedDay:
    """A tracked device's usage on the meter's last full day."""

    day: datetime
    peak_kwh: float
    off_peak_kwh: float
    peak_cost: float  # GBP
    off_peak_cost: float  # GBP
    share: float | None  # fraction of the meter's usage that day


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
        self.tracked: dict[str, TrackedDay] = {}
        self._devices_lock = asyncio.Lock()
        # Per meter: the circuit layout last written and how far the meter was settled then.
        self._marks_store: Store[dict[str, Any]] = Store(hass, 1, f"{DOMAIN}.circuits.{entry.entry_id}")
        self._marks: dict[str, Any] | None = None

    async def _fetch(self) -> dict[str, ConsumptionSummary]:
        await self._rebuild_if_outdated()
        result: dict[str, ConsumptionSummary] = {}
        for point in self.account.data.meter_points:
            if not point.serial:
                continue
            result[point.mpan] = await self._fetch_point(point)
        async with self._devices_lock:
            try:
                await self._update_devices(result)
            except Exception:
                # Tracked devices must never stop the meter's own figures updating.
                _LOGGER.exception("Updating tracked devices failed")
        return result

    async def _rebuild_if_outdated(self) -> None:
        """Clear statistics written by an older version with a pricing fault, so they're imported again.

        Version 2: Standard Variable's Direct Debit and non-Direct Debit prices
        were read as two bands, so its days were split into peak and off-peak.
        """
        if self._marks is None:
            self._marks = await self._marks_store.async_load() or {}
        if self._marks.get("_version") == STATISTICS_VERSION:
            return
        ids = [stat_id for point in self.account.data.meter_points for stat_id in all_statistic_ids(point)]
        ids += [
            device_history.statistic_id(subentry.subentry_id, kind)
            for subentry in self.config_entry.get_subentries_of_type(SUBENTRY_TRACKED_DEVICE)
            for kind in device_history.KINDS
        ]
        recorder = get_instance(self.hass)
        recorder.async_clear_statistics(ids)
        await recorder.async_block_till_done()
        # Circuits rebuild from scratch too.
        self._marks = {"_version": STATISTICS_VERSION}
        await self._marks_store.async_save(self._marks)

    async def async_update_devices(self) -> None:
        """Bring tracked devices up to the last complete hour, between meter fetches."""
        if self.data is None:
            return
        async with self._devices_lock:
            try:
                await self._update_devices(self.data)
            except EdfError as err:
                _LOGGER.debug("Tracked device update skipped: %s", err)
                return
            except Exception:
                _LOGGER.exception("Updating tracked devices failed")
                return
        self.async_update_listeners()

    async def _update_devices(self, meters: dict[str, ConsumptionSummary]) -> None:
        subentries = list(self.config_entry.get_subentries_of_type(SUBENTRY_TRACKED_DEVICE))
        grouped = circuits.grouped_ids(subentries)
        tracked: dict[str, TrackedDay] = {}
        for subentry in subentries:
            if subentry.subentry_id in grouped:
                continue
            point = self._point(subentry.data[CONF_MPAN])
            if point is None:
                continue
            day = await self._update_tracked(
                subentry.subentry_id, subentry.title, subentry.data[CONF_SOURCE_ENTITY], point, meters.get(point.mpan)
            )
            if day:
                tracked[subentry.subentry_id] = day
        for point in self.account.data.meter_points:
            if not point.is_export:
                tracked.update(await self._update_circuits(point, meters.get(point.mpan), subentries))
        self.tracked = tracked

    def _point(self, mpan: str) -> MeterPoint | None:
        return next((p for p in self.account.data.meter_points if p.mpan == mpan), None)

    async def _update_circuits(
        self, point: MeterPoint, meter: ConsumptionSummary | None, subentries: list[ConfigSubentry]
    ) -> dict[str, TrackedDay]:
        """Rewrite this meter's circuits from the first hour that may have changed."""
        board = circuits.layout(subentries, point.mpan)
        if self._marks is None:
            self._marks = await self._marks_store.async_load() or {}
        mark = self._marks.get(point.mpan, {})
        if not board:
            if mark:
                self._clear(mark.get("series", []))
                self._marks.pop(point.mpan)
                await self._marks_store.async_save(self._marks)
            return {}

        now = dt_util.utcnow()
        this_hour = now.replace(minute=0, second=0, microsecond=0)
        supply_start = point.agreements[0].valid_from if point.agreements else this_hour
        backfill_start = max(supply_start, this_hour - timedelta(days=MAX_BACKFILL_DAYS))
        layout_sig = circuits.signature(board)
        bases = circuits.series_ids(board)
        recorder = get_instance(self.hass)
        if mark.get("layout") != layout_sig:
            # New or changed circuits: rebuild everything.
            self._clear(mark.get("series", []))
            rewrite_from = backfill_start
        else:
            ends = [await device_history.async_last_imported(self.hass, base) for base in bases]
            rewrite_from = backfill_start if None in ends else min(ends)
            if mark.get("settled_to"):
                # Hours written before EDF had them may be settled now.
                rewrite_from = min(rewrite_from, dt_util.parse_datetime(mark["settled_to"]))
            rewrite_from = max(rewrite_from, backfill_start)
        # The meter's statistics from this run (and any clear) must be in before reading.
        await recorder.async_block_till_done()

        day_start = meter.previous_day if meter else None
        fetch_from = min(rewrite_from, day_start) if day_start else rewrite_from
        if fetch_from >= this_hour:
            return {}
        measured: dict[str, dict[datetime, float]] = {}
        for member in [m for c in board for m in (c, *c.loads)]:
            kind = await device_history.async_source_kind(self.hass, member.entity_id)
            if kind is None:
                _LOGGER.warning("%s has no long-term statistics; counted as zero", member.entity_id)
                continue
            measured[member.subentry_id] = dict(
                await device_history.async_hourly_kwh(self.hass, member.entity_id, kind, fetch_from, this_hour)
            )
        meter_hours = dict(
            await device_history.async_hourly_kwh(
                self.hass, meter_statistic_id(point, "energy"), "energy", fetch_from, this_hour
            )
        )
        settled_to = await async_last_imported(self.hass, point)
        hours, _scaled = circuits.reconcile(board, measured, meter_hours, settled_to, fetch_from, this_hour)

        rates = await self._historic_rates(point, fetch_from, now)
        names = {}
        for c in board:
            names[c.subentry_id] = c.name
            names.update({load.subentry_id: load.name for load in c.loads})
        tracked: dict[str, TrackedDay] = {}
        for base, kwh in hours.items():
            priced = device_history.price_hours(
                kwh,
                lambda moment: _rate_for(point, rates, moment),
                lambda moment: _band_for(point, rates, moment),
            )
            await device_history.async_rewrite(self.hass, base, names[base], [p for p in priced if p[0] >= rewrite_from])
            if day_start and (day := _tracked_day(priced, day_start, meter)):
                tracked[base] = day

        self._marks[point.mpan] = {
            "layout": layout_sig,
            "series": bases,
            "settled_to": settled_to.isoformat() if settled_to else None,
        }
        await self._marks_store.async_save(self._marks)
        return tracked

    def _clear(self, bases: list[str]) -> None:
        if bases:
            get_instance(self.hass).async_clear_statistics(
                [device_history.statistic_id(base, kind) for base in bases for kind in device_history.KINDS]
            )

    async def _update_tracked(
        self, subentry_id: str, name: str, entity_id: str, point: MeterPoint, meter: ConsumptionSummary | None
    ) -> TrackedDay | None:
        """Bring a tracked device's statistics up to date and total its last full day."""
        kind = await device_history.async_source_kind(self.hass, entity_id)
        if kind is None:
            _LOGGER.debug("%s has no long-term statistics; history not available", entity_id)
            return None
        now = dt_util.utcnow()
        this_hour = now.replace(minute=0, second=0, microsecond=0)
        # Backfill as far as the EDF supply goes (capped), then carry on hourly.
        supply_start = point.agreements[0].valid_from if point.agreements else this_hour
        start = await device_history.async_last_imported(self.hass, subentry_id)
        if start is None:
            start = max(supply_start, this_hour - timedelta(days=MAX_BACKFILL_DAYS))
        day_start = meter.previous_day if meter else None
        fetch_from = min(start, day_start) if day_start else start
        if fetch_from >= this_hour:
            return None

        hours = await device_history.async_hourly_kwh(self.hass, entity_id, kind, fetch_from, this_hour)
        rates = await self._historic_rates(point, fetch_from, now)
        priced = device_history.price_hours(
            hours,
            lambda moment: _rate_for(point, rates, moment),
            lambda moment: _band_for(point, rates, moment),
        )
        await device_history.async_import(self.hass, subentry_id, name, [p for p in priced if p[0] >= start])

        if not day_start:
            return None
        return _tracked_day(priced, day_start, meter)

    async def _fetch_point(self, point: MeterPoint) -> ConsumptionSummary:
        now = dt_util.now(UK_TZ)
        today = datetime.combine(now.date(), time(), tzinfo=UK_TZ)
        # Smart meter data usually lags a day or two, so look back far enough
        # to find the most recent complete day.
        summary_from = today - timedelta(days=SUMMARY_LOOKBACK_DAYS)

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
                lambda moment: _band_for(point, rates, moment),
            )

        summary = ConsumptionSummary()
        if readings:
            summary.latest_end = readings[-1][1]
        day_start, day = _latest_complete_day(readings, summary_from)
        if day_start is not None:
            summary.previous_day = day_start
            summary.previous_day_kwh = round(sum(r[2] for r in day), 3)
            energy_cost = 0.0
            bands = {PEAK: [0.0, 0.0], OFF_PEAK: [0.0, 0.0]}  # band -> [kWh, pence]
            for start, _end, kwh in day:
                rate = _rate_for(point, rates, start)
                if rate is None:
                    energy_cost = None
                    break
                energy_cost += kwh * rate
                band = _band_for(point, rates, start)
                if band:
                    bands[band][0] += kwh
                    bands[band][1] += kwh * rate
            if energy_cost is not None:
                summary.previous_day_energy_cost = round(energy_cost / 100, 2)
                if any(_band_for(point, rates, r[0]) for r in day):
                    summary.previous_day_peak_kwh = round(bands[PEAK][0], 3)
                    summary.previous_day_off_peak_kwh = round(bands[OFF_PEAK][0], 3)
                    summary.previous_day_peak_cost = round(bands[PEAK][1] / 100, 2)
                    summary.previous_day_off_peak_cost = round(bands[OFF_PEAK][1] / 100, 2)
            agreement = point.agreement_at(day_start)
            standing = _standing_for(agreement, rates, day_start)
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


def _tracked_day(
    priced: list[tuple[datetime, dict[str, float]]], day_start: datetime, meter: ConsumptionSummary | None
) -> TrackedDay:
    """Total priced hours over the meter's last full day."""
    day_end = datetime.combine(day_start.date() + timedelta(days=1), time(), tzinfo=UK_TZ)
    totals = dict.fromkeys(device_history.KINDS, 0.0)
    for hour, values in priced:
        if day_start <= hour < day_end:
            for key, value in values.items():
                totals[key] += value
    device_kwh = totals["peak_energy"] + totals["off_peak_energy"]
    return TrackedDay(
        day=day_start,
        peak_kwh=round(totals["peak_energy"], 3),
        off_peak_kwh=round(totals["off_peak_energy"], 3),
        peak_cost=round(totals["peak_cost"], 2),
        off_peak_cost=round(totals["off_peak_cost"], 2),
        share=round(device_kwh / meter.previous_day_kwh, 3) if meter and meter.previous_day_kwh else None,
    )


def _latest_complete_day(
    readings: list[tuple[datetime, datetime, float]], since: datetime
) -> tuple[datetime | None, list[tuple[datetime, datetime, float]]]:
    """The newest UK day since `since` that has every half hour, and its readings."""
    by_day: dict[datetime, list[tuple[datetime, datetime, float]]] = {}
    for reading in readings:
        if reading[0] < since:
            continue
        local = reading[0].astimezone(UK_TZ)
        by_day.setdefault(datetime.combine(local.date(), time(), tzinfo=UK_TZ), []).append(reading)
    for day_start in sorted(by_day, reverse=True):
        day_end = datetime.combine(day_start.date() + timedelta(days=1), time(), tzinfo=UK_TZ)
        # 46, 48 or 50 half hours depending on clock changes.
        slots = int((day_end - day_start.astimezone(dt_util.UTC)).total_seconds() // 1800)
        if len({r[0] for r in by_day[day_start]}) >= slots:
            return day_start, by_day[day_start]
    return None, []


def _rate_for(point: MeterPoint, rates: dict[str, dict[str, list[Rate]]], moment: datetime) -> float | None:
    agreement = point.agreement_at(moment)
    if not agreement or not agreement.tariff or agreement.tariff.tariff_code not in rates:
        return None
    rate = rate_at(rates[agreement.tariff.tariff_code]["unit"], moment)
    return rate.value if rate else None


def _band_for(point: MeterPoint, rates: dict[str, dict[str, list[Rate]]], moment: datetime) -> str | None:
    agreement = point.agreement_at(moment)
    if not agreement or not agreement.tariff or agreement.tariff.tariff_code not in rates:
        return None
    return band_at(rates[agreement.tariff.tariff_code]["unit"], moment)


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
