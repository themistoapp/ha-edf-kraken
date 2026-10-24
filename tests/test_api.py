"""Tests for the API client's token handling and rate-limit behaviour."""

import base64
import json
import time

import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMockResponse

from custom_components.edf_kraken import api
from custom_components.edf_kraken.api import EdfAuthError, EdfClient, EdfRateLimitError
from custom_components.edf_kraken.const import GRAPHQL_URL

from .fixtures import ACCOUNT_NUMBER, account_response


def _jwt(exp_in: int = 3600) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": int(time.time()) + exp_in}).encode()).decode().rstrip("=")
    return f"header.{payload}.sig"


def _error(code: str) -> dict:
    return {"errors": [{"message": "x", "extensions": {"errorCode": code, "errorDescription": code}}], "data": None}


@pytest.fixture(autouse=True)
def no_spacing(monkeypatch):
    monkeypatch.setattr(api, "MIN_REQUEST_SPACING", 0)


class FakeApi:
    """Routes GraphQL requests by operation and records what was sent."""

    def __init__(self) -> None:
        self.token_inputs: list[dict] = []
        self.auth_headers: list[str | None] = []
        self.account_replies: list[dict] = []

    async def __call__(self, method, url, data):
        if "obtainKrakenToken" in data["query"]:
            self.token_inputs.append(data["variables"]["input"])
            body = {"data": {"obtainKrakenToken": {"token": _jwt(), "refreshToken": "r1", "refreshExpiresIn": int(time.time()) + 86400}}}
        else:
            body = self.account_replies.pop(0) if self.account_replies else {"data": account_response()}
        return AiohttpClientMockResponse(method, url, json=body)


@pytest.fixture
def fake(aioclient_mock):
    fake = FakeApi()
    aioclient_mock.post(GRAPHQL_URL, side_effect=fake)
    return fake


async def test_token_reused_between_requests(hass, aioclient_mock, fake):
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    client = EdfClient(async_get_clientsession(hass), api_key="sk_live_test")
    await client.async_get_account(ACCOUNT_NUMBER)
    await client.async_get_account(ACCOUNT_NUMBER)
    assert fake.token_inputs == [{"APIKey": "sk_live_test"}]
    headers = [c[3] for c in aioclient_mock.mock_calls if "obtainKrakenToken" not in c[2]["query"]]
    assert all(h and h["Authorization"].startswith("header.") for h in headers)


async def test_expired_token_uses_refresh_token(hass, aioclient_mock, fake):
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    client = EdfClient(async_get_clientsession(hass), api_key="sk_live_test")
    await client.authenticate()
    fake.account_replies = [_error("KT-CT-1124")]
    data = await client.async_get_account(ACCOUNT_NUMBER)
    assert data["account"]["number"] == ACCOUNT_NUMBER
    assert fake.token_inputs == [{"APIKey": "sk_live_test"}, {"refreshToken": "r1"}]


async def test_rate_limit_pauses_all_requests(hass, aioclient_mock, fake):
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    client = EdfClient(async_get_clientsession(hass), api_key="sk_live_test")
    fake.account_replies = [_error("KT-CT-1199")]
    with pytest.raises(EdfRateLimitError):
        await client.async_get_account(ACCOUNT_NUMBER)
    calls = len(aioclient_mock.mock_calls)
    with pytest.raises(EdfRateLimitError):
        await client.async_get_account(ACCOUNT_NUMBER)
    assert len(aioclient_mock.mock_calls) == calls  # nothing sent while paused


async def test_bad_api_key(hass, aioclient_mock):
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    aioclient_mock.post(GRAPHQL_URL, json=_error("KT-CT-1139"))
    client = EdfClient(async_get_clientsession(hass), api_key="nope")
    with pytest.raises(EdfAuthError):
        await client.authenticate()


def test_direct_debit_price_only():
    """Standard Variable lists both payment methods' prices; they aren't two bands."""
    from datetime import datetime

    from homeassistant.util import dt as dt_util

    from custom_components.edf_kraken.models import band_at, parse_rates

    rows = [
        {"value_inc_vat": 27.954, "valid_from": "2026-09-30T23:00:00Z", "valid_to": None, "payment_method": "NON_DIRECT_DEBIT"},
        {"value_inc_vat": 26.466, "valid_from": "2026-09-30T23:00:00Z", "valid_to": None, "payment_method": "DIRECT_DEBIT"},
    ]
    rates = parse_rates(rows)
    assert [r.value for r in rates] == [26.466]
    assert band_at(rates, datetime(2026, 10, 21, 2, tzinfo=dt_util.UTC)) is None
