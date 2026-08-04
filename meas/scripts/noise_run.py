#!/usr/bin/env python3
"""Long, gapless-as-possible noise runs on the demo board alone.

The question is whether the sensors' own thermal noise is visible, which needs
a record that is both long (for the low-frequency end, where 1/f and the RTS
trap live) and finely sampled (for the high-frequency end, where a white
thermal floor would sit).

The Saleae is the wrong instrument for this. Each of its captures is followed
by arming and export, so a long record comes out as thousands of short blocks
with dead time between them, and dead time is poison for a spectrum. The RP2350
measures continuously, so a chunk here is limited only by RAM and the time to
ship it over USB.

What each sensor can actually show, worked out before building this:

  GR07  Its period is re-timed onto the 64 MHz project clock, so every period
        carries 15.6/sqrt(12) = 4.5 ns of quantisation - 4100 ppm, and a dither
        model already reproduces GR07's measured noise at r = +0.997. Its
        thermal noise is buried several decades down. Worth running for the
        low-frequency behaviour; pointless for a thermal floor.

  GR06  Asynchronous - no project clock anywhere near it. Its measured
        per-pulse sigma of 14.8 ns on a 7046 ns width is 3.3x the board's own
        4.5 ns timebase floor, so it is real analog noise. This is the sensor
        to point at the question.

Data is streamed to disk chunk by chunk, so a run that is interrupted keeps
everything up to that point. Each chunk records its own start time and sample
count, and the analysis treats chunks as separate records rather than
pretending the seam is not there.

    python3 scripts/noise_run.py --sensor GR06 --minutes 30
    python3 scripts/noise_run.py --sensor GR07 --minutes 30 --periods 907
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import platform
import socket
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))
sys.path.insert(0, HERE)
from board_only_probe import (  # noqa: E402
    GR06_CODE, GR06_GPIO, GR07_CODE, K_PER_PPM, SENSOR_GPIO, TICK_GPIO, run,
)
from jnwtemp.board import TTBoard  # noqa: E402

DATA = os.path.join(os.path.dirname(HERE), "data")

#: Samples per chunk. The board buffers these in RAM and then base64-encodes
#: the whole buffer to ship it, so the peak cost is about 1.35x the raw bytes
#: against ~335 kB of free MicroPython heap. Measured: 12 000 works, 20 000
#: raises MemoryError on the board. 8 000 leaves margin for heap fragmentation
#: over a run of hours, and still holds the duty cycle above 80%.
CHUNK = 8_000
#: Smallest chunk worth falling back to before giving up.
MIN_CHUNK = 500

#: GR06 reset pulse. 20 us high is what the sensor needs; the low time sets the
#: repetition rate and so the Nyquist frequency of the record.
GR06_HIGH_US = 20
GR06_LOW_US = 200


def provenance(args, sysclk, extra):
    return {
        "created": {"unix": time.time(),
                    "local": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
                    "user": getpass.getuser(), "host": socket.gethostname(),
                    "platform": platform.platform()},
        "tool": "scripts/noise_run.py",
        "purpose": "long noise record for PSD/Allan, thermal floor search",
        "sensor": args.sensor,
        "sysclk_hz": sysclk,
        "chunk_samples": args.chunk,
        "requested_minutes": args.minutes,
        "instrument": "TinyTapeout RP2350B demo board, PIO reciprocal counter",
        "note": ("No Saleae in this path: its capture/arm/export cycle leaves "
                 "dead time between blocks, which a spectrum cannot tolerate."),
        **extra,
    }


def gr07_chunk(b, sysclk, periods, n):
    code = (GR07_CODE.replace("SYSCLK_HZ", str(sysclk))
            .replace("PERIODS_M1", str(periods - 1)).replace("NBINS_N", str(n))
            .replace("TIMEOUT_US", str(int(n * periods / 910e3 * 3e6 + 2e6)))
            .replace("SENSOR_GPIO", str(SENSOR_GPIO)).replace("TICK_GPIO", str(TICK_GPIO)))
    return run(b, code, timeout=n * periods / 910e3 * 3 + 30)


def gr06_chunk(b, sysclk, n):
    code = (GR06_CODE.replace("SYSCLK_HZ", str(sysclk)).replace("PULSES_N", str(n))
            .replace("HIGH_US_V", str(GR06_HIGH_US)).replace("LOW_US_V", str(GR06_LOW_US))
            .replace("GR06_GPIO", str(GR06_GPIO)))
    return run(b, code, timeout=n * (GR06_HIGH_US + GR06_LOW_US + 40) / 1e6 + 30)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sensor", choices=("GR06", "GR07"), default="GR06")
    ap.add_argument("--minutes", type=float, default=30.0)
    ap.add_argument("--periods", type=int, default=907,
                    help="GR07 only: periods per sample. 907 is ~1 ms; smaller "
                         "raises the Nyquist but also the timebase floor, and "
                         "the board's collection loop saturates near 38 kHz.")
    ap.add_argument("--chunk", type=int, default=CHUNK)
    ap.add_argument("--port", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    os.makedirs(DATA, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    stem = args.out or os.path.join(DATA, f"noise-{args.sensor}-{stamp}")
    raw_path, meta_path = stem + ".u32", stem + ".meta.json"

    b = TTBoard(args.port)
    b.connect()
    # Re-select the project rather than trusting whatever state the board is
    # in. Anything that touches the clock resets the pin modes, and GR06 then
    # returns nothing at all - which looks like a dead sensor, not a
    # misconfigured pin. A 30 min run is too expensive to start on a guess.
    print("configuring:", b.select_project())
    got = b.set_clock_hz(64_000_000)
    if got != 64_000_000:
        raise SystemExit(f"project clock came back {got}, expected 64 MHz")
    b.reset_project()
    sysclk = int(b.exec_eval("__import__('machine').freq()"))
    proj = int(b.exec_eval("tt.auto_clocking_freq"))
    tick_s = (3 if args.sensor == "GR07" else 2) / sysclk

    # One chunk before committing to the full run, for the same reason.
    probe = (gr07_chunk(b, sysclk, args.periods, 2000) if args.sensor == "GR07"
             else gr06_chunk(b, sysclk, 500))
    if probe[0].size < 100:
        b.disconnect()
        raise SystemExit(f"{args.sensor} returned {probe[0].size} samples in a "
                         f"trial chunk - fix that before running for "
                         f"{args.minutes:g} minutes")
    print(f"trial chunk ok: {probe[0].size} samples\n")

    chunks = []
    t_start = time.time()
    deadline = t_start + args.minutes * 60
    print(f"{args.sensor} for {args.minutes:g} min -> {os.path.basename(raw_path)}")
    print(f"sysclk {sysclk/1e6:.0f} MHz, project clock {proj/1e6:.0f} MHz, "
          f"tick {tick_s*1e9:.2f} ns, {args.chunk:,} samples/chunk\n")
    print(f"{'elapsed':>9} {'chunks':>7} {'samples':>11} {'duty':>6} {'value':>12} "
          f"{'sigma ppm':>10}")

    fh = open(raw_path, "wb")
    live_s = 0.0
    try:
        while time.time() < deadline:
            t0 = time.time()
            try:
                if args.sensor == "GR07":
                    ticks, el_us = gr07_chunk(b, sysclk, args.periods, args.chunk)
                else:
                    ticks, el_us = gr06_chunk(b, sysclk, args.chunk)
            except SystemExit as exc:          # the board returned nothing usable
                print(f"  chunk failed, retrying: {exc}")
                time.sleep(1.0)
                continue
            except Exception as exc:
                # A long run must not die on a transient. The one failure seen
                # in practice is the board running out of heap for the base64
                # encode, which a smaller chunk fixes; anything else is worth
                # one retry before shrinking too.
                if "MemoryError" in str(exc) and args.chunk > MIN_CHUNK:
                    args.chunk = max(MIN_CHUNK, args.chunk // 2)
                    print(f"  board out of memory; chunk -> {args.chunk:,}")
                else:
                    print(f"  chunk error, retrying: {str(exc).splitlines()[-1][:80]}")
                time.sleep(1.0)
                continue
            arr = np.asarray(ticks, dtype=np.uint32)
            arr.tofile(fh)
            fh.flush()
            live_s += el_us / 1e6
            chunks.append({"t_unix": t0, "n": int(arr.size),
                           "elapsed_us": int(el_us)})
            if args.sensor == "GR07":
                val = args.periods / (arr * tick_s)
                unit, shown = "kHz", val.mean() / 1e3
            else:
                val = arr * tick_s * 1e9
                unit, shown = "ns", val.mean()
            ppm = val.std(ddof=1) / val.mean() * 1e6
            el = time.time() - t_start
            total = sum(c["n"] for c in chunks)
            print(f"{el/60:8.2f}m {len(chunks):7d} {total:11,} "
                  f"{live_s/max(el,1e-9)*100:5.0f}% {shown:9.2f} {unit:>2} {ppm:10.0f}")
    except KeyboardInterrupt:
        print("\ninterrupted - keeping everything written so far")
    finally:
        fh.close()
        b.disconnect()

    total = sum(c["n"] for c in chunks)
    meta = provenance(args, sysclk, {
        "project_clock_hz": proj,
        "tick_s": tick_s,
        "dtype": "uint32 raw PIO tick counts, little-endian, no header",
        "samples": total,
        "chunks": chunks,
        "duty_cycle": live_s / max(time.time() - t_start, 1e-9),
        "to_value": ("GR07: periods/(ticks*tick_s) = Hz;  "
                     "GR06: ticks*tick_s = pulse width in s"),
        "periods_per_sample": args.periods if args.sensor == "GR07" else 1,
        "k_per_ppm": K_PER_PPM,
        "gr06_pulse_us": [GR06_HIGH_US, GR06_LOW_US],
    })
    json.dump(meta, open(meta_path, "w"), indent=1)
    print(f"\n{total:,} samples in {len(chunks)} chunks, "
          f"{os.path.getsize(raw_path)/1e6:.1f} MB")
    print(f"duty cycle {meta['duty_cycle']*100:.0f}% "
          f"(the rest is shipping chunks over USB)")
    print("wrote", os.path.basename(raw_path), "and", os.path.basename(meta_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
