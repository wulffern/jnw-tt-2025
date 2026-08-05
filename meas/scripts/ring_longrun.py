#!/usr/bin/env python3
"""Long continuous record of the ring oscillator's frequency.

    Design:  Giant Ring Oscillator, ttsky25a project 132, by Uri Shaked
    Purpose: look for burst noise slower than a logic-analyser capture can reach

An 8 s Saleae capture reaches down to about 0.1 Hz, which is nowhere near slow
enough to rule out random telegraph noise - the trap found in GR06 had a
lifetime of seconds and only became obvious over half an hour. The demo board
can count continuously, so it is the right instrument for the slow end: the
only gap is the time to ship each chunk over USB, and that runs at ~90% duty.

The signal is on uo_out[0], the same pin GR07 uses, so the PIO chain from
board_only_probe applies unchanged - a stretcher catching each edge and a
reciprocal counter timing a fixed number of periods.

    python3 scripts/ring_longrun.py --minutes 30
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))
sys.path.insert(0, HERE)
from board_only_probe import GR07_CODE, SENSOR_GPIO, TICK_GPIO, run  # noqa: E402
from jnwtemp.board import TTBoard  # noqa: E402
from jnwtemp.logic import CaptureSettings, LogicCapture  # noqa: E402

DATA = os.path.join(os.path.dirname(HERE), "data")
PROJECT, INDEX, AUTHOR = "tt_um_urish_giant_ringosc", 132, "Uri Shaked"
REF_HZ = 10_000_000
F_RING = 2.948e6
#: Periods per sample. ~1 ms, giving ~24 ppm from the 24 ns tick over the bin.
PERIODS = 2948
CHUNK = 8000
MIN_CHUNK = 500


def verified_start(b, tries=15):
    """Bring up the oscillation and prove it, rather than assuming it started.

    The other start-up sequence leaves the output too fast to measure, and a
    long run taken in that state is worthless, so this checks with the Saleae
    before handing the pin to the board.
    """
    for _ in range(tries):
        b.reset_project()
        b.exec("tt.ui_in.value = 0x00", timeout=20)
        b.reset_project()
        b.exec("tt.ui_in.value = 0x02", timeout=20)
        time.sleep(0.2)
        st = CaptureSettings(channels=[0], sample_rate=250_000_000,
                             threshold_volts=1.2, duration_s=0.004)
        with LogicCapture(st) as cap:
            tr = cap.capture()
        if tr[0].num_edges < 32:
            continue
        _, per = tr[0].periods()
        f = 1.0 / per.mean()
        if abs(f - F_RING) / F_RING < 0.05:
            return f
    return None


def chunk(b, sysclk, n, periods):
    code = (GR07_CODE.replace("SYSCLK_HZ", str(sysclk))
            .replace("PERIODS_M1", str(periods - 1)).replace("NBINS_N", str(n))
            .replace("TIMEOUT_US", str(int(n * periods / F_RING * 3e6 + 2e6)))
            .replace("SENSOR_GPIO", str(SENSOR_GPIO))
            .replace("TICK_GPIO", str(TICK_GPIO)))
    return run(b, code, timeout=n * periods / F_RING * 3 + 30)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=30.0)
    ap.add_argument("--chunk", type=int, default=CHUNK)
    ap.add_argument("--periods", type=int, default=PERIODS)
    ap.add_argument("--port", default=None)
    args = ap.parse_args()

    b = TTBoard(args.port)
    b.connect()
    b.exec(f"tt.shuttle.{PROJECT}.enable()", timeout=30)
    b.exec("import machine\n"
           f"hz = {REF_HZ}\n"
           "if machine.freq() < 2*hz: machine.freq(2*hz)\n"
           "tt.clock_project_PWM(hz)\n", timeout=25)
    f = verified_start(b)
    if f is None:
        b.disconnect()
        raise SystemExit("could not bring up the oscillation - not starting a "
                         "long run in an unverified state")
    print(f"oscillation verified at {f/1e6:.6f} MHz")
    sysclk = int(b.exec_eval("__import__('machine').freq()"))
    tick_s = 3 / sysclk

    probe, _ = chunk(b, sysclk, 200, args.periods)
    if probe.size < 100:
        b.disconnect()
        raise SystemExit("board counter returned nothing on a trial chunk")
    print(f"trial chunk ok: {probe.size} samples, "
          f"{args.periods/(probe.mean()*tick_s)/1e6:.6f} MHz\n")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    stem = os.path.join(DATA, f"ring-longrun-{stamp}")
    fh = open(stem + ".u32", "wb")
    chunks, live = [], 0.0
    t0 = time.time()
    deadline = t0 + args.minutes * 60
    print(f"{'elapsed':>9} {'chunks':>7} {'samples':>11} {'duty':>6} {'MHz':>11} "
          f"{'ppm':>7}")
    try:
        while time.time() < deadline:
            t = time.time()
            try:
                ticks, el = chunk(b, sysclk, args.chunk, args.periods)
            except Exception as exc:
                if "MemoryError" in str(exc) and args.chunk > MIN_CHUNK:
                    args.chunk //= 2
                    print(f"  board out of memory; chunk -> {args.chunk}")
                else:
                    print(f"  chunk failed: {str(exc).splitlines()[-1][:70]}")
                time.sleep(1.0)
                continue
            arr = np.asarray(ticks, dtype=np.uint32)
            arr.tofile(fh)
            fh.flush()
            live += el / 1e6
            chunks.append({"t_unix": t, "n": int(arr.size),
                           "elapsed_us": int(el)})
            v = args.periods / (arr.astype(float) * tick_s)
            tot = sum(c["n"] for c in chunks)
            print(f"{(time.time()-t0)/60:8.2f}m {len(chunks):7d} {tot:11,} "
                  f"{live/max(time.time()-t0,1e-9)*100:5.0f}% {v.mean()/1e6:11.6f} "
                  f"{v.std(ddof=1)/v.mean()*1e6:7.0f}")
    except KeyboardInterrupt:
        print("\ninterrupted - keeping what was written")
    finally:
        fh.close()
        b.disconnect()

    meta = {
        "design": {"project": PROJECT, "index": INDEX, "author": AUTHOR,
                   "chip": "Tiny Tapeout ttsky25a",
                   "note": "measured by us; the design is not ours"},
        "created": {"unix": time.time(),
                    "local": time.strftime("%Y-%m-%d %H:%M:%S %Z")},
        "tool": "scripts/ring_longrun.py",
        "sysclk_hz": sysclk, "tick_s": tick_s,
        "periods_per_sample": args.periods,
        "sensor": "GR07",
        "dtype": "uint32 raw PIO tick counts, little-endian",
        "to_value": "periods_per_sample / (ticks * tick_s) = Hz",
        "verified_start_hz": f,
        "samples": sum(c["n"] for c in chunks),
        "duty_cycle": live / max(time.time() - t0, 1e-9),
        "chunks": chunks,
    }
    json.dump(meta, open(stem + ".meta.json", "w"), indent=1)
    print(f"\n{meta['samples']:,} samples, {os.path.getsize(stem+'.u32')/1e6:.1f} MB, "
          f"duty {meta['duty_cycle']*100:.0f}%")
    print("wrote", os.path.basename(stem) + ".u32")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
