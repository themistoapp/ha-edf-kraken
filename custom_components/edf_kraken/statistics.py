"""Long-term statistics for the Energy dashboard.

Half-hourly smart meter data arrives hours or days late, so a sensor's state
can't place it at the right time. Instead each meter gets two external
statistics, energy (kWh) and cost (GBP), written hour by hour with their real
timestamps.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter

from .const import DOMAIN
from .models import MeterPoint

Reading = tuple[datetime, datetime, float]


def statistic_id(point: MeterPoint, kind: str) -> str:
    direction = "export" if point.is_export else "consumption"
    return f"{DOMAIN}:electricity_{point.mpan}_{direction}_{kind}".lower()


_KINDS = {
    "energy": ("", UnitOfEnergy.KILO_WATT_HOUR),
    "cost": ("cost ", "GBP"),
    "peak_energy": ("peak ", UnitOfEnergy.KILO_WATT_HOUR),
    "peak_cost": ("peak cost ", "GBP"),
    "off_peak_energy": ("off-peak ", UnitOfEnergy.KILO_WATT_HOUR),
    "off_peak_cost": ("off-peak cost ", "GBP"),
}
BAND_KINDS = ("peak_energy", "peak_cost", "off_peak_energy", "off_peak_cost")


def _metadata(point: MeterPoint, kind: str) -> StatisticMetaData:
    label = "export" if point.is_export else "consumption"
    prefix, unit = _KINDS[kind]
    return StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=f"EDF electricity {label} {prefix}{point.mpan}",
        source=DOMAIN,
        statistic_id=statistic_id(point, kind),
        unit_class=EnergyConverter.UNIT_CLASS if unit == UnitOfEnergy.KILO_WATT_HOUR else None,
        unit_of_measurement=unit,
    )


async def _last(hass: HomeAssistant, stat_id: str) -> tuple[datetime, float] | None:
    result = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, stat_id, True, {"sum"}
    )
    rows = result.get(stat_id)
    if not rows:
        return None
    return dt_util.utc_from_timestamp(rows[0]["start"]), rows[0]["sum"] or 0.0


async def async_last_imported(hass: HomeAssistant, point: MeterPoint) -> datetime | None:
    """The end of the last hour imported for every series of this meter.

    None if a series has never been written, so it gets the full backfill.
    """
    ends = []
    for kind in _KINDS:
        last = await _last(hass, statistic_id(point, kind))
        if last is None:
            return None
        ends.append(last[0] + timedelta(hours=1))
    return min(ends)


async def async_import_statistics(
    hass: HomeAssistant,
    point: MeterPoint,
    readings: list[Reading],
    rate_for: Callable[[datetime], float | None],
    band_for: Callable[[datetime], str | None],
) -> None:
    """Add complete hours from half-hourly readings to the meter's statistics.

    rate_for returns the unit rate in pence/kWh at a moment (None if unknown);
    band_for returns "peak", "off_peak" or None on a single-rate tariff. Each
    series carries on from its own last hour, so a series added later is
    backfilled without rewriting the others.
    """
    hours: dict[datetime, list[Reading]] = {}
    for reading in readings:
        hour = reading[0].astimezone(dt_util.UTC).replace(minute=0, second=0, microsecond=0)
        hours.setdefault(hour, []).append(reading)

    last = {kind: await _last(hass, statistic_id(point, kind)) for kind in _KINDS}
    sums = {kind: (value[1] if value else 0.0) for kind, value in last.items()}
    stats: dict[str, list[StatisticData]] = {kind: [] for kind in _KINDS}

    ordered = sorted(hours)
    for hour in ordered:
        slots = hours[hour]
        # Hold back the newest hour until both half hours are in, so it never
        # needs revising. A gap further back is a missing reading; move past it.
        if len(slots) < 2 and hour == ordered[-1]:
            break
        values = dict.fromkeys(_KINDS, 0.0)
        for start, _end, kwh in slots:
            cost = kwh * (rate_for(start) or 0.0) / 100
            values["energy"] += kwh
            values["cost"] += cost
            band = band_for(start)
            # Single-rate time counts as peak, so peak + off-peak always equals the total.
            prefix = "off_peak" if band == "off_peak" else "peak"
            values[f"{prefix}_energy"] += kwh
            values[f"{prefix}_cost"] += cost
        for kind, value in values.items():
            if last[kind] is not None and hour <= last[kind][0]:
                continue
            sums[kind] += value
            stats[kind].append(StatisticData(start=hour, state=value, sum=sums[kind]))

    for kind, rows in stats.items():
        if rows:
            async_add_external_statistics(hass, _metadata(point, kind), rows)
