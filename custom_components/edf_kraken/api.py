"""Client for the EDF GB Kraken API.

Account data comes from the GraphQL API, half-hourly consumption from the REST
API and tariff rates from the public REST product endpoints. Kraken's usage
constraints shape the client:

- account users get 50,000 GraphQL points per hour and each request may cost
  at most 200 points, so queries are kept small and polled slowly;
- some fields carry request-specific limits that tighten every time they are
  broken (KT-CT-1199), so a rate-limit error pauses all requests rather than
  being retried;
- logins are rate limited per IP, so one JWT is reused until shortly before
  it expires and renewed with the refresh token where possible.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp

from .const import (
    GRAPHQL_URL,
    MIN_REQUEST_SPACING,
    REQUEST_TIMEOUT,
    REST_URL,
    TOKEN_EXPIRY_MARGIN,
)

_LOGGER = logging.getLogger(__name__)

# Credentials rejected or API access switched off for the account.
AUTH_FAILED_CODES = {
    "KT-CT-1138",  # wrong email/password
    "KT-CT-1139",
    "KT-CT-1141",
    "KT-CT-1142",
    "KT-CT-1143",  # Authorization header not recognised
    "KT-CT-1144",
    "KT-CT-11100",  # API key authentication unavailable
    "KT-CT-11334",  # account user API access not enabled
}
# The JWT is no longer usable; get a new one and retry once.
TOKEN_EXPIRED_CODES = {"KT-CT-1120", "KT-CT-1124", "KT-CT-1125", "KT-CT-1112"}
# The refresh token is no longer usable; log in again.
REFRESH_FAILED_CODES = {"KT-CT-1134", "KT-CT-1135"}
RATE_LIMIT_CODES = {"KT-CT-1199"}
TELEMETRY_UNAVAILABLE_CODES = {"KT-GB-4039"}

# How long to stop talking to the API after it says we sent too much.
RATE_LIMIT_PAUSE = timedelta(minutes=15)

TARIFF_FIELDS = "tariffCode productCode displayName fullName standingCharge"

TOKEN_MUTATION = """
mutation Token($input: ObtainJSONWebTokenInput!) {
  obtainKrakenToken(input: $input) {
    token
    refreshToken
    refreshExpiresIn
  }
}
"""

ACCOUNTS_QUERY = """
query Accounts {
  viewer {
    accounts {
      number
    }
  }
}
"""

ACCOUNT_QUERY = (
    """
query Account($accountNumber: String!) {
  account(accountNumber: $accountNumber) {
    number
    balance
    properties {
      id
      address
      postcode
      electricityMeterPoints {
        mpan
        direction
        agreements(includeInactive: true) {
          validFrom
          validTo
          tariff {
"""
    + "\n".join(
        f"            ... on {kind} {{ {TARIFF_FIELDS} }}"
        for kind in (
            "StandardTariff",
            "DayNightTariff",
            "ThreeRateTariff",
            "FourRateEvTariff",
            "HalfHourlyTariff",
            "PrepayTariff",
        )
    )
    + """
          }
        }
        meters {
          serialNumber
          smartDevices {
            deviceId
          }
        }
      }
    }
  }
  rateLimitInfo {
    pointsAllowanceRateLimit {
      limit
      remainingPoints
      usedPoints
      ttl
      isBlocked
    }
  }
}
"""
)

CREATE_API_KEY_MUTATION = """
mutation CreateApiKey {
  regenerateSecretKey {
    key
  }
}
"""

TELEMETRY_QUERY = """
query Telemetry($deviceId: String!) {
  smartMeterTelemetry(deviceId: $deviceId) {
    readAt
    consumption
    demand
  }
}
"""


class EdfError(Exception):
    """Base error for the EDF client."""


class EdfConnectionError(EdfError):
    """The API could not be reached or returned something unexpected."""


class EdfAuthError(EdfError):
    """The credentials were rejected."""


class EdfRateLimitError(EdfError):
    """The API asked us to slow down."""


class EdfApiError(EdfError):
    """The API returned a GraphQL error."""

    def __init__(self, code: str | None, message: str) -> None:
        super().__init__(f"{code}: {message}" if code else message)
        self.code = code


def _jwt_expiry(token: str) -> datetime:
    """Read the exp claim from a JWT without verifying it."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload))["exp"]
        return datetime.fromtimestamp(exp, tz=UTC)
    except (IndexError, KeyError, ValueError):
        # Kraken tokens last an hour; assume the worst if we can't read it.
        return datetime.now(UTC) + timedelta(minutes=30)


def _isoformat(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class EdfClient:
    """Talks to the EDF GB Kraken API on behalf of one account user."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        api_key: str | None = None,
        email: str | None = None,
        password: str | None = None,
    ) -> None:
        if not api_key and not (email and password):
            raise ValueError("An API key or an email and password are required")
        self._session = session
        self._api_key = api_key
        self._email = email
        self._password = password
        self._token: str | None = None
        self._token_expiry = datetime.min.replace(tzinfo=UTC)
        self._refresh_token: str | None = None
        self._refresh_expiry = datetime.min.replace(tzinfo=UTC)
        self._token_lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()
        self._last_request = 0.0
        self._paused_until = datetime.min.replace(tzinfo=UTC)

    # -- transport -------------------------------------------------------

    async def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """Send one request, spaced out from every other request we make."""
        now = datetime.now(UTC)
        if now < self._paused_until:
            raise EdfRateLimitError(
                f"Paused after a rate-limit response until {self._paused_until}"
            )
        async with self._request_lock:
            wait = self._last_request + MIN_REQUEST_SPACING - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                async with self._session.request(
                    method,
                    url,
                    json=json_body,
                    params=params,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                ) as resp:
                    if resp.status == 429:
                        self._pause(resp.headers.get("Retry-After"))
                        raise EdfRateLimitError("HTTP 429 from the API")
                    if resp.status in (401, 403):
                        raise EdfAuthError(f"HTTP {resp.status} from {url}")
                    if resp.status >= 400:
                        text = await resp.text()
                        raise EdfConnectionError(
                            f"HTTP {resp.status} from {url}: {text[:200]}"
                        )
                    return await resp.json(content_type=None)
            except (TimeoutError, aiohttp.ClientError) as err:
                raise EdfConnectionError(f"Error talking to {url}: {err}") from err
            finally:
                self._last_request = time.monotonic()

    def _pause(self, retry_after: str | None = None) -> None:
        pause = RATE_LIMIT_PAUSE
        if retry_after and retry_after.isdigit():
            pause = max(pause, timedelta(seconds=int(retry_after)))
        self._paused_until = datetime.now(UTC) + pause
        _LOGGER.warning(
            "EDF API rate limit hit; pausing all requests until %s",
            self._paused_until,
        )

    async def _graphql_raw(
        self, query: str, variables: dict[str, Any] | None, token: str | None
    ) -> dict[str, Any]:
        headers = {"Authorization": token} if token else None
        body = await self._request(
            "POST",
            GRAPHQL_URL,
            json_body={"query": query, "variables": variables or {}},
            headers=headers,
        )
        if not isinstance(body, dict):
            raise EdfConnectionError("Unexpected GraphQL response")
        errors = body.get("errors") or []
        if errors:
            ext = errors[0].get("extensions") or {}
            code = ext.get("errorCode")
            message = ext.get("errorDescription") or errors[0].get("message", "")
            if code in RATE_LIMIT_CODES:
                self._pause()
                raise EdfRateLimitError(message)
            if code in AUTH_FAILED_CODES:
                raise EdfAuthError(f"{code}: {message}")
            raise EdfApiError(code, message)
        return body.get("data") or {}

    async def graphql(
        self, query: str, variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Run an authenticated GraphQL request, renewing the token once if needed."""
        token = await self._get_token()
        try:
            return await self._graphql_raw(query, variables, token)
        except EdfApiError as err:
            if err.code not in TOKEN_EXPIRED_CODES:
                raise
        token = await self._get_token(force=True)
        return await self._graphql_raw(query, variables, token)

    async def _rest(
        self, path: str, params: dict[str, Any], *, auth: bool
    ) -> list[dict[str, Any]]:
        """Fetch every page of a paginated REST endpoint."""
        url: str | None = f"{REST_URL}{path}"
        results: list[dict[str, Any]] = []
        while url:
            headers = {"Authorization": await self._get_token()} if auth else None
            body = await self._request("GET", url, params=params, headers=headers)
            if not isinstance(body, dict):
                raise EdfConnectionError(f"Unexpected response from {path}")
            results.extend(body.get("results") or [])
            # The next link already carries the query string.
            url, params = body.get("next"), None
        return results

    # -- authentication --------------------------------------------------

    async def _get_token(self, force: bool = False) -> str:
        async with self._token_lock:
            now = datetime.now(UTC)
            if not force and self._token and now < self._token_expiry - TOKEN_EXPIRY_MARGIN:
                return self._token
            data = None
            if self._refresh_token and now < self._refresh_expiry - TOKEN_EXPIRY_MARGIN:
                try:
                    data = await self._obtain_token({"refreshToken": self._refresh_token})
                except (EdfApiError, EdfAuthError) as err:
                    _LOGGER.debug("Refresh token rejected, logging in again: %s", err)
                    self._refresh_token = None
            if data is None:
                if self._api_key:
                    data = await self._obtain_token({"APIKey": self._api_key})
                else:
                    data = await self._obtain_token(
                        {"email": self._email, "password": self._password}
                    )
            self._token = data["token"]
            self._token_expiry = _jwt_expiry(self._token)
            if data.get("refreshToken"):
                self._refresh_token = data["refreshToken"]
                self._refresh_expiry = datetime.fromtimestamp(
                    data.get("refreshExpiresIn") or 0, tz=UTC
                )
            return self._token

    async def _obtain_token(self, token_input: dict[str, Any]) -> dict[str, Any]:
        try:
            data = await self._graphql_raw(
                TOKEN_MUTATION, {"input": token_input}, token=None
            )
        except EdfApiError as err:
            if err.code in REFRESH_FAILED_CODES and "refreshToken" in token_input:
                raise
            # Any other validation error on login means bad credentials.
            raise EdfAuthError(str(err)) from err
        result = data.get("obtainKrakenToken")
        if not result or not result.get("token"):
            raise EdfAuthError("No token returned")
        return result

    async def authenticate(self) -> None:
        """Check the credentials by getting a token."""
        await self._get_token(force=True)

    async def async_create_api_key(self) -> str:
        """Create a new API key for this user. Any previous key stops working.

        EDF doesn't show keys in its website or app, and only returns a usable
        one when it's created, so this is how an email/password login becomes
        a stored API key.
        """
        data = await self.graphql(CREATE_API_KEY_MUTATION)
        key = (data.get("regenerateSecretKey") or {}).get("key")
        if not key:
            raise EdfConnectionError("EDF didn't return an API key")
        return key

    # -- queries ---------------------------------------------------------

    async def async_get_account_numbers(self) -> list[str]:
        data = await self.graphql(ACCOUNTS_QUERY)
        accounts = (data.get("viewer") or {}).get("accounts") or []
        return [a["number"] for a in accounts if a.get("number")]

    async def async_get_account(self, account_number: str) -> dict[str, Any]:
        """Return the account, its meter points and the points allowance."""
        data = await self.graphql(ACCOUNT_QUERY, {"accountNumber": account_number})
        if not data.get("account"):
            raise EdfConnectionError(f"Account {account_number} not found")
        return data

    async def async_get_telemetry(self, device_id: str) -> dict[str, Any] | None:
        """Return the latest reading from the in-home display, if it has one."""
        data = await self.graphql(TELEMETRY_QUERY, {"deviceId": device_id})
        readings = data.get("smartMeterTelemetry") or []
        return readings[-1] if readings else None

    async def async_get_consumption(
        self, mpan: str, serial: str, period_from: datetime, period_to: datetime | None = None
    ) -> list[dict[str, Any]]:
        """Half-hourly consumption in kWh, oldest first."""
        params: dict[str, Any] = {
            "period_from": _isoformat(period_from),
            "page_size": 25000,
            "order_by": "period",
        }
        if period_to:
            params["period_to"] = _isoformat(period_to)
        return await self._rest(
            f"/electricity-meter-points/{mpan}/meters/{serial}/consumption/",
            params,
            auth=True,
        )

    async def async_get_unit_rates(
        self, product_code: str, tariff_code: str, period_from: datetime, period_to: datetime
    ) -> list[dict[str, Any]]:
        """Unit rates in pence/kWh. Public data, so no token and no points."""
        return await self._rest(
            f"/products/{product_code}/electricity-tariffs/{tariff_code}/standard-unit-rates/",
            {
                "period_from": _isoformat(period_from),
                "period_to": _isoformat(period_to),
                "page_size": 1500,
            },
            auth=False,
        )

    async def async_get_standing_charges(
        self, product_code: str, tariff_code: str, period_from: datetime, period_to: datetime
    ) -> list[dict[str, Any]]:
        return await self._rest(
            f"/products/{product_code}/electricity-tariffs/{tariff_code}/standing-charges/",
            {
                "period_from": _isoformat(period_from),
                "period_to": _isoformat(period_to),
            },
            auth=False,
        )
