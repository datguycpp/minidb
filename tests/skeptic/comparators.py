"""Frozen load comparators: which baseline a gate's capture is matched against.

A comparator is an identity, not a number typed at the call site: `GATE:REVISION:LOADAVG`, where
REVISION is the commit the frozen script came from and LOADAVG is the load its baseline median was
measured at. The table is keyed by the full identity because gates get re-baselined — a table keyed
by gate name would silently overwrite the number the last capture was called against, which is the
moving-referent problem this table exists to prevent.

A revision ending in `+` marks a comparator measured on the frozen script at the named revision with
the fixture and runner taken from later commits; the entry's own note says which ones.
"""

COMPARATORS = {
    "gate1:d89f689:2.00": {
        "gate": "gate1",
        "revision": "d89f689",
        "base_load": 2.00,
        "ceiling_ms": 259.0,
        "retired": False,
        "note": "quiet BLOCK capture 329.1 ms, ceiling by the band rule",
    },
    "gate3:d89f689:2.00": {
        "gate": "gate3",
        "revision": "d89f689",
        "base_load": 2.00,
        "ceiling_ms": 1952.7,
        "retired": "gate3:d89f689+:2.33",
        "note": "original LOOP baseline 3905.4 ms, ceiling by the half-median rule",
    },
    "gate3:d89f689+:2.33": {
        "gate": "gate3",
        "revision": "d89f689+",
        "base_load": 2.33,
        "ceiling_ms": 1934.4,
        "retired": False,
        "note": ("'+' marks a comparator measured on the frozen script at d89f689, with the tiny "
                 "fixture rebuilt at e81300e and the runner committed at 0e03532; baseline 3868.7"),
    },
}


def lookup(spec):
    """Return `(entry, problem)` for a comparator identity.

    `problem` is None when the identity is frozen and live, otherwise one of:
    `("retired", successor)` — the comparator was superseded, and by what;
    `("drift", expected_identity)` — a live comparator exists for this gate and revision but the
    supplied load is not its frozen number (a typo, refused rather than trusted);
    `("unknown", None)` — no comparator has this identity.
    """
    entry = COMPARATORS.get(spec)
    if entry is not None:
        if entry.get("retired"):
            return entry, ("retired", entry["retired"])
        return entry, None
    gate, _, rest = spec.partition(":")
    revision, _, load = rest.rpartition(":")
    if not gate or not revision or not load:
        return None, ("unknown", None)
    for identity, candidate in COMPARATORS.items():
        if candidate["gate"] == gate and candidate["revision"] == revision:
            return candidate, ("drift", identity)
    return None, ("unknown", None)
