"""Sensors for EDF Energy (Kraken)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_utc_time_change
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import EdfConfigEntry
from .coordinator import (
    AccountCoordinator,
    ConsumptionCoordinator,
    ConsumptionSummary,
    RatesCoordinator,
    TariffRates,
    TelemetryCoordinator,
)
from .entity import account_device, meter_device
from .models import AccountData, MeterPoint, Rate, merge_rates, rate_at

GBP_PER_KWH = "GBP/kWh"
GBP_PER_DAY = "GBP/day"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EdfConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    data = entry.runtime_data
    account = data.account
    number = account.account_number
    entities: list[SensorEntity] = [
        AccountSensor(account, desc) for desc in ACCOUNT_SENSORS
    ]
    for point in account.data.meter_points:
        entities += [MeterPointSensor(account, point, desc) for desc in TARIFF_SENSORS]
        if not point.is_export:
            entities += [RateSensor(data.rates, number, point, desc) for desc in RATE_SENSORS]
        if point.serial:
            entities += [
                ConsumptionSensor(data.consumption, number, point, desc)
                for desc in CONSUMPTION_SENSORS
            ]
        telemetry = data.telemetry.get(point.device_id or "")
        if telemetry and not point.is_export:
            entities += [
                TelemetrySensor(telemetry, number, point, desc) for desc in TELEMETRY_SENSORS
            ]
    async_add_entities(entities)


# -- account -------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class AccountSensorDescription(SensorEntityDescription):
    value_fn: Callable[[AccountData], Any]
    attrs_fn: Callable[[AccountData], dict[str, Any]] | None = None


def _points_attrs(data: AccountData) -> dict[str, Any]:
    points = data.points or {}
    ttl = points.get("ttl")
    return {
        "limit": points.get("limit"),
        "used": points.get("usedPoints"),
        "blocked": points.get("isBlocked"),
        "resets_at": dt_util.utc_from_timestamp(ttl).isoformat() if ttl else None,
    }


ACCOUNT_SENSORS = (
    AccountSensorDescription(
        key="balance",
        translation_key="balance",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="GBP",
        suggested_display_precision=2,
        value_fn=lambda d: d.balance / 100,
    ),
    AccountSensorDescription(
        key="api_points_remaining",
        translation_key="api_points_remaining",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        value_fn=lambda d: (d.points or {}).get("remainingPoints"),
        attrs_fn=_points_attrs,
    ),
)


class AccountSensor(CoordinatorEntity[AccountCoordinator], SensorEntity):
    _attr_has_entity_name = True
    entity_description: AccountSensorDescription

    def __init__(self, coordinator: AccountCoordinator, description: AccountSensorDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        number = coordinator.account_number
        self._attr_unique_id = f"{number}_{description.key}"
        self._attr_device_info = account_device(number)

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        fn = self.entity_description.attrs_fn
        return fn(self.coordinator.data) if fn else None


# -- meter point (from the account query) ----------------------------------


TARIFF_SENSORS = (
    SensorEntityDescription(
        key="tariff",
        translation_key="tariff",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


class MeterPointEntity:
    _attr_has_entity_name = True

    def _setup(self, number: str, point: MeterPoint, key: str) -> None:
        self._point = point
        self._attr_unique_id = f"{number}_electricity_{point.mpan}_{key}"
        self._attr_device_info = meter_device(number, point)


class MeterPointSensor(MeterPointEntity, CoordinatorEntity[AccountCoordinator], SensorEntity):
    def __init__(self, coordinator: AccountCoordinator, point: MeterPoint, description) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._setup(coordinator.account_number, point, description.key)

    def _current_point(self) -> MeterPoint | None:
        return next(
            (p for p in self.coordinator.data.meter_points if p.mpan == self._point.mpan), None
        )

    @property
    def native_value(self) -> str | None:
        point = self._current_point()
        tariff = point.tariff if point else None
        return tariff.display_name if tariff else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        point = self._current_point()
        if not point:
            return None
        agreement = point.agreement_at(dt_util.utcnow())
        tariff = agreement.tariff if agreement else None
        return {
            "mpan": point.mpan,
            "meter_serial_number": point.serial,
            "tariff_code": tariff.tariff_code if tariff else None,
            "product_code": tariff.product_code if tariff else None,
            "agreement_start": agreement.valid_from.isoformat() if agreement else None,
            "agreement_end": agreement.valid_to.isoformat() if agreement and agreement.valid_to else None,
        }


# -- rates -----------------------------------------------------------------


@dataclass
class RateView:
    rates: list[Rate]
    current: Rate | None
    next: Rate | None
    standing: Rate | None


def rate_view(data: TariffRates | None) -> RateView | None:
    if data is None:
        return None
    now = dt_util.utcnow()
    rates = merge_rates(data.unit_rates)
    current = rate_at(rates, now)
    upcoming = next((r for r in rates if r.start > now), None)
    return RateView(rates, current, upcoming, rate_at(data.standing_charges, now))


def _rate_attrs(rate: Rate | None) -> dict[str, Any]:
    if rate is None:
        return {}
    return {
        "valid_from": rate.start.isoformat(),
        "valid_to": rate.end.isoformat() if rate.end else None,
        "pence_per_kwh": rate.value,
    }


@dataclass(frozen=True, kw_only=True)
class RateSensorDescription(SensorEntityDescription):
    value_fn: Callable[[RateView], Any]
    attrs_fn: Callable[[RateView], dict[str, Any]] | None = None


RATE_SENSORS = (
    RateSensorDescription(
        key="current_rate",
        translation_key="current_rate",
        native_unit_of_measurement=GBP_PER_KWH,
        suggested_display_precision=4,
        value_fn=lambda v: round(v.current.value / 100, 5) if v.current else None,
        attrs_fn=lambda v: {
            **_rate_attrs(v.current),
            "rates": [
                {"start": r.start.isoformat(), "end": r.end.isoformat() if r.end else None, "value": round(r.value / 100, 5)}
                for r in v.rates
            ],
        },
    ),
    RateSensorDescription(
        key="next_rate",
        translation_key="next_rate",
        native_unit_of_measurement=GBP_PER_KWH,
        suggested_display_precision=4,
        value_fn=lambda v: round(v.next.value / 100, 5) if v.next else None,
        attrs_fn=lambda v: _rate_attrs(v.next),
    ),
    RateSensorDescription(
        key="next_rate_change",
        translation_key="next_rate_change",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda v: v.next.start if v.next else None,
    ),
    RateSensorDescription(
        key="standing_charge",
        translation_key="standing_charge",
        native_unit_of_measurement=GBP_PER_DAY,
        suggested_display_precision=4,
        value_fn=lambda v: round(v.standing.value / 100, 5) if v.standing else None,
    ),
)


class RateSensor(MeterPointEntity, CoordinatorEntity[RatesCoordinator], SensorEntity):
    """Rate sensors recompute on every half-hour boundary from cached rates."""

    entity_description: RateSensorDescription
    _unrecorded_attributes = frozenset({"rates"})

    def __init__(self, coordinator: RatesCoordinator, number: str, point: MeterPoint, description) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._setup(number, point, description.key)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_utc_time_change(self.hass, self._tick, minute=(0, 30), second=0)
        )

    @callback
    def _tick(self, _now: datetime) -> None:
        self.async_write_ha_state()

    @property
    def _view(self) -> RateView | None:
        return rate_view((self.coordinator.data or {}).get(self._point.mpan))

    @property
    def available(self) -> bool:
        return super().available and self._view is not None

    @property
    def native_value(self) -> Any:
        view = self._view
        return self.entity_description.value_fn(view) if view else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        view = self._view
        fn = self.entity_description.attrs_fn
        return fn(view) if view and fn else None


# -- consumption -------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class ConsumptionSensorDescription(SensorEntityDescription):
    value_fn: Callable[[ConsumptionSummary], Any]
    attrs_fn: Callable[[ConsumptionSummary], dict[str, Any]] | None = None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


CONSUMPTION_SENSORS = (
    ConsumptionSensorDescription(
        key="previous_day_consumption",
        translation_key="previous_day_consumption",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=lambda s: s.previous_day_kwh,
        attrs_fn=lambda s: {"date": s.previous_day.date().isoformat() if s.previous_day else None},
    ),
    ConsumptionSensorDescription(
        key="previous_day_cost",
        translation_key="previous_day_cost",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="GBP",
        suggested_display_precision=2,
        value_fn=lambda s: (
            round(s.previous_day_energy_cost + (s.previous_day_standing_charge or 0), 2)
            if s.previous_day_energy_cost is not None
            else None
        ),
        attrs_fn=lambda s: {
            "date": s.previous_day.date().isoformat() if s.previous_day else None,
            "energy_cost": s.previous_day_energy_cost,
            "standing_charge": s.previous_day_standing_charge,
        },
    ),
    ConsumptionSensorDescription(
        key="latest_reading",
        translation_key="latest_reading",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=3,
        value_fn=lambda s: s.latest_kwh,
        attrs_fn=lambda s: {"interval_start": _iso(s.latest_start), "interval_end": _iso(s.latest_end)},
    ),
    ConsumptionSensorDescription(
        key="data_available_to",
        translation_key="data_available_to",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.latest_end,
    ),
)


class ConsumptionSensor(MeterPointEntity, CoordinatorEntity[ConsumptionCoordinator], SensorEntity):
    entity_description: ConsumptionSensorDescription

    def __init__(self, coordinator: ConsumptionCoordinator, number: str, point: MeterPoint, description) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._setup(number, point, description.key)
        if point.is_export:
            self._attr_translation_key = f"export_{description.translation_key}"

    @property
    def _summary(self) -> ConsumptionSummary | None:
        return (self.coordinator.data or {}).get(self._point.mpan)

    @property
    def available(self) -> bool:
        return super().available and self._summary is not None

    @property
    def native_value(self) -> Any:
        summary = self._summary
        return self.entity_description.value_fn(summary) if summary else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        summary = self._summary
        fn = self.entity_description.attrs_fn
        return fn(summary) if summary and fn else None


# -- live telemetry ------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class TelemetrySensorDescription(SensorEntityDescription):
    value_fn: Callable[[dict[str, Any]], Any]


def _float(value: Any, scale: float = 1) -> float | None:
    try:
        return round(float(value) * scale, 3)
    except (TypeError, ValueError):
        return None


TELEMETRY_SENSORS = (
    TelemetrySensorDescription(
        key="demand",
        translation_key="demand",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        value_fn=lambda r: _float(r.get("demand")),
    ),
    TelemetrySensorDescription(
        key="meter_total",
        translation_key="meter_total",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=3,
        value_fn=lambda r: _float(r.get("consumption"), 0.001),  # API reports Wh
    ),
)


class TelemetrySensor(MeterPointEntity, CoordinatorEntity[TelemetryCoordinator], SensorEntity):
    entity_description: TelemetrySensorDescription

    def __init__(self, coordinator: TelemetryCoordinator, number: str, point: MeterPoint, description) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._setup(number, point, description.key)

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data is not None

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data or {})

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        data = self.coordinator.data or {}
        return {"read_at": data.get("readAt")}
