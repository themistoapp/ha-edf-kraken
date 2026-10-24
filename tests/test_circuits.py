"""Circuits (CT clamps on boards) reconciled with the settled meter."""

from datetime import datetime, timedelta

import pytest
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

from custom_components.edf_kraken import circuits
from custom_components.edf_kraken.const import (
    AUTH_API_KEY,
    CONF_ACCOUNT_NUMBER,
    CONF_API_KEY,
    CONF_AUTH_METHOD,
    CONF_MPAN,
    CONF_PARENT,
    CONF_ROLE,
    CONF_SOURCE_ENTITY,
    DOMAIN,
    ROLE_CIRCUIT,
    ROLE_INSIDE,
    SUBENTRY_TRACKED_DEVICE,
)

from .fixtures import ACCOUNT_NUMBER, MPAN, consumption
from .test_init import mock_client  # noqa: F401
from .test_tracker import _seed_energy_history

H = timedelta(hours=1)
T0 = datetime(2026, 10, 23, 0, tzinfo=dt_util.UTC)


def test_reconcile_scales_settled_hours_only():
    board = [
        circuits.Circuit("db1", "House", "sensor.db1", [circuits.Member("spa", "Spa", "sensor.spa")]),
        circuits.Circuit("ev", "EV", "sensor.ev"),
    ]
    measured = {
        "db1": {T0: 3.0, T0 + H: 3.0, T0 + 2 * H: 3.0},
        "spa": {T0: 1.0, T0 + H: 1.0, T0 + 2 * H: 1.0},
        "ev": {T0: 1.0, T0 + H: 1.0, T0 + 2 * H: 1.0},
    }
    # Hour 0 settled at 5 kWh (x1.25); hour 1 settled at 20 kWh (x5: a CT was out, left alone); hour 2 not settled.
    meter = {T0: 5.0, T0 + H: 20.0}
    out, scaled = circuits.reconcile(board, measured, meter, T0 + 2 * H, T0, T0 + 3 * H)
    assert scaled == {T0}
    # The house circuit leaves out the spa inside it.
    assert [v for _, v in out["db1"]] == [2.5, 2.0, 2.0]
    assert [v for _, v in out["spa"]] == [1.25, 1.0, 1.0]
    assert [v for _, v in out["ev"]] == [1.25, 1.0, 1.0]


def _entry(hass):
    def sub(name, data):
        return ConfigSubentryData(
            data={"name": name, CONF_MPAN: MPAN, **data},
            subentry_type=SUBENTRY_TRACKED_DEVICE,
            title=name,
            unique_id=None,
            subentry_id=name.lower().replace("-", ""),
        )

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ACCOUNT_NUMBER,
        data={CONF_AUTH_METHOD: AUTH_API_KEY, CONF_API_KEY: "k", CONF_ACCOUNT_NUMBER: ACCOUNT_NUMBER},
        subentries_data=[
            sub("House", {CONF_SOURCE_ENTITY: "sensor.db1", CONF_ROLE: ROLE_CIRCUIT}),
            sub("EV", {CONF_SOURCE_ENTITY: "sensor.dbev", CONF_ROLE: ROLE_CIRCUIT}),
            sub("Spa", {CONF_SOURCE_ENTITY: "sensor.spa", CONF_ROLE: ROLE_INSIDE, CONF_PARENT: "house"}),
        ],
    )
    entry.add_to_hass(hass)
    return entry


async def _hours(hass, base, start, end):
    from homeassistant.components.recorder.statistics import statistics_during_period

    ids = {f"{DOMAIN}:tracked_{base}_{kind}" for kind in ("peak_energy", "off_peak_energy")}
    stats = await hass.async_add_executor_job(
        statistics_during_period, hass, start, end, ids, "hour", None, {"state", "sum"}
    )
    total: dict[datetime, float] = {}
    for rows in stats.values():
        for row in rows:
            hour = dt_util.utc_from_timestamp(row["start"])
            total[hour] = total.get(hour, 0.0) + row["state"]
    return stats, total


async def test_board_estimate_then_settled(hass, freezer, mock_client):  # noqa: F811
    freezer.move_to("2026-10-24T11:10:00+00:00")
    seed = dt_util.parse_datetime("2026-10-20T00:00:00+00:00")
    hours = 4 * 24 + 12
    # Meter (fixture): 0.5 kWh an hour. CTs: DB1 0.3, DB-EV 0.1 (together 0.4, so x1.25 once settled); spa 0.1 of DB1.
    await _seed_energy_history(hass, "sensor.db1", seed, hours, lambda h: 0.3)
    await _seed_energy_history(hass, "sensor.dbev", seed, hours, lambda h: 0.1)
    await _seed_energy_history(hass, "sensor.spa", seed, hours, lambda h: 0.1)
    for entity_id in ("sensor.db1", "sensor.dbev", "sensor.spa"):
        hass.states.async_set(entity_id, "0", {"unit_of_measurement": "kWh"})

    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)

    midnight = dt_util.parse_datetime("2026-10-24T00:00:00+00:00")
    _, house = await _hours(hass, "house", seed, None)
    _, ev = await _hours(hass, "ev", seed, None)
    _, spa = await _hours(hass, "spa", seed, None)
    # Settled (before midnight UTC): scaled to the meter.
    assert house[midnight - H] == pytest.approx(0.25)
    assert spa[midnight - H] == pytest.approx(0.125)
    assert ev[midnight - H] == pytest.approx(0.125)
    # Not yet settled: the CTs' own figures, up to the last complete hour.
    assert house[midnight + H] == pytest.approx(0.2)
    assert ev[midnight + H] == pytest.approx(0.1)
    assert max(house) == dt_util.parse_datetime("2026-10-24T10:00:00+00:00")

    # The last full day (23 Oct UK) is settled, and House leaves out the spa.
    house_day = entry.runtime_data.consumption.tracked["house"]
    assert house_day.peak_kwh + house_day.off_peak_kwh == pytest.approx(0.25 * 24, abs=0.01)

    # Circuits and loads have no live running totals, just the settled-day sensors and current cost.
    registry = er.async_get(hass)
    assert registry.async_get_entity_id("sensor", DOMAIN, "tracked_house_peak_energy") is None
    assert registry.async_get_entity_id("sensor", DOMAIN, "tracked_house_day_peak_energy")
    assert registry.async_get_entity_id("sensor", DOMAIN, "tracked_house_current_cost")

    # Just after the next hour, devices catch up between meter fetches.
    freezer.move_to("2026-10-24T12:05:00+00:00")
    await entry.runtime_data.consumption.async_update_devices()
    await async_wait_recording_done(hass)
    _, house = await _hours(hass, "house", seed, None)
    assert max(house) == dt_util.parse_datetime("2026-10-24T11:00:00+00:00")

    # EDF's data now reaches 06:00: those hours are rewritten, and the sums stay continuous.
    six = dt_util.parse_datetime("2026-10-24T06:00:00+00:00")
    mock_client["consumption"].side_effect = lambda mpan, serial, f, t=None: consumption(f, six)
    await entry.runtime_data.consumption.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)
    stats, house = await _hours(hass, "house", seed, None)
    assert house[midnight + H] == pytest.approx(0.25)
    assert house[six] == pytest.approx(0.2)
    for rows in stats.values():
        running = rows[0]["sum"]
        for row in rows[1:]:
            running += row["state"]
            assert row["sum"] == pytest.approx(running)


async def test_usage_websocket_lists_board(hass, freezer, mock_client):  # noqa: F811
    from unittest.mock import MagicMock

    from custom_components.edf_kraken.websocket import ws_usage

    freezer.move_to("2026-10-24T11:10:00+00:00")
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    connection = MagicMock()
    ws_usage(hass, connection, {"id": 1, "type": f"{DOMAIN}/usage"})
    result = connection.send_result.call_args[0][1]
    meter = result["meters"][0]
    assert meter["mpan"] == MPAN
    assert meter["id"] == f"{DOMAIN}:electricity_{MPAN}_consumption".lower()
    assert meter["settled_to"] == "2026-10-24T00:00:00+00:00"
    assert [(s["name"], s["role"], s["id"]) for s in meter["series"]] == [
        ("House", "circuit", f"{DOMAIN}:tracked_house"),
        ("Spa", "inside", f"{DOMAIN}:tracked_spa"),
        ("EV", "circuit", f"{DOMAIN}:tracked_ev"),
    ]
