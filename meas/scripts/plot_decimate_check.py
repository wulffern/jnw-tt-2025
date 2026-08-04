#!/usr/bin/env python3
"""Guard the trace decimator against the two faults that stalled the GUI.

A 27 min recording arrives as ~13 000 short blocks separated by NaN gap
markers, one per capture. The original decimator handled that badly in two
independent ways, and both got worse the longer the run:

  * it looped over blocks in Python - 68 ms for 13 000 blocks, against 0.4 ms
    for the same number of points contiguous;
  * it floored each block at 4 output points, so a 2 000-point budget returned
    65 000 points, and pyqtgraph then had to paint all of them.

Together those took the per-reading cost past the capture cycle at about
25 minutes. Past that the queued readings - each carrying its whole event
array - pile up faster than they drain, so it does not merely lag, it stops.

Run after touching envelope_decimate:  python3 scripts/plot_decimate_check.py
"""
from __future__ import annotations

import os
import sys
import time

import numpy as np

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
)
from jnwtemp.plots import envelope_decimate  # noqa: E402

BUDGET = 2000
#: Generous against the ~3 ms measured, so this fails on a regression to the
#: Python-loop behaviour (68 ms) rather than on a slow machine.
MAX_MS = 25.0


def gapped(nblocks: int, per_block: int = 22, bin_s: float = 0.005,
           dead_s: float = 0.010):
    """A trace shaped like a real recording: short captures split by dead time."""
    t, v = [], []
    at = 0.0
    for _ in range(nblocks):
        t.append(at + np.arange(per_block) * bin_s)
        v.append(np.random.randn(per_block) + 900)
        at += per_block * bin_s
        t.append([at])
        v.append([np.nan])
        at += dead_s
    return np.concatenate(t), np.concatenate(v)


def main() -> int:
    fails = []

    for nb in (100, 1000, 5000, 13000):
        x, y = gapped(nb)
        t0 = time.perf_counter()
        ox, oy = envelope_decimate(x, y, BUDGET)
        ms = (time.perf_counter() - t0) * 1e3
        # A little slack: the exact path keeps a separator per block, which is
        # what the budget is for, but it must not be exceeded by a multiple.
        over = ox.size > BUDGET * 1.5
        slow = ms > MAX_MS
        print(f"  {nb:6,} blocks {x.size:8,} pts -> {ox.size:7,} out {ms:7.1f} ms"
              f"{'   OVER BUDGET' if over else ''}{'   TOO SLOW' if slow else ''}")
        if over:
            fails.append(f"{nb} blocks returned {ox.size} points for a {BUDGET} budget")
        if slow:
            fails.append(f"{nb} blocks took {ms:.0f} ms")
        if ox.size != oy.size:
            fails.append(f"{nb} blocks: x/y length mismatch")

    # A genuine pause must still break the line - that is why the NaNs exist.
    x = np.concatenate([np.arange(500) * 0.005, [np.nan], np.arange(500) * 0.005 + 100])
    y = np.concatenate([np.random.randn(500), [np.nan], np.random.randn(500)])
    _, oy = envelope_decimate(x, y, BUDGET)
    if not np.isnan(oy).any():
        fails.append("a 100 s pause was drawn across instead of broken")
    print(f"  long pause preserved: {int(np.isnan(oy).sum())} break(s)")

    # The envelope must still bracket the data, or outliers vanish silently.
    x = np.arange(50_000) * 1e-3
    y = np.random.randn(50_000)
    y[12_345] = 42.0
    _, oy = envelope_decimate(x, y, BUDGET)
    if not np.isclose(np.nanmax(oy), 42.0):
        fails.append("the envelope lost the maximum")
    print(f"  spike preserved: max {np.nanmax(oy):.1f}")

    print()
    if fails:
        for f in fails:
            print("FAIL:", f)
        return 1
    print("PASS - bounded output, bounded time, gaps and extremes intact")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
