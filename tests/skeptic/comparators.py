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
        "retired": "gate3:d89f689+:0.72",
        "note": ("'+' marks a comparator measured on the frozen script at d89f689, with the tiny "
                 "fixture rebuilt at e81300e and the runner committed at 0e03532; baseline 3868.7. "
                 "RETIRED: the box's own ambient log put its median at 0.72, i.e. this baseline sat at "
                 "3.3x the median and its window [1.0, 3.0] excluded the majority state from below. "
                 "Successor identity follows the capture's own reading (see the pending row)."),
    },
    # Pre-registered row: the target is frozen from the ambient log BEFORE any capture, so the parameter
    # freeze and the measurement cannot blur. `base_load` stays None because a row whose load is a target
    # has no ceiling to offer yet, and lookup() refuses it until the capture lands.
    #
    # Identity follows the CAPTURED reading, not the target: if the capture launches at 0.83, the live
    # comparator becomes `gate3:d89f689+:0.83` so the identity keeps naming the load the ceiling actually
    # belongs to, and 0.72 stays as declared intent. Until then this row is `pending` and unusable.
    "gate3:d89f689+:0.72": {
        "gate": "gate3",
        "revision": "d89f689+",
        "base_load": None,
        "target_load": 0.72,
        "captured_pre_load": None,
        "ceiling_ms": None,
        "retired": False,
        "pending": True,
        "launch_tolerance": 0.30,
        "note": ("PRE-REGISTERED, NOT YET MEASURED: target_load 0.72 is the ambient log's median "
                 "(n~3,600) before any capture; the launch gate is |reading - 0.72| <= 0.30 "
                 "(0.42-1.02, 47.9% of samples); the capture runs on baseline_g6_quiet (D~30s) and its "
                 "captured_pre_load becomes both the live identity's load and the window's centre "
                 "(window = [max(0, captured - 1.0), captured + 1.0]). No ceiling exists until then, "
                 "and a capture that misses the target by more than the tolerance gains no ceiling."),
    },
}


def lookup(spec):
    """Return `(entry, problem)` for a comparator identity.

    `problem` is None when the identity is frozen and live, otherwise one of:
    `("pending", target_load)` -- the row is pre-registered from a target load and has no measured
    ceiling yet; a capture must run before it can be called against;
    `("retired", successor)` — the comparator was superseded, and by what;
    `("drift", expected_identity)` — a live comparator exists for this gate and revision but the
    supplied load is not its frozen number (a typo, refused rather than trusted);
    `("unknown", None)` — no comparator has this identity.
    """
    entry = COMPARATORS.get(spec)
    if entry is not None:
        if entry.get("pending"):
            return entry, ("pending", entry.get("target_load"))
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
