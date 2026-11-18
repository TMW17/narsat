"""Stable fire-event identities across repeated clustering runs.

Clustering is recomputed on a sliding window every time new detections arrive, and
DBSCAN labels are arbitrary integers. ``reconcile`` turns each run's labels into
event identifiers that survive growth, merging and reprocessing.

The rule that makes this simple: **an event is identified by the detection that
founded it** (its *seed*), and an event identifier belongs to whichever cluster
currently contains that seed.

* A cluster containing exactly one known seed keeps that event.
* A cluster containing several seeds is a merge: the event with the earliest seed
  survives, the others are recorded as ``merged`` into it.
* A cluster containing no seed is a new event, seeded by its earliest detection.
* A previous event whose seed no longer sits in any cluster is ``merged`` into the
  event that received most of its detections, or ``dissolved`` if none did.

The function is pure (no clock, no database), which is what the property-based
tests rely on.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from narsat import ids
from narsat.events.clustering import NOISE


@dataclass(frozen=True)
class Reconciliation:
    #: Event identifier for each input row (``None`` for noise).
    event_ids: list[uuid.UUID | None]
    #: Seed detection of every event present in this run.
    seeds: dict[uuid.UUID, uuid.UUID]
    #: Previous event -> event that absorbed it.
    merged: dict[uuid.UUID, uuid.UUID] = field(default_factory=dict)
    #: Previous events left without any clustered detection.
    dissolved: set[uuid.UUID] = field(default_factory=set)


def reconcile(
    hotspot_ids: Sequence[uuid.UUID],
    labels: Sequence[int],
    acq_ts: Sequence[dt.datetime],
    previous_event_ids: Sequence[uuid.UUID | None],
    previous_seeds: Mapping[uuid.UUID, uuid.UUID],
) -> Reconciliation:
    """Assign stable event identifiers to one clustering result.

    ``previous_event_ids`` is the event each detection belonged to before this run
    and ``previous_seeds`` maps those events to their seed detection.
    """
    n = len(hotspot_ids)
    if not (len(labels) == len(acq_ts) == len(previous_event_ids) == n):
        raise ValueError("all per-detection sequences must have the same length")

    members: dict[int, list[int]] = defaultdict(list)
    for row, label in enumerate(labels):
        if label != NOISE:
            members[int(label)].append(row)

    row_of_hotspot = {hotspot_ids[row]: row for row in range(n)}
    # Which cluster currently holds each known seed (if the seed is in this run at all).
    seed_events_in_cluster: dict[int, list[uuid.UUID]] = defaultdict(list)
    for event, seed in previous_seeds.items():
        seed_row = row_of_hotspot.get(seed)
        if seed_row is not None and labels[seed_row] != NOISE:
            seed_events_in_cluster[int(labels[seed_row])].append(event)

    def seed_order(event: uuid.UUID) -> tuple[dt.datetime, str]:
        return (acq_ts[row_of_hotspot[previous_seeds[event]]], str(event))

    event_of_label: dict[int, uuid.UUID] = {}
    seeds: dict[uuid.UUID, uuid.UUID] = {}
    merged: dict[uuid.UUID, uuid.UUID] = {}

    for label in sorted(members):
        candidates = seed_events_in_cluster.get(label, [])
        if candidates:
            survivor = min(candidates, key=seed_order)
            seeds[survivor] = previous_seeds[survivor]
            for other in candidates:
                if other != survivor:
                    merged[other] = survivor
        else:
            founder = min(members[label], key=lambda r: (acq_ts[r], str(hotspot_ids[r])))
            survivor = ids.event_id(hotspot_ids[founder])
            seeds[survivor] = hotspot_ids[founder]
        event_of_label[label] = survivor

    event_ids: list[uuid.UUID | None] = [
        event_of_label[int(label)] if label != NOISE else None for label in labels
    ]

    # Previous events that kept no cluster: follow the majority of their detections.
    destinations: dict[uuid.UUID, Counter[uuid.UUID]] = defaultdict(Counter)
    seen_previous: set[uuid.UUID] = set()
    for row in range(n):
        previous = previous_event_ids[row]
        if previous is None:
            continue
        seen_previous.add(previous)
        current = event_ids[row]
        if current is not None:
            destinations[previous][current] += 1

    alive = set(event_of_label.values())
    dissolved: set[uuid.UUID] = set()
    for previous in sorted(seen_previous | set(previous_seeds), key=str):
        if previous in alive or previous in merged:
            continue
        votes = destinations.get(previous)
        if votes:
            # Deterministic tie-break on the identifier.
            target = min(votes, key=lambda event: (-votes[event], str(event)))
            merged[previous] = target
        else:
            dissolved.add(previous)

    return Reconciliation(event_ids=event_ids, seeds=seeds, merged=merged, dissolved=dissolved)
