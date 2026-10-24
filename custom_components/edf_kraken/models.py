"""Parsed views of the API responses."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from homeassistant.util import dt as dt_util


@dataclass(frozen=True)
class Tariff:
    tariff_code: str
    product_code: str
    display_name: str
    standing_charge: float | None  # pence/day inc VAT


@dataclass(frozen=True)
class Agreement:
    valid_from: datetime
    valid_to: datetime | None
    tariff: Tariff | None

    def covers(self, moment: datetime) -> bool:
        return self.valid_from <= moment and (self.valid_to is None or moment < self.valid_to)


@dataclass
class MeterPoint:
    mpan: str
    is_export: bool
    serial: str | None
    device_id: str | None
    address: str | None
    agreements: list[Agreement] = field(default_factory=list)

    def agreement_at(self, moment: datetime) -> Agreement | None:
        return next((a for a in self.agreements if a.covers(moment)), None)

    @property
    def tariff(self) -> Tariff | None:
        agreement = self.agreement_at(dt_util.utcnow())
        return agreement.tariff if agreement else None


@dataclass
class AccountData:
    number: str
    balance: int  # pence; positive means in credit
    meter_points: list[MeterPoint]
    points: dict[str, Any] | None


@dataclass(frozen=True)
class Rate:
    start: datetime
    end: datetime | None
    value: float  # pence/kWh inc VAT


def _dt(value: str | None) -> datetime | None:
    return dt_util.parse_datetime(value) if value else None


def parse_account(data: dict[str, Any]) -> AccountData:
    account = data["account"]
    meter_points: list[MeterPoint] = []
    for prop in account.get("properties") or []:
        for point in prop.get("electricityMeterPoints") or []:
            meters = point.get("meters") or []
            # Prefer the smart meter; that is the one with half-hourly data.
            meter = next((m for m in meters if m.get("smartDevices")), meters[0] if meters else None)
            devices = (meter or {}).get("smartDevices") or []
            agreements = []
            for agreement in point.get("agreements") or []:
                start = _dt(agreement.get("validFrom"))
                if start is None:
                    continue
                raw = agreement.get("tariff") or {}
                tariff = (
                    Tariff(
                        tariff_code=raw["tariffCode"],
                        product_code=raw.get("productCode") or "",
                        display_name=raw.get("displayName") or raw.get("fullName") or raw["tariffCode"],
                        standing_charge=raw.get("standingCharge"),
                    )
                    if raw.get("tariffCode")
                    else None
                )
                agreements.append(Agreement(start, _dt(agreement.get("validTo")), tariff))
            agreements.sort(key=lambda a: a.valid_from)
            meter_points.append(
                MeterPoint(
                    mpan=point["mpan"],
                    is_export=point.get("direction") == "EXPORT",
                    serial=(meter or {}).get("serialNumber"),
                    device_id=devices[0]["deviceId"] if devices else None,
                    address=prop.get("address"),
                    agreements=agreements,
                )
            )
    return AccountData(
        number=account["number"],
        balance=account.get("balance") or 0,
        meter_points=meter_points,
        points=(data.get("rateLimitInfo") or {}).get("pointsAllowanceRateLimit"),
    )


def parse_rates(rows: list[dict[str, Any]], payment_method: str = "DIRECT_DEBIT") -> list[Rate]:
    """Rates in time order.

    Some tariffs (Standard Variable) list a Direct Debit and a non-Direct Debit
    price for the same period; only one of them applies, or the two would look
    like peak and off-peak bands.
    """
    if any(r.get("payment_method") == payment_method for r in rows):
        rows = [r for r in rows if r.get("payment_method") in (payment_method, None)]
    rates = [
        Rate(_dt(r["valid_from"]), _dt(r.get("valid_to")), float(r["value_inc_vat"]))
        for r in rows
        if r.get("valid_from") and r.get("value_inc_vat") is not None
    ]
    rates.sort(key=lambda r: r.start)
    return rates


PEAK = "peak"
OFF_PEAK = "off_peak"


def band_at(rates: list[Rate], moment: datetime) -> str | None:
    """PEAK or OFF_PEAK at a moment, or None on a single-rate tariff.

    Off-peak is the cheapest price among the rates given, which for two-rate
    tariffs like Go Electric is the overnight band.
    """
    values = {r.value for r in rates}
    rate = rate_at(rates, moment)
    if rate is None or len(values) < 2:
        return None
    return OFF_PEAK if rate.value == min(values) else PEAK


def merge_rates(rates: list[Rate]) -> list[Rate]:
    """Join back-to-back periods that have the same price.

    The API splits some bands at arbitrary points (22:00-23:00 and 23:00-05:00
    at the same off-peak price), which would otherwise show as a rate change.
    """
    merged: list[Rate] = []
    for rate in rates:
        last = merged[-1] if merged else None
        if last and last.end == rate.start and last.value == rate.value:
            merged[-1] = Rate(last.start, rate.end, last.value)
        else:
            merged.append(rate)
    return merged


def rate_at(rates: list[Rate], moment: datetime) -> Rate | None:
    for rate in reversed(rates):
        if rate.start <= moment and (rate.end is None or moment < rate.end):
            return rate
    return None
