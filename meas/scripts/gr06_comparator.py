#!/usr/bin/env python3
"""Is GR06's noise already at the comparator, or added on the way to the pin?

Every noise conclusion about GR06 so far was reached by elimination: not kT/C
(the real 53.8 fF capacitor accounts for 5% of the white power), not the supply
(the cross-correlation bounds anything shared with GR07 at 3.3 uV/sqrt(Hz)), so
by exhaustion the comparator. Elimination is weaker than measurement, and the
chip exposes the node that settles it.

The pinout carries a third output nobody had looked at. uo[1] is OUT06 - GR06's
comparator output, buffered straight to the pin - while uo[2] is that same node
NOR'd with the reset to make the pulse we normally time. Capturing both at once
gives the comparator's own transition and the pin's, so the jitter the digital
path adds is the difference between them.

    python3 scripts/gr06_comparator.py
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))
from jnwtemp.board import TTBoard  # noqa: E402
from jnwtemp.logic import CaptureSettings, LogicCapture  # noqa: E402

OUT06_CH, PWM06_CH = 1, 2
#: Two channels, so the Logic Pro halves its rate. 8 ns per edge is coarse
#: against the effect, which is why the answer is quoted as a bound.
RATE = 125_000_000


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pulses", type=int, default=3000)
    ap.add_argument("--high-us", type=int, default=20)
    ap.add_argument("--low-us", type=int, default=200)
    ap.add_argument("--port", default=None)
    args = ap.parse_args()

    b = TTBoard(args.port)
    b.connect()
    b.select_project()
    if b.set_clock_hz(64_000_000) != 64_000_000:
        b.disconnect()
        raise SystemExit("project clock did not come up at 64 MHz")
    b.reset_project()
    budget = b.pulse_budget_s(args.pulses, args.high_us, args.low_us)
    st = CaptureSettings(channels=[OUT06_CH, PWM06_CH], sample_rate=RATE,
                         threshold_volts=1.2, duration_s=min(budget * 0.9, 1.0))
    b.pulse_ui_in_begin(0, args.pulses, args.high_us, args.low_us)
    try:
        with LogicCapture(st) as cap:
            trains = cap.capture()
            got = cap.actual_sample_rate or RATE
    finally:
        try:
            b.pulse_ui_in_end(args.pulses, args.high_us, args.low_us)
        except Exception:
            pass
        b.disconnect()

    t1, t2 = trains[OUT06_CH], trains[PWM06_CH]
    d2r, d2f = t2.rising(), t2.falling()
    n = min(d2r.size, d2f.size)
    w = (d2f[:n] - d2r[:n])
    w = w[w > 0]
    if w.size < 100:
        raise SystemExit(f"only {w.size} pulses - is the stimulus reaching ui_in[0]?")

    # Each Pwm06 pulse ends when the comparator trips; pair it with the nearest
    # OUT06 edge and the spread of that pairing is the added jitter.
    edges1 = np.sort(np.concatenate([t1.rising(), t1.falling()]))
    idx = np.searchsorted(edges1, d2f)
    lag = {}
    for k, te in enumerate(d2f):
        for j in (idx[k] - 1, idx[k]):
            if 0 <= j < edges1.size:
                s = edges1[j] - te
                if k not in lag or abs(s) < abs(lag[k]):
                    lag[k] = s
    d = np.array([lag[k] for k in sorted(lag)])
    d = d[np.abs(d) < 200e-9]

    tick = (1.0 / got) / np.sqrt(12)
    diff_floor = tick * np.sqrt(2)          # two independent edges
    added = float(np.sqrt(max(d.std(ddof=1) ** 2 - diff_floor ** 2, 0.0)))
    sig_w = float(w.std(ddof=1))

    print(f"captured at {got/1e6:.0f} MS/s, {w.size} pulses")
    print(f"  Pwm06 width          {w.mean()*1e9:8.1f} ns   "
          f"sigma {sig_w*1e9:.1f} ns ({sig_w/w.mean()*1e6:.0f} ppm)")
    print(f"  OUT06 -> pin delay   {d.mean()*1e9:+8.2f} ns   "
          f"sigma {d.std(ddof=1)*1e9:.2f} ns")
    print(f"  timebase on a difference        {diff_floor*1e9:.2f} ns")
    print(f"  jitter added after the comparator {added*1e9:.2f} ns")
    frac_amp = added / sig_w
    print(f"\n  the digital path contributes {frac_amp*100:.0f}% of GR06's noise "
          f"in amplitude, {frac_amp**2*100:.1f}% in power")
    print(f"  -> {100 - frac_amp**2*100:.0f}% of the noise power is already "
          f"present at the comparator output")
    print("\n  This is the direct version of a conclusion previously reached by")
    print("  elimination. It is a bound, not an equality: at 8 ns per edge the")
    print("  timebase is a large part of the measured spread, so the true added")
    print("  jitter could be smaller, not larger.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
