#!/usr/bin/env python3
"""Measure both sensors at once, to split shared noise from each sensor's own.

Why this works
--------------
Both sensors reference VDD. GR06's comparator has no reference net at all - only
VDD and VSS - so its 0.6 V trip point is set relative to the supply, and its
pulse width t = V_ref*C/I inherits the supply directly:

    dt/t = dVDD/VDD                       (gain +1 in width)

GR07 is the same architecture, so its period follows the supply the same way,
which means its *frequency* moves the opposite way:

    df/f = -dVDD/VDD                      (gain -1 in frequency)

That is a sharp prediction, and it is what makes this a test rather than a
fishing trip. Supply noise must appear in both sensors, anti-correlated between
GR06's width and GR07's frequency, with unity gain in fractional terms. Anything
correlated but at a different gain is not the supply.

Everything else is uncorrelated: kT/C on each ramp capacitor, each comparator's
own noise, the RTS trap in GR06 (one trap in one device), and GR07's 64 MHz
retiming quantisation. Those are independent between the two sensors by
construction, so the cross-spectrum rejects them as 1/sqrt(N) in N averages -
which is the whole point. GR07's quantisation is 4100 ppm per period and would
bury any shared term in its own auto-spectrum; in the cross-spectrum it averages
away and the shared term survives.

Separating supply from temperature
----------------------------------
Real die temperature is also common to both - it is what they measure. The die's
thermal time constant was measured at 1.0-1.4 s, so above about 1 Hz the
temperature cannot follow and anything common up there is supply or substrate.
Below 0.1 Hz the common part is mostly real temperature. The band between is
mixed and should not be over-read.

Controls, which matter more than the measurement
------------------------------------------------
A coherence estimate is biased upward by 1/N with N averages, so a small
coherence always appears whether or not anything is shared. Two controls:

  * time-shift null: correlate GR06 against GR07 displaced by many seconds.
    Real shared noise vanishes; estimator bias does not. This calibrates the
    floor the real number has to beat.
  * interleaved run (--interleave): measure the two sensors alternately rather
    than simultaneously. Genuine common-mode disappears; anything that survives
    is an artefact of the analysis or of the shared instrument.

Alignment
---------
Both sensors are serviced by one CPU loop, so every GR07 sample is tagged with
the GR06 pulse index current when it arrived. That makes the alignment exact
rather than inferred from two free-running rates.

    python3 scripts/corr_run.py --minutes 20
    python3 scripts/corr_run.py --minutes 5 --interleave      # the control
"""
from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import platform
import socket
import struct
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))
sys.path.insert(0, HERE)
from board_only_probe import GR06_GPIO, K_PER_PPM, SENSOR_GPIO, TICK_GPIO  # noqa: E402
from noise_run import gr06_chunk, gr07_chunk  # noqa: E402
from jnwtemp.board import TTBoard  # noqa: E402

DATA = os.path.join(os.path.dirname(HERE), "data")

#: GR07 samples per chunk. Each also costs a GR06 array roughly 4x longer and a
#: pulse-index array of the same length, so this is the binding constraint on
#: the board's heap. 2 500 keeps the peak base64 allocation near 60 kB.
CHUNK = 2_500
GR07_PERIODS = 907          # ~1 ms per GR07 sample
GR06_HIGH_US = 20
GR06_LOW_US = 200

DUAL_CODE = """
import rp2, array, time, ubinascii
from machine import Pin

for _i in range(8):
    try:
        rp2.StateMachine(_i).active(0)
    except Exception:
        pass
for _b in (0, 1):
    try:
        rp2.PIO(_b).remove_program()
    except Exception:
        pass
rp2.PIO(0).gpio_base(16)
rp2.PIO(1).gpio_base(16)

# ---- GR07: stretch the ~8 ns pulse, then time N periods with a 3-cycle loop
@rp2.asm_pio(set_init=rp2.PIO.OUT_LOW)
def stretcher():
    wrap_target()
    wait(1, pin, 0)
    set(pins, 1) [7]
    set(pins, 0)
    wait(0, pin, 0)
    wrap()

@rp2.asm_pio()
def bin_timer():
    pull(block)
    wrap_target()
    mov(x, osr)
    label("sync_low")
    jmp(pin, "sync_low")
    label("sync_high")
    jmp(pin, "start")
    jmp("sync_high")
    label("start")
    mov(y, invert(null))
    label("wait_low")
    jmp(y_dec, "chk_low")
    label("chk_low")
    jmp(pin, "loop_low")
    jmp("wait_high")
    label("loop_low")
    jmp("wait_low")
    label("wait_high")
    jmp(y_dec, "chk_high")
    label("chk_high")
    jmp(pin, "edge")
    jmp("wait_high")
    label("edge")
    jmp(x_dec, "wait_low")
    mov(isr, invert(y))
    push(noblock)
    wrap()

# ---- GR06: width of the response pulse, 2-cycle loop, on the second PIO block
@rp2.asm_pio()
def width():
    wrap_target()
    wait(0, pin, 0)
    wait(1, pin, 0)
    mov(y, invert(null))
    label("hi")
    jmp(pin, "cont")
    jmp("done")
    label("cont")
    jmp(y_dec, "hi")
    label("done")
    mov(isr, invert(y))
    push(noblock)
    wrap()

sm0 = rp2.StateMachine(0, stretcher, freq=SYSCLK_HZ,
                       in_base=Pin(SENSOR_GPIO), set_base=Pin(TICK_GPIO))
sm1 = rp2.StateMachine(1, bin_timer, freq=SYSCLK_HZ, jmp_pin=Pin(TICK_GPIO))
sm4 = rp2.StateMachine(4, width, freq=SYSCLK_HZ,
                       in_base=Pin(GR06_GPIO), jmp_pin=Pin(GR06_GPIO))
sm0.active(1)
sm1.put(PERIODS_M1)
sm1.active(1)
sm4.active(1)

p = tt.pins.ui_in0.raw_pin
a7 = array.array("I", bytes(4 * N7))
a6 = array.array("I", bytes(4 * N6))
ix = array.array("I", bytes(4 * N7))     # GR06 pulse index when a7[i] arrived
i7 = 0
i6 = 0
t0 = time.ticks_us()
deadline = time.ticks_add(t0, TIMEOUT_US)
while i7 < N7 and time.ticks_diff(deadline, time.ticks_us()) > 0:
    # one GR06 stimulus pulse
    if i6 < N6:
        p.value(1)
        time.sleep_us(HIGH_US_V)
        p.value(0)
        time.sleep_us(LOW_US_V)
        if sm4.rx_fifo():
            a6[i6] = sm4.get()
            i6 += 1
    # drain whatever GR07 has produced meanwhile
    while sm1.rx_fifo() and i7 < N7:
        a7[i7] = sm1.get()
        ix[i7] = i6
        i7 += 1
t1 = time.ticks_us()
p.value(0)
sm0.active(0)
sm1.active(0)
sm4.active(0)
try:
    rp2.PIO(0).remove_program()
    rp2.PIO(1).remove_program()
    rp2.PIO(0).gpio_base(0)
    rp2.PIO(1).gpio_base(0)
except Exception:
    pass
print("n7", i7, "n6", i6, "elapsed_us", time.ticks_diff(t1, t0))
print(ubinascii.b2a_base64(a7[:i7]).decode().strip())
print(ubinascii.b2a_base64(a6[:i6]).decode().strip())
print(ubinascii.b2a_base64(ix[:i7]).decode().strip())
"""


def dual_chunk(b, sysclk, n7):
    n6 = int(n7 * (GR07_PERIODS / 910e3) / ((GR06_HIGH_US + GR06_LOW_US + 40) / 1e6)) + 64
    code = (DUAL_CODE.replace("SYSCLK_HZ", str(sysclk))
            .replace("PERIODS_M1", str(GR07_PERIODS - 1))
            .replace("N7", str(n7)).replace("N6", str(n6))
            .replace("HIGH_US_V", str(GR06_HIGH_US)).replace("LOW_US_V", str(GR06_LOW_US))
            .replace("SENSOR_GPIO", str(SENSOR_GPIO)).replace("TICK_GPIO", str(TICK_GPIO))
            .replace("GR06_GPIO", str(GR06_GPIO))
            .replace("TIMEOUT_US", str(int(n7 * GR07_PERIODS / 910e3 * 3e6 + 3e6))))
    out = b.exec(code, timeout=n7 * GR07_PERIODS / 910e3 * 3 + 40)
    lines = [l for l in out.splitlines() if l.strip()]
    head = next(i for i, l in enumerate(lines) if l.startswith("n7"))
    p = lines[head].split()
    c7, c6, el = int(p[1]), int(p[3]), int(p[5])
    if c7 == 0 or c6 == 0:
        raise RuntimeError(f"nothing captured: n7={c7} n6={c6}")
    a7 = np.array(struct.unpack(f"<{c7}I", base64.b64decode(lines[head + 1])), dtype=np.uint32)
    a6 = np.array(struct.unpack(f"<{c6}I", base64.b64decode(lines[head + 2])), dtype=np.uint32)
    ix = np.array(struct.unpack(f"<{c7}I", base64.b64decode(lines[head + 3])), dtype=np.uint32)
    return a7, a6, ix, el


def interleaved_chunk(b, sysclk, n7):
    """The control: the two sensors measured one after the other, never at once.

    Genuine common-mode cannot survive this, because the sensors were not
    sampled at the same time. What *can* survive is anything shared by the
    instrument rather than by the die - both PIO blocks running off the same
    system clock, the CPU loop that services them, the USB transfer. So this
    separates "the sensors share noise" from "my measurement correlates them",
    which the time-shift null alone cannot do.

    The two records are then written as if they were simultaneous, so the
    analysis treats them identically and any coherence it reports is
    attributable to the analysis or the instrument.
    """
    a7, el7 = gr07_chunk(b, sysclk, GR07_PERIODS, n7)
    n6 = int(n7 * (GR07_PERIODS / 910e3) / ((GR06_HIGH_US + GR06_LOW_US + 40) / 1e6))
    a6, el6 = gr06_chunk(b, sysclk, max(n6, 64))
    n = min(a7.size, 1 + (a6.size - 1))
    a7 = a7[:n]
    ratio = a6.size / max(n, 1)
    ix = np.clip(np.round(np.arange(1, n + 1) * ratio), 1, a6.size).astype(np.uint32)
    return (a7.astype(np.uint32), a6.astype(np.uint32), ix, el7 + el6)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--minutes", type=float, default=20.0)
    ap.add_argument("--chunk", type=int, default=CHUNK)
    ap.add_argument("--interleave", action="store_true",
                    help="control run: measure the two sensors alternately, so "
                         "genuine common-mode cannot appear")
    ap.add_argument("--port", default=None)
    args = ap.parse_args()

    os.makedirs(DATA, exist_ok=True)
    tag = "interleaved" if args.interleave else "dual"
    stem = os.path.join(DATA, f"corr-{tag}-{time.strftime('%Y%m%d-%H%M%S')}")

    b = TTBoard(args.port)
    b.connect()
    print("configuring:", b.select_project())
    if b.set_clock_hz(64_000_000) != 64_000_000:
        raise SystemExit("project clock did not come up at 64 MHz")
    b.reset_project()
    sysclk = int(b.exec_eval("__import__('machine').freq()"))

    a7, a6, ix, el = (interleaved_chunk(b, sysclk, 400) if args.interleave
                      else dual_chunk(b, sysclk, 400))
    print(f"trial: {a7.size} GR07, {a6.size} GR06 samples in {el/1e3:.0f} ms"
          + ("  (INTERLEAVED control - not simultaneous)" if args.interleave else "")
          + "\n")

    f7 = open(stem + ".gr07.u32", "wb")
    f6 = open(stem + ".gr06.u32", "wb")
    fi = open(stem + ".index.u32", "wb")
    chunks = []
    t_start = time.time()
    deadline = t_start + args.minutes * 60
    print(f"{'elapsed':>9} {'chunks':>7} {'GR07':>10} {'GR06':>10} "
          f"{'GR07 kHz':>10} {'GR06 ns':>10}")
    try:
        while time.time() < deadline:
            t0 = time.time()
            try:
                a7, a6, ix, el = (
                    interleaved_chunk(b, sysclk, args.chunk) if args.interleave
                    else dual_chunk(b, sysclk, args.chunk))
            except Exception as exc:
                msg = str(exc).splitlines()[-1][:70]
                if "MemoryError" in str(exc) and args.chunk > 400:
                    args.chunk //= 2
                    print(f"  board out of memory; chunk -> {args.chunk}")
                else:
                    print(f"  chunk failed: {msg}")
                time.sleep(1.0)
                continue
            a7.tofile(f7); a6.tofile(f6); ix.tofile(fi)
            for fh in (f7, f6, fi):
                fh.flush()
            chunks.append({"t_unix": t0, "n7": int(a7.size), "n6": int(a6.size),
                           "elapsed_us": int(el)})
            v7 = GR07_PERIODS / (a7.astype(float) * 3 / sysclk)
            v6 = a6.astype(float) * 2 / sysclk * 1e9
            print(f"{(time.time()-t_start)/60:8.2f}m {len(chunks):7d} "
                  f"{sum(c['n7'] for c in chunks):10,} "
                  f"{sum(c['n6'] for c in chunks):10,} "
                  f"{v7.mean()/1e3:10.3f} {v6.mean():10.1f}")
    except KeyboardInterrupt:
        print("\ninterrupted - keeping what was written")
    finally:
        for fh in (f7, f6, fi):
            fh.close()
        b.disconnect()

    meta = {
        "created": {"unix": time.time(),
                    "local": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
                    "user": getpass.getuser(), "host": socket.gethostname(),
                    "platform": platform.platform()},
        "tool": "scripts/corr_run.py",
        "purpose": ("simultaneous dual-sensor record, to split supply-borne "
                    "common-mode noise from each sensor's own"),
        "mode": tag,
        "simultaneous": not args.interleave,
        "sysclk_hz": sysclk,
        "gr07_periods_per_sample": GR07_PERIODS,
        "gr07_tick_s": 3 / sysclk,
        "gr06_tick_s": 2 / sysclk,
        "gr06_pulse_us": [GR06_HIGH_US, GR06_LOW_US],
        "k_per_ppm": K_PER_PPM,
        "alignment": ("index.u32 gives, for each GR07 sample, the GR06 pulse "
                      "count at the moment it was read - both are serviced by "
                      "one loop, so this is exact, not inferred"),
        "prediction": ("supply noise: GR06 width gain +1, GR07 frequency gain "
                       "-1, in fractional terms; die tau 1.0-1.4 s, so common "
                       "noise above ~1 Hz cannot be temperature"),
        "chunks": chunks,
    }
    json.dump(meta, open(stem + ".meta.json", "w"), indent=1)
    print(f"\n{sum(c['n7'] for c in chunks):,} GR07 and "
          f"{sum(c['n6'] for c in chunks):,} GR06 samples in {len(chunks)} chunks")
    print("wrote", os.path.basename(stem) + ".{gr07,gr06,index}.u32 + .meta.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
