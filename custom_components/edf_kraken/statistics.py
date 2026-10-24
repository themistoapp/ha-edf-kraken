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


def _metadata(point: MeterPoint, kind: str) -> StatisticMetaData:
    label = "export" if point.is_export else "consumption"
    if kind == "energy":
        name, unit, unit_class = f"EDF electricity {label} {point.mpan}", UnitOfEnergy.KILO_WATT_HOUR, EnergyConverter.UNIT_CLASS
    else:
        name, unit, unit_class = f"EDF electricity {label} cost {point.mpan}", "GBP", None
    return StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=name,
        source=DOMAIN,
        statistic_id=statistic_id(point, kind),
        unit_class=unit_class,
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
    """The end of the last hour already imported for this meter."""
    last = await _last(hass, statistic_id(point, "energy"))
    return last[0] + timedelta(hours=1) if last else None


async def async_import_statistics(
    hass: HomeAssistant,
    point: MeterPoint,
    readings: list[Reading],
    rate_for: Callable[[datetime], float | None],
) -> None:
    """Add complete hours from half-hourly readings to the energy and cost statistics.

    rate_for returns the unit rate in pence/kWh at a moment, or None if unknown.
    """
    hours: dict[datetime, list[Reading]] = {}
    for reading in readings:
        hour = reading[0].astimezone(dt_util.UTC).replace(minute=0, second=0, microsecond=0)
        hours.setdefault(hour, []).append(reading)

    last_energy = await _last(hass, statistic_id(point, "energy"))
    last_cost = await _last(hass, statistic_id(point, "cost"))
    energy_sum = last_energy[1] if last_energy else 0.0
    cost_sum = last_cost[1] if last_cost else 0.0
    after = last_energy[0] if last_energy else None

    energy_stats: list[StatisticData] = []
    cost_stats: list[StatisticData] = []
    ordered = sorted(hours)
    for hour in ordered:
        if after is not None and hour <= after:
            continue
        slots = hours[hour]
        # Hold back the newest hour until both half hours are in, so it never
        # needs revising. A gap further back is a missing reading; move past it.
        if len(slots) < 2 and hour == ordered[-1]:
            break
        kwh = sum(s[2] for s in slots)
        cost = 0.0
        for start, _end, slot_kwh in slots:
            rate = rate_for(start)
            cost += slot_kwh * (rate or 0.0) / 100
        energy_sum += kwh
        cost_sum += cost
        energy_stats.append(StatisticData(start=hour, state=kwh, sum=energy_sum))
        cost_stats.append(StatisticData(start=hour, state=cost, sum=cost_sum))

    if energy_stats:
        async_add_external_statistics(hass, _metadata(point, "energy"), energy_stats)
        async_add_external_statistics(hass, _metadata(point, "cost"), cost_stats)
