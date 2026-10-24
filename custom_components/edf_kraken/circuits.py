"""Circuits measured at the board, reconciled with EDF's settled meter data.

A circuit is a CT clamp (or any meter) on a whole board or circuit, and all
the circuits on one meter should add up to what the smart meter measures. A
load inside a circuit (a hot tub on the house board) is measured as well, and
the rest of the circuit is worked out as what's left.

Until EDF's half hours arrive, every figure is the CTs' own. Once an hour is
settled, every circuit on that meter is scaled by the same factor so they add
up to the meter (CT clamps are rarely spot on), and loads inside a circuit
scale with it. The hour's statistics are then rewritten, so graphs move from
the estimate to the settled figure on their own.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigSubentry

from .const import (
    CONF_MPAN,
    CONF_PARENT,
    CONF_REMAINDER_NAME,
    CONF_ROLE,
    CONF_SOURCE_ENTITY,
    RECONCILE_MAX_FACTOR,
    RECONCILE_MIN_FACTOR,
    ROLE_CIRCUIT,
    ROLE_INSIDE,
)

HOUR = timedelta(hours=1)


@dataclass
class Member:
    subentry_id: str
    name: str
    entity_id: str


@dataclass
class Circuit(Member):
    remainder_name: str
    loads: list[Member] = field(default_factory=list)


def remainder_id(circuit_id: str) -> str:
    """Base of a circuit's remainder statistics; kinds are appended as for devices."""
    return f"remainder_{circuit_id}"


def layout(subentries: Iterable[ConfigSubentry], mpan: str) -> list[Circuit]:
    """The circuits on one meter, each with the loads inside it."""
    subentries = [s for s in subentries if s.data.get(CONF_MPAN) == mpan]
    circuits = {
        s.subentry_id: Circuit(
            s.subentry_id,
            s.title,
            s.data[CONF_SOURCE_ENTITY],
            s.data.get(CONF_REMAINDER_NAME) or "",
        )
        for s in subentries
        if s.data.get(CONF_ROLE) == ROLE_CIRCUIT
    }
    for s in subentries:
        if s.data.get(CONF_ROLE) == ROLE_INSIDE and s.data.get(CONF_PARENT) in circuits:
            circuits[s.data[CONF_PARENT]].loads.append(Member(s.subentry_id, s.title, s.data[CONF_SOURCE_ENTITY]))
    for c in circuits.values():
        # With nothing inside, the rest is the whole circuit.
        c.remainder_name = c.remainder_name or (f"Rest of {c.name}" if c.loads else c.name)
    return list(circuits.values())


def grouped_ids(subentries: Iterable[ConfigSubentry]) -> set[str]:
    """Tracked devices handled here rather than on their own: circuits and the loads inside them."""
    subentries = list(subentries)
    circuits = {s.subentry_id for s in subentries if s.data.get(CONF_ROLE) == ROLE_CIRCUIT}
    return circuits | {
        s.subentry_id
        for s in subentries
        if s.data.get(CONF_ROLE) == ROLE_INSIDE and s.data.get(CONF_PARENT) in circuits
    }


def signature(circuits: list[Circuit]) -> str:
    """Changes whenever the history has to be rebuilt from scratch."""
    return "|".join(
        f"{c.subentry_id}={c.entity_id}[{','.join(f'{l.subentry_id}={l.entity_id}' for l in c.loads)}]"
        for c in sorted(circuits, key=lambda c: c.subentry_id)
    )


def series_ids(circuits: list[Circuit]) -> list[str]:
    """Every statistics base this layout writes."""
    ids = []
    for c in circuits:
        ids += [c.subentry_id, remainder_id(c.subentry_id), *(l.subentry_id for l in c.loads)]
    return ids


def reconcile(
    circuits: list[Circuit],
    measured: dict[str, dict[datetime, float]],
    meter: dict[datetime, float],
    settled_to: datetime | None,
    start: datetime,
    end: datetime,
) -> tuple[dict[str, list[tuple[datetime, float]]], set[datetime]]:
    """kWh for every series, hour by hour from start to end, and the hours scaled to the meter.

    `measured` maps each circuit and load to its own hourly kWh; `meter` is the
    settled hourly kWh, used for hours ending by `settled_to`.
    """
    out: dict[str, list[tuple[datetime, float]]] = {base: [] for base in series_ids(circuits)}
    scaled: set[datetime] = set()
    hour = start
    while hour < end:
        own = {c.subentry_id: measured.get(c.subentry_id, {}).get(hour, 0.0) for c in circuits}
        factor = 1.0
        total = sum(own.values())
        if settled_to and hour + HOUR <= settled_to and hour in meter and total > 0:
            candidate = meter[hour] / total
            if RECONCILE_MIN_FACTOR <= candidate <= RECONCILE_MAX_FACTOR:
                factor = candidate
                scaled.add(hour)
        for c in circuits:
            circuit_kwh = own[c.subentry_id]
            inside = 0.0
            for load in c.loads:
                kwh = measured.get(load.subentry_id, {}).get(hour, 0.0)
                inside += kwh
                out[load.subentry_id].append((hour, kwh * factor))
            out[c.subentry_id].append((hour, circuit_kwh * factor))
            # A load can read a little over its circuit; the rest never goes negative.
            out[remainder_id(c.subentry_id)].append((hour, max(circuit_kwh - inside, 0.0) * factor))
        hour += HOUR
    return out, scaled

