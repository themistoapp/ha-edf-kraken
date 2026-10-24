"""Shared entity helpers."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo

from .const import DOMAIN, MANUFACTURER
from .models import MeterPoint


def account_device(account_number: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, account_number)},
        name=f"EDF account {account_number}",
        manufacturer=MANUFACTURER,
        entry_type=DeviceEntryType.SERVICE,
    )


def meter_device(account_number: str, point: MeterPoint) -> DeviceInfo:
    label = "export" if point.is_export else "electricity"
    return DeviceInfo(
        identifiers={(DOMAIN, f"electricity_{point.mpan}")},
        name=f"EDF {label} meter {point.mpan}",
        manufacturer=MANUFACTURER,
        model="Smart meter" if point.device_id else "Meter",
        serial_number=point.serial,
        via_device=(DOMAIN, account_number),
    )
