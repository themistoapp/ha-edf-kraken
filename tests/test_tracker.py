"""Tracked device: split a power or energy sensor into peak/off-peak kWh and cost."""

from datetime import timedelta

import pytest
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.edf_kraken.const import (
    AUTH_API_KEY,
    CONF_ACCOUNT_NUMBER,
    CONF_API_KEY,
    CONF_AUTH_METHOD,
    CONF_MPAN,
    CONF_SOURCE_ENTITY,
    DOMAIN,
    SUBENTRY_TRACKED_DEVICE,
)

from .fixtures import ACCOUNT_NUMBER, MPAN
from .test_init import mock_client  # noqa: F401

OFF_PEAK = 6.66
PEAK = 37.207


def _entry(hass, source="sensor.charger_power"):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ACCOUNT_NUMBER,
        data={CONF_AUTH_METHOD: AUTH_API_KEY, CONF_API_KEY: "k", CONF_ACCOUNT_NUMBER: ACCOUNT_NUMBER},
        subentries_data=[
            ConfigSubentryData(
                data={"name": "EV charger", CONF_SOURCE_ENTITY: source, CONF_MPAN: MPAN},
                subentry_type=SUBENTRY_TRACKED_DEVICE,
                title="EV charger",
                unique_id=None,
            )
        ],
    )
    entry.add_to_hass(hass)
    return entry


def _value(hass, entry, key):
    subentry_id = next(iter(entry.subentries))
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"tracked_{subentry_id}_{key}")
    return float(hass.states.get(entity_id).state)


async def _advance(hass, freezer, to):
    freezer.move_to(to)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def test_power_sensor_split_across_off_peak_start(hass, freezer, mock_client):  # noqa: F811
    # 21:30 UK (20:30Z); off-peak starts 23:00 UK (22:00Z).
    freezer.move_to("2026-10-24T20:30:00+00:00")
    hass.states.async_set("sensor.charger_power", "7000", {"unit_of_measurement": "W"})
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    # Charge at 7 kW for 3 hours: 1.5 h peak, 1.5 h off-peak.
    await _advance(hass, freezer, "2026-10-24T23:30:00+00:00")
    hass.states.async_set("sensor.charger_power", "0", {"unit_of_measurement": "W"})
    await hass.async_block_till_done()

    assert _value(hass, entry, "peak_energy") == pytest.approx(10.5, abs=0.01)
    assert _value(hass, entry, "off_peak_energy") == pytest.approx(10.5, abs=0.01)
    assert _value(hass, entry, "peak_cost") == pytest.approx(10.5 * PEAK / 100, abs=0.01)
    assert _value(hass, entry, "off_peak_cost") == pytest.approx(10.5 * OFF_PEAK / 100, abs=0.01)
    assert _value(hass, entry, "total_cost") == pytest.approx(10.5 * (PEAK + OFF_PEAK) / 100, abs=0.01)


async def test_energy_sensor_deltas_and_reset(hass, freezer, mock_client):  # noqa: F811
    freezer.move_to("2026-10-24T22:30:00+00:00")  # off-peak
    hass.states.async_set("sensor.spa_energy", "100.0", {"unit_of_measurement": "kWh"})
    entry = _entry(hass, "sensor.spa_energy")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    freezer.move_to("2026-10-24T23:00:00+00:00")
    hass.states.async_set("sensor.spa_energy", "102.0", {"unit_of_measurement": "kWh"})
    await hass.async_block_till_done()
    # Meter resets: the new value is the baseline, not negative usage.
    hass.states.async_set("sensor.spa_energy", "0.5", {"unit_of_measurement": "kWh"})
    await hass.async_block_till_done()
    hass.states.async_set("sensor.spa_energy", "1.5", {"unit_of_measurement": "kWh"})
    await hass.async_block_till_done()

    assert _value(hass, entry, "off_peak_energy") == pytest.approx(3.0)
    assert _value(hass, entry, "peak_energy") == pytest.approx(0.0)


async def test_totals_survive_reload(hass, freezer, mock_client):  # noqa: F811
    freezer.move_to("2026-10-24T12:00:00+00:00")
    hass.states.async_set("sensor.charger_power", "2000", {"unit_of_measurement": "W"})
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await _advance(hass, freezer, dt_util.utcnow() + timedelta(minutes=30))
    before = _value(hass, entry, "peak_energy")
    assert before == pytest.approx(1.0, abs=0.01)

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert _value(hass, entry, "peak_energy") == pytest.approx(before, abs=0.01)


async def test_add_tracked_device_flow(hass, freezer, mock_client):  # noqa: F811
    freezer.move_to("2026-10-24T12:00:00+00:00")
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ACCOUNT_NUMBER,
        data={CONF_AUTH_METHOD: AUTH_API_KEY, CONF_API_KEY: "k", CONF_ACCOUNT_NUMBER: ACCOUNT_NUMBER},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TRACKED_DEVICE), context={"source": "user"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Hot tub", CONF_SOURCE_ENTITY: "sensor.spa_power"}
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done(wait_background_tasks=True)
    subentry = next(iter(entry.subentries.values()))
    assert subentry.data == {"name": "Hot tub", CONF_SOURCE_ENTITY: "sensor.spa_power", CONF_MPAN: MPAN}
    assert len(entry.runtime_data.trackers) == 1


async def _seed_energy_history(hass, entity_id, start, hours, kwh_per_hour):
    """Write hourly long-term statistics for a kWh sensor, as the recorder would."""
    from homeassistant.components.recorder.models import StatisticMeanType
    from homeassistant.components.recorder.statistics import async_import_statistics
    from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

    rows, total = [], 0.0
    for i in range(hours):
        total += kwh_per_hour(start + timedelta(hours=i))
        rows.append({"start": start + timedelta(hours=i), "state": total, "sum": total})
    async_import_statistics(
        hass,
        {
            "mean_type": StatisticMeanType.NONE,
            "has_sum": True,
            "name": None,
            "source": "recorder",
            "statistic_id": entity_id,
            "unit_class": "energy",
            "unit_of_measurement": "kWh",
        },
        rows,
    )
    await async_wait_recording_done(hass)


async def test_history_backfill_and_last_full_day(hass, freezer, mock_client):  # noqa: F811
    from homeassistant.components.recorder.statistics import statistics_during_period
    from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

    freezer.move_to("2026-10-24T11:10:00+00:00")
    # The charger draws 7 kWh an hour from 23:00 to 02:00 UK time each night (off-peak)
    # and 1 kWh an hour 12:00-13:00 UK (peak), from the start of the EDF supply.
    def charger(hour):
        uk = hour.astimezone(dt_util.get_time_zone("Europe/London")).hour
        return 7.0 if uk in (23, 0, 1) else 1.0 if uk == 12 else 0.0

    seed_start = dt_util.parse_datetime("2026-09-19T23:00:00+00:00")
    await _seed_energy_history(hass, "sensor.charger_energy", seed_start, 24 * 34 + 12, charger)
    hass.states.async_set("sensor.charger_energy", "0", {"unit_of_measurement": "kWh"})

    entry = _entry(hass, "sensor.charger_energy")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)

    # Last full day (meter's: 23 Oct): 21 kWh off-peak, 1 kWh peak.
    assert _value(hass, entry, "day_off_peak_energy") == pytest.approx(21.0)
    assert _value(hass, entry, "day_peak_energy") == pytest.approx(1.0)
    assert _value(hass, entry, "day_off_peak_cost") == pytest.approx(21 * OFF_PEAK / 100, abs=0.01)
    assert _value(hass, entry, "day_peak_cost") == pytest.approx(1 * PEAK / 100, abs=0.01)
    subentry_id = next(iter(entry.subentries))
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"tracked_{subentry_id}_day_peak_energy")
    attrs = hass.states.get(entity_id).attributes
    assert attrs["date"] == "2026-10-23"
    assert attrs["share_of_meter_percent"] == pytest.approx(22 / 12 * 100, abs=0.1)

    # Statistics were backfilled from the start of the EDF agreement (20 Sep).
    stat_id = f"{DOMAIN}:tracked_{subentry_id}_off_peak_energy".lower()
    stats = await hass.async_add_executor_job(
        statistics_during_period, hass, seed_start, None, {stat_id}, "hour", None, {"sum"}
    )
    rows = stats[stat_id]
    assert dt_util.utc_from_timestamp(rows[0]["start"]) == dt_util.parse_datetime("2026-09-19T23:00:00+00:00")
    # 34 nights of 21 kWh, plus 00:00-02:00 UK on 24 Oct.
    assert rows[-1]["sum"] == pytest.approx(21.0 * 34 + 14)


async def test_current_cost_from_separate_power_sensor(hass, freezer, mock_client):  # noqa: F811
    freezer.move_to("2026-10-24T22:30:00+00:00")  # off-peak
    hass.states.async_set("sensor.charger_energy", "10", {"unit_of_measurement": "kWh"})
    hass.states.async_set("sensor.charger_power", "7000", {"unit_of_measurement": "W"})
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ACCOUNT_NUMBER,
        data={CONF_AUTH_METHOD: AUTH_API_KEY, CONF_API_KEY: "k", CONF_ACCOUNT_NUMBER: ACCOUNT_NUMBER},
        subentries_data=[
            ConfigSubentryData(
                data={
                    "name": "EV charger",
                    CONF_SOURCE_ENTITY: "sensor.charger_energy",
                    "power_entity": "sensor.charger_power",
                    CONF_MPAN: MPAN,
                },
                subentry_type=SUBENTRY_TRACKED_DEVICE,
                title="EV charger",
                unique_id=None,
            )
        ],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert _value(hass, entry, "current_cost") == pytest.approx(7 * OFF_PEAK / 100, abs=0.001)

    hass.states.async_set("sensor.charger_power", "3500", {"unit_of_measurement": "W"})
    await hass.async_block_till_done()
    assert _value(hass, entry, "current_cost") == pytest.approx(3.5 * OFF_PEAK / 100, abs=0.001)
