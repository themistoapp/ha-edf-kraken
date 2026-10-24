"""Split a device's own usage into peak and off-peak energy and cost.

The source is any Home Assistant power sensor (W/kW) or cumulative energy
sensor (Wh/kWh), e.g. an EV charger or a hot tub. Energy is attributed to
the tariff band in force when it was used, splitting at rate changes, and
priced at that moment's unit rate. Nothing here calls the EDF API; it reuses
the cached rates.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from datetime import datetime
from typing import Any

from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfEnergy,
    UnitOfPower,
)
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.helpers.event import async_track_state_change_event, async_track_utc_time_change
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import RatesCoordinator
from .models import OFF_PEAK, band_at, merge_rates, rate_at

POWER_UNITS = {UnitOfPower.WATT: 0.001, UnitOfPower.KILO_WATT: 1.0}
ENERGY_UNITS = {UnitOfEnergy.WATT_HOUR: 0.001, UnitOfEnergy.KILO_WATT_HOUR: 1.0}
SAVE_DELAY = 30  # seconds

TOTALS = ("peak_kwh", "off_peak_kwh", "peak_cost", "off_peak_cost")


class DeviceTracker:
    """Accumulates one device's peak/off-peak energy (kWh) and cost (GBP)."""

    def __init__(
        self,
        hass: HomeAssistant,
        rates: RatesCoordinator,
        subentry_id: str,
        source_entity_id: str,
        mpan: str,
    ) -> None:
        self.hass = hass
        self.rates = rates
        self.source = source_entity_id
        self.mpan = mpan
        self.totals = dict.fromkeys(TOTALS, 0.0)
        self._store: Store[dict[str, Any]] = Store(hass, 1, f"{DOMAIN}.tracker.{subentry_id}")
        self._listeners: list[Callable[[], None]] = []
        # Last reading: (time, value in kW or kWh, "power" | "energy").
        self._last: tuple[datetime, float, str] | None = None

    async def async_start(self) -> Callable[[], Coroutine[Any, Any, None]]:
        stored = await self._store.async_load() or {}
        for key in TOTALS:
            self.totals[key] = float(stored.get(key, 0.0))
        # Start from the source's current state; time Home Assistant was down isn't counted.
        self._last = self._reading(self.hass.states.get(self.source), dt_util.utcnow())
        unsubs = [
            async_track_state_change_event(self.hass, [self.source], self._state_changed),
            # Close off power intervals at each half hour so bands and states stay current.
            async_track_utc_time_change(self.hass, self._tick, minute=(0, 30), second=0),
        ]

        async def stop() -> None:
            for unsub in unsubs:
                unsub()
            self._flush(dt_util.utcnow())
            await self._store.async_save(self._data())

        return stop

    @staticmethod
    async def async_remove_storage(hass: HomeAssistant, subentry_id: str) -> None:
        await Store(hass, 1, f"{DOMAIN}.tracker.{subentry_id}").async_remove()

    @callback
    def add_listener(self, update: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(update)
        return lambda: self._listeners.remove(update)

    def _data(self) -> dict[str, Any]:
        return dict(self.totals)

    @staticmethod
    def _reading(state: State | None, now: datetime) -> tuple[datetime, float, str] | None:
        if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return None
        unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
        try:
            value = float(state.state)
        except ValueError:
            return None
        if unit in POWER_UNITS:
            return now, value * POWER_UNITS[unit], "power"
        if unit in ENERGY_UNITS:
            return now, value * ENERGY_UNITS[unit], "energy"
        return None

    @callback
    def _state_changed(self, event: Event[EventStateChangedData]) -> None:
        now = event.time_fired
        new = self._reading(event.data["new_state"], now)
        last = self._last
        if last and last[2] == "power":
            self._flush(now)
        elif last and new and last[2] == "energy" == new[2]:
            delta = new[1] - last[1]
            if delta > 0:  # a drop means the meter reset; take the new value as the baseline
                self._add_energy(last[0], now, delta)
        self._last = new
        self._changed()

    @callback
    def _tick(self, now: datetime) -> None:
        if self._last and self._last[2] == "power":
            self._flush(now)
            self._changed()

    def _flush(self, now: datetime) -> None:
        """Credit the power reading held since the last update, up to now."""
        if not self._last or self._last[2] != "power":
            return
        start, kw, _ = self._last
        # An unchanged state still holds: Home Assistant doesn't repeat identical values.
        if now > start and kw > 0:
            self._add_energy(start, now, kw * (now - start).total_seconds() / 3600)
        self._last = (now, kw, "power")

    def _add_energy(self, start: datetime, end: datetime, kwh: float) -> None:
        """Spread kWh evenly over [start, end), splitting where the rate changes."""
        data = (self.rates.data or {}).get(self.mpan)
        rates = merge_rates(data.unit_rates) if data else []
        span = (end - start).total_seconds()
        if span <= 0:
            # Two updates at the same instant: price it at that moment.
            rate = rate_at(rates, start)
            band = "off_peak" if band_at(rates, start) == OFF_PEAK else "peak"
            self.totals[f"{band}_kwh"] += kwh
            if rate is not None:
                self.totals[f"{band}_cost"] += kwh * rate.value / 100
            return
        cursor = start
        while cursor < end:
            rate = rate_at(rates, cursor)
            if rate is not None:
                segment_end = min(end, rate.end or end)
            else:
                later = [r.start for r in rates if r.start > cursor]
                segment_end = min([end, *later])
            share = kwh * (segment_end - cursor).total_seconds() / span
            band = "off_peak" if band_at(rates, cursor) == OFF_PEAK else "peak"
            self.totals[f"{band}_kwh"] += share
            if rate is not None:
                self.totals[f"{band}_cost"] += share * rate.value / 100
            cursor = segment_end

    def _changed(self) -> None:
        self._store.async_delay_save(self._data, SAVE_DELAY)
        for update in list(self._listeners):
            update()

