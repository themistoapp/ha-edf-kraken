"""Binary sensors for EDF Energy (Kraken)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_utc_time_change
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import EdfConfigEntry
from .coordinator import RatesCoordinator
from .entity import meter_device
from .models import MeterPoint
from .sensor import rate_view


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EdfConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    data = entry.runtime_data
    number = data.account.account_number
    async_add_entities(
        OffPeakSensor(data.rates, number, point)
        for point in data.account.data.meter_points
        if not point.is_export
    )


class OffPeakSensor(CoordinatorEntity[RatesCoordinator], BinarySensorEntity):
    """On while the cheapest rate in the cached window applies.

    For Go Electric that is 23:00-06:00. On a single-rate tariff it stays off.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "off_peak"

    def __init__(self, coordinator: RatesCoordinator, number: str, point: MeterPoint) -> None:
        super().__init__(coordinator)
        self._mpan = point.mpan
        self._attr_unique_id = f"{number}_electricity_{point.mpan}_off_peak"
        self._attr_device_info = meter_device(number, point)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_utc_time_change(self.hass, self._tick, minute=(0, 30), second=0)
        )

    @callback
    def _tick(self, _now: datetime) -> None:
        self.async_write_ha_state()

    @property
    def _view(self):
        return rate_view((self.coordinator.data or {}).get(self._mpan))

    @property
    def available(self) -> bool:
        return super().available and self._view is not None

    @property
    def is_on(self) -> bool | None:
        view = self._view
        if not view or not view.current:
            return None
        values = {r.value for r in view.rates}
        return len(values) > 1 and view.current.value == min(values)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        view = self._view
        if not view:
            return None
        cheapest = min((r.value for r in view.rates), default=None)
        upcoming = [r for r in view.rates if r.value == cheapest and r.start > (view.current.start if view.current else r.start)]
        nxt = upcoming[0] if upcoming else None
        return {
            "current_period_end": view.current.end.isoformat() if view.current and view.current.end else None,
            "next_off_peak_start": nxt.start.isoformat() if nxt else None,
            "next_off_peak_end": nxt.end.isoformat() if nxt and nxt.end else None,
        }
