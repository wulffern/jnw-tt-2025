#!/usr/bin/env python3
######################################################################
##        Copyright (c) 2026 Carsten Wulff Software, Norway
## ###################################################################
##  The MIT License (MIT)
##
##  Permission is hereby granted, free of charge, to any person obtaining a copy
##  of this software and associated documentation files (the "Software"), to deal
##  in the Software without restriction, including without limitation the rights
##  to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
##  copies of the Software, and to permit persons to whom the Software is
##  furnished to do so, subject to the following conditions:
##
##  The above copyright notice and this permission notice shall be included in all
##  copies or substantial portions of the Software.
######################################################################
"""Measure both sensors with the demo board alone - no Saleae.

The RP2350's PIO does the timing, so the host only carries reduced numbers.
Run it with the Logic 2 unplugged; it needs nothing but the serial port.

    ./board_only_probe.py            # both sensors
    ./board_only_probe.py --gr07     # just the free-running one

Why it is built this way
------------------------
GR07 emits a **~8 ns pulse** per period - narrower than any PIO polling loop
can sample (the tightest useful loop is 2 instructions = 15.6 ns at 128 MHz).
Measured directly with a polling loop it reads 3x low, because two pulses in
three fall between samples. Only ``wait``, which samples every cycle, catches
it. But ``wait`` stalls the state machine, so it cannot also count time.

So it takes two state machines and an unconnected GPIO between them:

    uo_out[0] --(wait, cycle-accurate)--> SM0 stretcher --> GPIO47 (62 ns pulse)
    GPIO47 --(3-cycle polling loop)--> SM1 reciprocal counter --> one word per bin

SM1 times exactly N pulses and pushes the elapsed tick count, so each bin is a
reciprocal-counter measurement: resolution is one 23.4 ns tick over the whole
bin (23 ppm ~ 7 mK at N=907), not one count of the input (1100 ppm ~ 330 mK).

GR06 needs no stretcher: its response pulse is ~7 us wide, so a 2-cycle loop
(15.6 ns) times it directly. The stimulus and the measurement then both live on
the RP2350, which is the one thing the Saleae cannot do.

Measured against the Saleae on 2026-08-03 (Saleae reference from 2026-07-31):

    GR07   907.351 kHz vs 907.590 kHz    (-263 ppm = -0.08 K)
           90.7 mK white noise per 1 ms bin; Saleae's per-event sigma predicts
           77.2 mK. The board sees the sensor, not itself: the timebase
           contributes 6.9 mK and the Allan deviation flattens at ~60 mK.
    GR06   7046.2 ns wide, per-pulse sigma 0.622 K; the Saleae saw 0.619 K.

GPIO47 is used as the internal tick line because ttboard claims nothing above
GPIO40 and 41..47 all read as floating (they follow both pull-up and pull-down,
so nothing external drives them).
"""

from __future__ import annotations

import argparse
import base64
import os
import struct
import sys
import time

import numpy as np

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
)

from jnwtemp.board import TTBoard  # noqa: E402

SENSOR_GPIO = 33      # uo_out[0], GR07
GR06_GPIO = 35        # uo_out[2], GR06 response
TICK_GPIO = 47        # unclaimed by ttboard, verified floating

#: PTAT sensors: a fractional frequency change is a fractional change of
#: absolute temperature, so 1 ppm is 1/3378 K near room temperature.
K_PER_PPM = 1 / 3378.0

GR07_CODE = """
import rp2, array, time, ubinascii
from machine import Pin

for _i in range(8):
    try:
        rp2.StateMachine(_i).active(0)
    except Exception:
        pass
try:
    rp2.PIO(0).remove_program()
except Exception:
    pass
rp2.PIO(0).gpio_base(16)          # uo_out is GPIO33+, above the default window

@rp2.asm_pio(set_init=rp2.PIO.OUT_LOW)
def stretcher():
    wrap_target()
    wait(1, pin, 0)               # the ~8 ns pulse, caught at 1-cycle resolution
    set(pins, 1) [7]              # re-emit it 8 cycles = 62 ns wide
    set(pins, 0)
    wait(0, pin, 0)               # already low; guards against a double count
    wrap()

@rp2.asm_pio()
def bin_timer():
    pull(block)                   # N-1, kept in OSR for every bin
    wrap_target()
    mov(x, osr)
    label("sync_low")             # start the clock on an edge, not mid-period
    jmp(pin, "sync_low")
    label("sync_high")
    jmp(pin, "start")
    jmp("sync_high")
    label("start")
    mov(y, invert(null))          # timer counts down from 0xFFFFFFFF
    label("wait_low")             # both loops are 3 cycles, so the tick is even
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
    mov(isr, invert(y))           # ticks elapsed over N periods
    push(noblock)
    wrap()

sm0 = rp2.StateMachine(0, stretcher, freq=SYSCLK_HZ,
                       in_base=Pin(SENSOR_GPIO), set_base=Pin(TICK_GPIO))
sm1 = rp2.StateMachine(1, bin_timer, freq=SYSCLK_HZ, jmp_pin=Pin(TICK_GPIO))
sm0.active(1)
sm1.put(PERIODS_M1)
sm1.active(1)

buf = array.array("I", bytes(4 * NBINS_N))
i = 0
t0 = time.ticks_us()
deadline = time.ticks_add(t0, TIMEOUT_US)
while i < NBINS_N and time.ticks_diff(deadline, time.ticks_us()) > 0:
    if sm1.rx_fifo():
        buf[i] = sm1.get()
        i += 1
t1 = time.ticks_us()
sm1.active(0)
sm0.active(0)
try:
    rp2.PIO(0).remove_program()
    rp2.PIO(0).gpio_base(0)
    Pin(TICK_GPIO, Pin.IN)
except Exception as e:
    print("cleanup:", e)
print("n", i, "elapsed_us", time.ticks_diff(t1, t0))
print(ubinascii.b2a_base64(buf[:i]).decode().strip())
"""

GR06_CODE = """
import rp2, array, time, ubinascii
from machine import Pin

for _i in range(8):
    try:
        rp2.StateMachine(_i).active(0)
    except Exception:
        pass
try:
    rp2.PIO(0).remove_program()
except Exception:
    pass
rp2.PIO(0).gpio_base(16)

@rp2.asm_pio()
def width():
    wrap_target()
    wait(0, pin, 0)               # start from a known low
    wait(1, pin, 0)               # rising edge of the response pulse
    mov(y, invert(null))
    label("hi")                   # 2-cycle loop -> 15.6 ns per tick
    jmp(pin, "cont")
    jmp("done")
    label("cont")
    jmp(y_dec, "hi")
    label("done")
    mov(isr, invert(y))
    push(noblock)
    wrap()

# wait() reads in_base, the polling loop reads jmp_pin: both must be set.
sm = rp2.StateMachine(0, width, freq=SYSCLK_HZ,
                      in_base=Pin(GR06_GPIO), jmp_pin=Pin(GR06_GPIO))
sm.active(1)

p = tt.pins.ui_in0.raw_pin
buf = array.array("I", bytes(4 * PULSES_N))
j = 0
t0 = time.ticks_us()
for _k in range(PULSES_N):
    p.value(1)
    time.sleep_us(HIGH_US_V)
    p.value(0)
    time.sleep_us(LOW_US_V)
    if sm.rx_fifo():
        buf[j] = sm.get()
        j += 1
t1 = time.ticks_us()
p.value(0)                        # never leave ResetTemp06 asserted
sm.active(0)
try:
    rp2.PIO(0).remove_program()
    rp2.PIO(0).gpio_base(0)
except Exception as e:
    print("cleanup:", e)
print("n", j, "elapsed_us", time.ticks_diff(t1, t0))
print(ubinascii.b2a_base64(buf[:j]).decode().strip())
"""


def run(b: TTBoard, code: str, timeout: float) -> tuple:
    """Run a board program that ends with a count line and a base64 blob."""
    out = b.exec(code, timeout=timeout)
    lines = [l for l in out.strip().splitlines() if l]
    for extra in lines[:-2]:
        print("  board:", extra)
    if len(lines) < 2:
        raise SystemExit(f"board returned nothing usable:\n{out}")
    head, blob = lines[-2], lines[-1]
    n = int(head.split()[1])
    elapsed_us = int(head.split()[3])
    if n == 0:
        raise SystemExit("no samples: is the project selected and clocked?")
    return np.array(struct.unpack(f"<{n}I", base64.b64decode(blob)), dtype=float), elapsed_us


def ensure_project(b: TTBoard) -> int:
    state = b.exec("import machine\nprint(tt)\nprint(machine.freq())", timeout=10).splitlines()
    print(state[0])
    if "jnw_wulffern" not in state[0]:
        print("selecting JNW-TEMP...")
        b.select_project()
        b.set_clock_hz(64_000_000)
    return int(b.exec_eval("__import__('machine').freq()"))


def gr07(b: TTBoard, sysclk: int, periods: int, bins: int) -> None:
    tick_s = 3 / sysclk                     # the timing loop is 3 instructions
    code = (GR07_CODE.replace("SYSCLK_HZ", str(sysclk))
            .replace("PERIODS_M1", str(periods - 1))
            .replace("NBINS_N", str(bins))
            .replace("TIMEOUT_US", str(int(bins * 2000 + 1e6)))
            .replace("SENSOR_GPIO", str(SENSOR_GPIO))
            .replace("TICK_GPIO", str(TICK_GPIO)))
    ticks, elapsed_us = run(b, code, timeout=bins / 500.0 + 20)
    f = periods / (ticks * tick_s)
    ppm = (f - f.mean()) / f.mean() * 1e6
    white = np.diff(ppm).std(ddof=1) / np.sqrt(2) if ppm.size > 2 else float("nan")
    print(f"\nGR07  {ticks.size} bins of {periods} periods in {elapsed_us/1e3:.1f} ms")
    print(f"  frequency   {f.mean()/1e3:.3f} kHz   bin {ticks.mean()*tick_s*1e6:.1f} us")
    print(f"  bin noise   {ppm.std(ddof=1):.1f} ppm = {ppm.std(ddof=1)*K_PER_PPM*1e3:.1f} mK"
          f"  (white part {white*K_PER_PPM*1e3:.1f} mK)")
    print(f"  timebase    {tick_s*1e9:.1f} ns tick -> "
          f"{tick_s/(ticks.mean()*tick_s)*1e6*K_PER_PPM*1e3:.1f} mK quantisation")
    print(f"  mean sem    {ppm.std(ddof=1)/np.sqrt(ticks.size)*K_PER_PPM*1e3:.2f} mK")


def gr06(b: TTBoard, sysclk: int, pulses: int, high_us: int, low_us: int) -> None:
    tick_s = 2 / sysclk                     # the timing loop is 2 instructions
    code = (GR06_CODE.replace("SYSCLK_HZ", str(sysclk)).replace("PULSES_N", str(pulses))
            .replace("HIGH_US_V", str(high_us)).replace("LOW_US_V", str(low_us))
            .replace("GR06_GPIO", str(GR06_GPIO)))
    ticks, elapsed_us = run(b, code, timeout=pulses * (high_us + low_us + 40) / 1e6 + 20)
    w_ns = ticks * tick_s * 1e9
    sigma_k = w_ns.std(ddof=1) / w_ns.mean() * 1e6 * K_PER_PPM
    print(f"\nGR06  {ticks.size}/{pulses} pulses in {elapsed_us/1e3:.1f} ms "
          f"({ticks.size/(elapsed_us/1e6):.0f} /s)")
    print(f"  width       {w_ns.mean():.1f} ns   sd {w_ns.std(ddof=1):.1f} ns")
    print(f"  per pulse   sigma {sigma_k:.3f} K")
    print(f"  timebase    {tick_s*1e9:.2f} ns tick -> "
          f"{tick_s*1e9/w_ns.mean()*1e6*K_PER_PPM*1e3:.0f} mK quantisation")
    print(f"  mean sem    {sigma_k/np.sqrt(ticks.size)*1e3:.1f} mK")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", default=None)
    ap.add_argument("--gr07", action="store_true", help="only the free-running sensor")
    ap.add_argument("--gr06", action="store_true", help="only the reset-triggered sensor")
    ap.add_argument("--periods", type=int, default=907, help="periods per GR07 bin (~1 ms)")
    ap.add_argument("--bins", type=int, default=1000)
    ap.add_argument("--pulses", type=int, default=500)
    ap.add_argument("--high-us", type=int, default=20)
    ap.add_argument("--low-us", type=int, default=200)
    args = ap.parse_args()
    both = not (args.gr07 or args.gr06)

    b = TTBoard(port=args.port)
    b.connect()
    try:
        sysclk = ensure_project(b)
        print(f"sysclk {sysclk/1e6:.0f} MHz\n")
        if args.gr07 or both:
            gr07(b, sysclk, args.periods, args.bins)
        if args.gr06 or both:
            gr06(b, sysclk, args.pulses, args.high_us, args.low_us)
    finally:
        b.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
