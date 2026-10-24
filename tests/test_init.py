"""Set the integration up against a mocked client and check the entities."""

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

from custom_components.edf_kraken.api import EdfApiError, EdfAuthError
from custom_components.edf_kraken.const import (
    AUTH_API_KEY,
    CONF_ACCOUNT_NUMBER,
    CONF_API_KEY,
    CONF_AUTH_METHOD,
    DOMAIN,
)

from .fixtures import ACCOUNT_NUMBER, MPAN, account_response, consumption, standing_charges, unit_rates

CLIENT = "custom_components.edf_kraken.api.EdfClient"


@pytest.fixture
def entry(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ACCOUNT_NUMBER,
        data={CONF_AUTH_METHOD: AUTH_API_KEY, CONF_API_KEY: "sk_live_test", CONF_ACCOUNT_NUMBER: ACCOUNT_NUMBER},
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def mock_client():
    now = dt_util.utcnow()

    async def get_consumption(mpan, serial, period_from, period_to=None):
        # Data runs up to midnight UTC today, like the real feed's lag.
        end = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return consumption(period_from, end)

    with (
        patch(f"{CLIENT}.async_get_account", AsyncMock(return_value=account_response())),
        patch(f"{CLIENT}.async_get_unit_rates", AsyncMock(side_effect=lambda p, t, f, e: unit_rates(f, e))),
        patch(f"{CLIENT}.async_get_standing_charges", AsyncMock(return_value=standing_charges())),
        patch(f"{CLIENT}.async_get_consumption", AsyncMock(side_effect=get_consumption)) as cons,
        patch(
            f"{CLIENT}.async_get_telemetry",
            AsyncMock(return_value={"readAt": "2026-10-24T11:00:00+00:00", "consumption": "1234567", "demand": "450"}),
        ) as tel,
    ):
        yield {"consumption": cons, "telemetry": tel}


async def test_setup_creates_entities(hass: HomeAssistant, freezer, entry, mock_client):
    freezer.move_to("2026-10-24T11:10:00+00:00")  # 12:10 BST, peak rate
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.state is ConfigEntryState.LOADED

    def state(key, platform="sensor"):
        from homeassistant.helpers import entity_registry as er

        entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, key)
        assert entity_id, key
        return hass.states.get(entity_id)

    prefix = f"{ACCOUNT_NUMBER}_electricity_{MPAN}"
    assert float(state(f"{ACCOUNT_NUMBER}_balance").state) == 123.45
    assert state(f"{prefix}_tariff").state == "Go Electric 12m v3"
    assert float(state(f"{prefix}_current_rate").state) == pytest.approx(0.37207)
    assert float(state(f"{prefix}_next_rate").state) == pytest.approx(0.0666)
    assert state(f"{prefix}_next_rate_change").state == "2026-10-24T22:00:00+00:00"
    assert float(state(f"{prefix}_standing_charge").state) == pytest.approx(0.4807)
    assert state(f"{prefix}_off_peak", "binary_sensor").state == "off"
    assert float(state(f"{prefix}_demand").state) == 450
    assert float(state(f"{prefix}_meter_total").state) == pytest.approx(1234.567)

    # Yesterday (23 Oct, BST): 48 half hours of 0.25 kWh, 14 of them off-peak.
    assert float(state(f"{prefix}_previous_day_consumption").state) == pytest.approx(12.0)
    expected = (14 * 0.25 * 6.66 + 34 * 0.25 * 37.207) / 100 + 0.4807
    assert float(state(f"{prefix}_previous_day_cost").state) == pytest.approx(expected, abs=0.01)
    assert float(state(f"{prefix}_previous_day_off_peak_consumption").state) == pytest.approx(14 * 0.25)
    assert float(state(f"{prefix}_previous_day_peak_consumption").state) == pytest.approx(34 * 0.25)
    assert float(state(f"{prefix}_previous_day_off_peak_cost").state) == pytest.approx(14 * 0.25 * 6.66 / 100, abs=0.01)
    assert float(state(f"{prefix}_previous_day_peak_cost").state) == pytest.approx(34 * 0.25 * 37.207 / 100, abs=0.01)

    # Off-peak flips on at 23:00 local.
    freezer.move_to("2026-10-24T22:00:00+00:00")
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert state(f"{prefix}_off_peak", "binary_sensor").state == "on"
    assert float(state(f"{prefix}_current_rate").state) == pytest.approx(0.0666)

    # Hourly statistics were imported for the Energy dashboard.
    await async_wait_recording_done(hass)
    stat_id = f"{DOMAIN}:electricity_{MPAN}_consumption_energy"
    stats = await hass.async_add_executor_job(
        statistics_during_period, hass, dt_util.utcnow() - timedelta(days=40), None, {stat_id}, "hour", None, {"sum"}
    )
    rows = stats[stat_id]
    # 30 days back from midnight UK time (23:00Z) up to the feed's end at 00:00Z.
    assert len(rows) == 30 * 24 + 1
    assert rows[-1]["sum"] == pytest.approx(len(rows) * 0.5)
    band_ids = {f"{DOMAIN}:electricity_{MPAN}_consumption_{b}_energy" for b in ("peak", "off_peak")}
    band_stats = await hass.async_add_executor_job(
        statistics_during_period, hass, dt_util.utcnow() - timedelta(days=40), None, band_ids, "hour", None, {"sum"}
    )
    # Peak and off-peak add up to the total; off-peak is 7 of every 24 hours.
    peak, off_peak = (band_stats[i][-1]["sum"] for i in sorted(band_ids, reverse=True))
    assert peak + off_peak == pytest.approx(rows[-1]["sum"])
    assert off_peak == pytest.approx(30 * 7 * 0.5, abs=1.0)


async def test_statistics_import_is_incremental(hass, freezer, entry, mock_client):
    freezer.move_to("2026-10-24T11:10:00+00:00")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)
    first_from = mock_client["consumption"].call_args.args[2]

    coordinator = entry.runtime_data.consumption
    await coordinator.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    second_from = mock_client["consumption"].call_args.args[2]
    # Second run only reaches back for the summary window, not the 30-day backfill.
    assert first_from < second_from
    assert dt_util.utcnow() - second_from <= timedelta(days=5)


async def test_no_telemetry_entities_without_display(hass, freezer, entry, mock_client):
    mock_client["telemetry"].side_effect = EdfApiError("KT-GB-4039", "Unable to query smart meter telemetry data.")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.runtime_data.telemetry == {}
    assert not [s for s in hass.states.async_entity_ids("sensor") if s.endswith("_demand")]


async def test_auth_failure_starts_reauth(hass, entry):
    with patch(f"{CLIENT}.async_get_account", AsyncMock(side_effect=EdfAuthError("KT-CT-1139"))):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert any(f["context"]["source"] == "reauth" for f in hass.config_entries.flow.async_progress())



async def test_last_full_day_uses_lagged_data(hass, freezer, entry, mock_client):
    """Real data lags ~1.5 days; the summary should use the newest complete day."""
    freezer.move_to("2026-10-24T11:10:00+00:00")
    now = dt_util.utcnow()
    end = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1, hours=1)  # 22 Oct 23:00Z
    mock_client["consumption"].side_effect = lambda mpan, serial, f, t=None: consumption(f, end)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    summary = entry.runtime_data.consumption.data[MPAN]
    assert summary.previous_day.date().isoformat() == "2026-10-22"
    assert summary.previous_day_kwh == pytest.approx(12.0)
