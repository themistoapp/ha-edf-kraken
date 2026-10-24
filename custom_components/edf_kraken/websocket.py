"""Websocket command the bundled card uses to find its series."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from . import circuits
from .const import CONF_MPAN, DOMAIN, SUBENTRY_TRACKED_DEVICE
from .device_history import statistic_id
from .statistics import statistic_id as meter_statistic_id


@callback
def async_register(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_usage)


def _prefix(stat_id: str) -> str:
    """Statistic id without its kind, e.g. edf_kraken:tracked_abc; the card adds _peak_energy and so on."""
    return stat_id.removesuffix("_peak_energy")


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/usage"})
@callback
def ws_usage(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Every import meter, with the series its usage splits into and how far EDF has settled it."""
    meters = []
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        runtime = entry.runtime_data
        subentries = list(entry.subentries.values())
        grouped = circuits.grouped_ids(subentries)
        summaries = runtime.consumption.data or {}
        for point in runtime.account.data.meter_points:
            if point.is_export or not point.serial:
                continue
            series = []
            for c in circuits.layout(subentries, point.mpan):
                series.append({"id": _prefix(statistic_id(c.subentry_id, "peak_energy")), "name": c.name, "role": "circuit"})
                series += [
                    {"id": _prefix(statistic_id(l.subentry_id, "peak_energy")), "name": l.name, "role": "inside", "circuit": c.name}
                    for l in c.loads
                ]
            series += [
                {"id": _prefix(statistic_id(s.subentry_id, "peak_energy")), "name": s.title, "role": "device"}
                for s in subentries
                if s.subentry_type == SUBENTRY_TRACKED_DEVICE
                and s.subentry_id not in grouped
                and s.data.get(CONF_MPAN) == point.mpan
            ]
            summary = summaries.get(point.mpan)
            meters.append(
                {
                    "mpan": point.mpan,
                    "id": _prefix(meter_statistic_id(point, "peak_energy")),
                    "settled_to": summary.latest_end.isoformat() if summary and summary.latest_end else None,
                    "series": series,
                }
            )
    connection.send_result(msg["id"], {"meters": meters})
