"""Constants for the EDF Energy (Kraken) integration."""

from datetime import timedelta
from zoneinfo import ZoneInfo

DOMAIN = "edf_kraken"
# Tariff days and bills follow UK time, whatever zone Home Assistant runs in.
UK_TZ = ZoneInfo("Europe/London")
MANUFACTURER = "EDF Energy"

API_BASE = "https://api.edfgb-kraken.energy"
GRAPHQL_URL = f"{API_BASE}/v1/graphql/"
REST_URL = f"{API_BASE}/v1"

CONF_AUTH_METHOD = "auth_method"
CONF_API_KEY = "api_key"
CONF_ACCOUNT_NUMBER = "account_number"
CONF_TELEMETRY_INTERVAL = "telemetry_interval"
CONF_BACKFILL_DAYS = "backfill_days"
CONF_SOURCE_ENTITY = "source_entity"
CONF_POWER_ENTITY = "power_entity"
CONF_MPAN = "mpan"
CONF_ROLE = "role"
CONF_PARENT = "parent"
CONF_REMAINDER_NAME = "remainder_name"
SUBENTRY_TRACKED_DEVICE = "tracked_device"

# How a tracked device relates to the meter: on its own, a circuit (a CT on a
# board; all circuits add up to the meter), or a load inside a circuit.
ROLE_STANDALONE = "standalone"
ROLE_CIRCUIT = "circuit"
ROLE_INSIDE = "inside"

AUTH_API_KEY = "api_key"
AUTH_PASSWORD = "password"

# Polling intervals. Kraken gives account users 50,000 GraphQL points per hour
# and a 200 point ceiling per request. Measured on a live account, the hourly
# account query costs 36 points and a telemetry request 9.
ACCOUNT_INTERVAL = timedelta(hours=1)
RATES_INTERVAL = timedelta(hours=6)
CONSUMPTION_INTERVAL = timedelta(minutes=30)
DEFAULT_TELEMETRY_INTERVAL = 120  # seconds
# smartMeterTelemetry is limited to 125 requests an hour per user.
MIN_TELEMETRY_INTERVAL = 60
TELEMETRY_BACKOFF = timedelta(minutes=30)
TELEMETRY_MAX_FAILURES = 3

DEFAULT_BACKFILL_DAYS = 30
SUMMARY_LOOKBACK_DAYS = 4
MAX_BACKFILL_DAYS = 365
# A settled hour is only scaled to the meter when the circuits already come
# within this factor of it; further out, a CT was probably offline.
RECONCILE_MIN_FACTOR = 0.5
RECONCILE_MAX_FACTOR = 2.0

# Minimum gap between any two requests to the API, across all coordinators.
MIN_REQUEST_SPACING = 1.0  # seconds
REQUEST_TIMEOUT = 30  # seconds
# Refresh the JWT this long before it expires.
TOKEN_EXPIRY_MARGIN = timedelta(minutes=5)
