"""Canned API responses shaped like the live EDF Kraken API."""

from datetime import UTC, datetime, timedelta

ACCOUNT_NUMBER = "A-1234ABCD"
MPAN = "1900000000001"
SERIAL = "22L0000001"
DEVICE_ID = "00-11-22-33-44-55-66-77"
TARIFF_CODE = "E-1R-EDF_EV_FIX_GOELEC_12M_V3_HH-C"
PRODUCT_CODE = "EDF_EV_FIX_GOELEC_12M_V3_HH"


def account_response():
    return {
        "account": {
            "number": ACCOUNT_NUMBER,
            "balance": 12345,
            "properties": [
                {
                    "id": "1",
                    "address": "1 Test Street",
                    "postcode": "AB1 2CD",
                    "electricityMeterPoints": [
                        {
                            "mpan": MPAN,
                            "direction": "IMPORT",
                            "agreements": [
                                {
                                    "validFrom": "2026-09-20T00:00:00+01:00",
                                    "validTo": "2027-09-20T00:00:00+01:00",
                                    "tariff": {
                                        "tariffCode": TARIFF_CODE,
                                        "productCode": PRODUCT_CODE,
                                        "displayName": "Go Electric 12m v3",
                                        "fullName": "Go Electric 12m v3 HH",
                                        "standingCharge": 48.07,
                                    },
                                }
                            ],
                            "meters": [
                                {"serialNumber": SERIAL, "smartDevices": [{"deviceId": DEVICE_ID}]}
                            ],
                        }
                    ],
                }
            ],
        },
        "rateLimitInfo": {
            "pointsAllowanceRateLimit": {
                "limit": 50000,
                "remainingPoints": 49900,
                "usedPoints": 100,
                "ttl": 1792850400,
                "isBlocked": False,
            }
        },
    }


def unit_rates(period_from: datetime, period_to: datetime):
    """Go Electric: 6.66p from 23:00 to 06:00 UK time, 37.207p otherwise."""
    rows = []
    day = period_from.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    while day < period_to:
        # BST in October: 22:00Z-05:00Z is 23:00-06:00 local.
        peak_start = day + timedelta(hours=5)
        peak_end = day + timedelta(hours=22)
        rows.append((day - timedelta(hours=2), peak_start, 6.66))
        rows.append((peak_start, peak_end, 37.207))
        day += timedelta(days=1)
    return [
        {
            "value_exc_vat": v,
            "value_inc_vat": v,
            "valid_from": s.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "valid_to": e.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "payment_method": None,
        }
        for s, e, v in rows
        if e > period_from and s < period_to
    ]


def standing_charges():
    return [
        {
            "value_exc_vat": 48.07,
            "value_inc_vat": 48.07,
            "valid_from": "2026-09-17T23:00:00Z",
            "valid_to": None,
            "payment_method": None,
        }
    ]


def consumption(period_from: datetime, period_to: datetime, kwh: float = 0.25):
    rows = []
    t = period_from.astimezone(UTC)
    while t + timedelta(minutes=30) <= period_to:
        rows.append(
            {
                "consumption": kwh,
                "interval_start": t.isoformat(),
                "interval_end": (t + timedelta(minutes=30)).isoformat(),
            }
        )
        t += timedelta(minutes=30)
    return rows
