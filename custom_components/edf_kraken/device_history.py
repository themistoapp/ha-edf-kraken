"""Rebuild a tracked device's peak/off-peak usage and cost from recorder history.

EDF's meter data only settles a day or two late, so a device's figures need
to be worked out for past days too, not just accumulated live. Home Assistant
already keeps hourly long-term statistics for any sensor with a state class:
an hourly change for energy sensors and an hourly mean for power sensors.
Go Electric's bands change on the hour, so hourly data splits exactly.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMeanType, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    get_metadata,
    statistics_during_period,
)
from homeassistant.const import UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter, PowerConverter

from .const import DOMAIN

Hour = tuple[datetime, float]  # (hour start UTC, kWh)

KINDS = {
    "peak_energy": ("peak energy", UnitOfEnergy.KILO_WATT_HOUR),
    "off_peak_energy": ("off-peak energy", UnitOfEnergy.KILO_WATT_HOUR),
    "peak_cost": ("peak cost", "GBP"),
    "off_peak_cost": ("off-peak cost", "GBP"),
}


def statistic_id(base: str, kind: str) -> str:
    """A tracked device's statistic for one kind."""
    return f"{DOMAIN}:tracked_{base}_{kind}".lower()


async def async_source_kind(hass: HomeAssistant, entity_id: str) -> str | None:
    """"energy" or "power" if the sensor has usable long-term statistics, else None."""
    meta = await get_instance(hass).async_add_executor_job(
        lambda: get_metadata(hass, statistic_ids={entity_id})
    )
    if entity_id not in meta:
        return None
    _id, info = meta[entity_id]
    unit_class = info.get("unit_class")
    if info.get("has_sum") and (unit_class == EnergyConverter.UNIT_CLASS or info.get("unit_of_measurement") in EnergyConverter.VALID_UNITS):
        return "energy"
    if info.get("mean_type") != StatisticMeanType.NONE and (
        unit_class == PowerConverter.UNIT_CLASS or info.get("unit_of_measurement") in PowerConverter.VALID_UNITS
    ):
        return "power"
    return None


async def async_hourly_kwh(
    hass: HomeAssistant, entity_id: str, kind: str, start: datetime, end: datetime
) -> list[Hour]:
    """kWh used in each hour from start (inclusive) to end (exclusive)."""
    if start >= end:
        return []
    if kind == "energy":
        units, types, key = {EnergyConverter.UNIT_CLASS: UnitOfEnergy.KILO_WATT_HOUR}, {"change"}, "change"
    else:
        units, types, key = {PowerConverter.UNIT_CLASS: UnitOfPower.KILO_WATT}, {"mean"}, "mean"
    result = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, start, end, {entity_id}, "hour", units, types
    )
    hours: list[Hour] = []
    for row in result.get(entity_id, []):
        value = row.get(key)
        if value is None:
            continue
        # A negative change means the meter reset; that hour is unknowable.
        hours.append((dt_util.utc_from_timestamp(row["start"]), max(float(value), 0.0)))
    return hours


def price_hours(
    hours: list[Hour],
    rate_for: Callable[[datetime], float | None],
    band_for: Callable[[datetime], str | None],
) -> list[tuple[datetime, dict[str, float]]]:
    """Split each hour into band energy and cost.

    Each half of the hour is priced separately, so half-hourly tariffs are
    handled too; on Go Electric both halves always share a band.
    """
    priced = []
    for hour, kwh in hours:
        values = dict.fromkeys(KINDS, 0.0)
        for offset in (timedelta(0), timedelta(minutes=30)):
            moment = hour + offset
            half = kwh / 2
            band = "off_peak" if band_for(moment) == "off_peak" else "peak"
            values[f"{band}_energy"] += half
            values[f"{band}_cost"] += half * (rate_for(moment) or 0.0) / 100
        priced.append((hour, values))
    return priced


async def _last(hass: HomeAssistant, stat_id: str) -> tuple[datetime, float] | None:
    result = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, stat_id, True, {"sum"}
    )
    rows = result.get(stat_id)
    if not rows:
        return None
    return dt_util.utc_from_timestamp(rows[0]["start"]), rows[0]["sum"] or 0.0


async def async_last_imported(hass: HomeAssistant, subentry_id: str) -> datetime | None:
    """End of the last hour imported for every series, or None to backfill."""
    ends = []
    for kind in KINDS:
        last = await _last(hass, statistic_id(subentry_id, kind))
        if last is None:
            return None
        ends.append(last[0] + timedelta(hours=1))
    return min(ends)


async def async_import(
    hass: HomeAssistant,
    subentry_id: str,
    name: str,
    priced: list[tuple[datetime, dict[str, float]]],
) -> None:
    """Append priced hours to the device's statistics, each series from its own last hour."""
    last = {kind: await _last(hass, statistic_id(subentry_id, kind)) for kind in KINDS}
    sums = {kind: (value[1] if value else 0.0) for kind, value in last.items()}
    rows: dict[str, list[StatisticData]] = {kind: [] for kind in KINDS}
    for hour, values in priced:
        for kind, value in values.items():
            if last[kind] is not None and hour <= last[kind][0]:
                continue
            sums[kind] += value
            rows[kind].append(StatisticData(start=hour, state=value, sum=sums[kind]))
    _write(hass, subentry_id, name, rows)


async def async_rewrite(
    hass: HomeAssistant,
    base: str,
    name: str,
    priced: list[tuple[datetime, dict[str, float]]],
) -> None:
    """Write priced hours, replacing any already there from the first one on.

    `priced` must cover every hour from its first to the end, so the running
    sums stay continuous.
    """
    if not priced:
        return
    first = priced[0][0]
    sums = {kind: await _sum_before(hass, statistic_id(base, kind), first) for kind in KINDS}
    rows: dict[str, list[StatisticData]] = {kind: [] for kind in KINDS}
    for hour, values in priced:
        for kind, value in values.items():
            sums[kind] += value
            rows[kind].append(StatisticData(start=hour, state=value, sum=sums[kind]))
    _write(hass, base, name, rows)


async def _sum_before(hass: HomeAssistant, stat_id: str, hour: datetime) -> float:
    """The running sum at the end of the hour before `hour`."""
    result = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, hour - timedelta(hours=1), hour, {stat_id}, "hour", None, {"sum"}
    )
    rows = result.get(stat_id)
    if rows:
        return rows[-1]["sum"] or 0.0
    # Nothing just before: either the series is new or it ends earlier.
    last = await _last(hass, stat_id)
    return last[1] if last and last[0] < hour else 0.0


def _write(hass: HomeAssistant, base: str, name: str, rows: dict[str, list[StatisticData]]) -> None:
    for kind, data in rows.items():
        if not data:
            continue
        label, unit = KINDS[kind]
        async_add_external_statistics(
            hass,
            StatisticMetaData(
                mean_type=StatisticMeanType.NONE,
                has_sum=True,
                name=f"{name} {label}",
                source=DOMAIN,
                statistic_id=statistic_id(base, kind),
                unit_class=EnergyConverter.UNIT_CLASS if unit == UnitOfEnergy.KILO_WATT_HOUR else None,
                unit_of_measurement=unit,
            ),
            data,
        )
