#!/usr/bin/env python3
"""Does GR07 still work as the project clock is raised?

GR07's period is re-timed onto the project clock, so a faster clock means a
finer LSB - 4.69 K at 64 MHz, 3.00 K at 100 MHz. The catch is that the retiming
flop fails by going metastable, which does not crash anything: it emits wrong
codes that look like plausible data. So the clock cannot simply be raised and
trusted; it has to be raised and checked.

Result on this board, 2026-08-04 - 64 MHz is already the ceiling:

    proj    sysclk   GR07        verdict
    64 MHz  128 MHz  911.6 kHz   ok
    65 MHz  130 MHz  910.9 kHz   -781 ppm
    66 MHz  132 MHz  909.9 kHz   -1909 ppm
    67 MHz+ -        no output   PWM refuses; no clock reaches the chip

Two separate traps, both of which report success:

1. ``tt.auto_clocking_freq`` returns what was *requested*, not what the hardware
   produced. Above the PWM's limit the SDK logs "Could not set project clock
   PWM: Requested frequency too high" and then reports the requested value
   anyway. Asking for 100 MHz yields a board that claims 100 MHz while emitting
   no project clock at all - which presents as a dead sensor, not a clock error.
   This script scrapes the SDK's log rather than trusting the property.

2. At 65-66 MHz the PWM does succeed and GR07 does output, but its frequency
   drops ~2000 ppm - the retiming path losing edges. That was checked against
   GR06, which is asynchronous and never sees the project clock: interleaved
   A/B at 64 and 66 MHz moved GR07 by -2096 ppm and GR06 by only +550, so the
   shift is the chip and not a misreported PLL corrupting the timebase.

So the answer to "the board supports 100 MHz" is that the *board* may, but this
path does not deliver it, and the design does not survive even 66.

    python3 scripts/gr07_clock_sweep.py                  # 64, 75, 100 MHz
    python3 scripts/gr07_clock_sweep.py --clocks 64 65 66 67
"""
from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))
sys.path.insert(0, HERE)
import numpy as np  # noqa: E402

from board_only_probe import (  # noqa: E402
    GR07_CODE, K_PER_PPM, SENSOR_GPIO, TICK_GPIO, run,
)
from jnwtemp.board import TTBoard  # noqa: E402

DEFAULT_CLOCKS = (64, 75, 100)

# The stretcher+reciprocal-counter chain in board_only_probe is already
# verified against the Saleae (-263 ppm on 2026-08-03), so reuse it rather than
# writing a second PIO path whose failures would be indistinguishable from the
# chip's. A hand-rolled edge counter written for this script returned zero at
# 64 MHz, where the sensor demonstrably works - exactly the ambiguity this
# measurement exists to avoid.
def measure(b: TTBoard, sysclk: int, periods: int, bins: int):
    """Mean frequency and per-bin noise, or None if the sensor produced nothing."""
    code = (GR07_CODE.replace("SYSCLK_HZ", str(sysclk))
            .replace("PERIODS_M1", str(periods - 1))
            .replace("NBINS_N", str(bins))
            .replace("TIMEOUT_US", str(int(bins * 2000 + 1e6)))
            .replace("SENSOR_GPIO", str(SENSOR_GPIO))
            .replace("TICK_GPIO", str(TICK_GPIO)))
    try:
        ticks, _ = run(b, code, timeout=bins / 500.0 + 25)
    except SystemExit:
        return None
    if ticks.size < 3:
        return None
    f = periods / (ticks * (3 / sysclk))
    ppm = (f - f.mean()) / f.mean() * 1e6
    return f.mean(), ppm.std(ddof=1), np.diff(ppm).std(ddof=1) / np.sqrt(2)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=None)
    ap.add_argument("--clocks", type=float, nargs="+", default=list(DEFAULT_CLOCKS),
                    help="project clocks to try, in MHz")
    ap.add_argument("--periods", type=int, default=907)
    ap.add_argument("--bins", type=int, default=400)
    ap.add_argument("--restore", type=float, default=64.0,
                    help="project clock to leave the board at")
    args = ap.parse_args()

    b = TTBoard(args.port)
    b.connect()
    base = None
    try:
        print(f"{'proj MHz':>9} {'sysclk':>8} {'GR07 kHz':>11} {'noise':>9} "
              f"{'white':>9} {'vs base':>9}  verdict")
        for mhz in args.clocks:
            hz = int(mhz * 1e6)
            out = b.exec(
                "import machine\n"
                f"hz = {hz}\n"
                "if machine.freq() < 2 * hz:\n"
                "    machine.freq(2 * hz)\n"
                "tt.clock_project_PWM(hz)\n"
                "print('ACHIEVED', tt.auto_clocking_freq, machine.freq())\n",
                timeout=20)
            got = sysclk = 0
            for line in out.splitlines():
                if line.startswith("ACHIEVED"):
                    got, sysclk = int(line.split()[1]), int(line.split()[2])
            # tt.auto_clocking_freq reports what was *requested*, not what the
            # hardware produced: above the PWM's limit the SDK logs "Could not
            # set project clock PWM" and then reports the requested value
            # anyway. Believing it means running with no project clock at all,
            # which presents as a dead sensor rather than as a clock error.
            pwm_failed = ("Could not set project clock" in out
                          or "too high" in out)
            if not got:
                print(f"{mhz:9.0f}   clock not achieved:\n{out}")
                continue
            if pwm_failed:
                print(f"{mhz:9.0f} {sysclk/1e6:7.0f}M {'-':>11} {'-':>9} {'-':>9}"
                      f" {'-':>9}  PWM REFUSED (no clock reaches the chip)")
                continue
            got_m = measure(b, sysclk, args.periods, args.bins)
            if got_m is None:
                print(f"{mhz:9.0f} {sysclk/1e6:7.0f}M {'-':>11} {'-':>9} {'-':>9}"
                      f"  NO OUTPUT")
                continue
            rate, noise_ppm, white_ppm = got_m
            if base is None:
                base = rate
            dev = (rate - base) / base * 1e6
            # The sensor's own frequency must not depend on a clock that only
            # re-times it. A shift beyond a few hundred ppm means the retiming
            # path is losing or duplicating edges.
            ok = abs(dev) < 500
            print(f"{mhz:9.0f} {sysclk/1e6:7.0f}M {rate/1e3:11.3f} "
                  f"{noise_ppm*K_PER_PPM*1e3:8.1f}m {white_ppm*K_PER_PPM*1e3:8.1f}m "
                  f"{dev:+8.0f}p  {'ok' if ok else 'SUSPECT'}")
    finally:
        b.exec(f"tt.clock_project_PWM({int(args.restore*1e6)})\n"
               "print('restored', tt.auto_clocking_freq)", timeout=20)
        b.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
