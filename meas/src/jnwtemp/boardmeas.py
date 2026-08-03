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
"""Measure both sensors with the demo board alone, using the RP2350's PIO.

No Saleae, no sample rate, no threshold: the RP2350 times the chip's own
output and the host carries only reduced numbers, a few kB/s.

Why two state machines for GR07
-------------------------------
GR07 emits a **~8 ns pulse** per period - narrower than any PIO polling loop
can sample, since the tightest useful loop is 2 instructions (15.6 ns at
128 MHz). Polled directly it reads exactly 3x low, because two pulses in three
fall between samples. Only ``wait`` sees it, and ``wait`` stalls the state
machine so it cannot also count time. Hence:

    uo_out[0] --(wait, cycle-accurate)--> SM0 stretcher --> GPIO47, 62 ns wide
    GPIO47 --(3-cycle polling loop)--> SM1 reciprocal counter --> word per bin

SM1 times exactly N periods per bin, so the resolution is one 23.4 ns tick over
the whole bin (23 ppm, ~7 mK at N=907) rather than one count of the input
(1100 ppm, ~330 mK). Measured against the Saleae: 907.351 kHz vs 907.590 kHz,
and 90.7 mK of white noise per 1 ms bin where the Saleae's per-event sigma
predicts 77.2 mK.

GR06 needs no stretcher - its response pulse is ~7 us - so SM2 times it
directly at 15.6 ns while the CPU drives the ResetTemp06 burst. Its per-pulse
sigma matches the Saleae's to within 1%.

GPIO47 carries the internal tick because ttboard claims nothing above GPIO40,
and 41..47 all read as floating.
"""

from __future__ import annotations

import base64
import struct
import time
from typing import Callable, Dict, Optional

import numpy as np

from .acquire import BOTH, Reading, SENSORS, reduce_events
from .board import BoardError, TTBoard
from .temperature import Calibration

#: RP2350 GPIOs. uo_out sits above the PIO's default 32-pin window, so the
#: window is moved to 16, which still covers ui_in and the tick.
SENSOR_GPIO = 33      # uo_out[0], GR07
GR06_GPIO = 35        # uo_out[2], GR06 response
UI_GPIO = 17          # ui_in[0], ResetTemp06 - driven by PIO, not by the CPU
TICK_GPIO = 47        # unclaimed by ttboard, verified floating

#: Instructions per iteration of each timing loop, i.e. the tick in cycles.
GR07_TICK_CYCLES = 3
GR06_TICK_CYCLES = 2
#: The reset generator's loop is one instruction with a 31-cycle delay.
RESET_TICK_CYCLES = 32
#: Each phase of the reset train is a 16-bit tick count.
RESET_MAX_TICKS = 0xFFFF

#: Periods per bin is derived from this until the first reading measures it.
NOMINAL_RATE_HZ = 907_000.0

#: Headroom for bins measured while the host was busy between reads. The RX
#: FIFO is four deep and three lengths stay queued, so this covers the worst
#: case of a round trip that runs long.
BACKLOG_BINS = 8

#: Never ask for a bin thinner than this many periods: below it the reciprocal
#: counter's own quantisation would start to show above the sensor's noise.
MIN_PERIODS_PER_BIN = 16

#: Uploaded once per connection; every read is then a one-line call. The pin
#: numbers are substituted by name rather than with str.format, because this is
#: Python source and it has braces of its own.
BOARD_SETUP = """
import rp2, array, time, ubinascii
from machine import Pin

_SENSOR = SENSOR_PIN
_TICK = TICK_PIN
_GR06 = GR06_PIN
_UI = UI_PIN

@rp2.asm_pio(set_init=rp2.PIO.OUT_LOW)
def _jnw_stretch():
    wrap_target()
    wait(1, pin, 0)                 # the ~8 ns pulse, caught at 1-cycle resolution
    set(pins, 1) [7]                # re-emit it 8 cycles = 62 ns wide
    set(pins, 0)
    wait(0, pin, 0)                 # already low; guards against a double count
    wrap()

@rp2.asm_pio()
def _jnw_bintimer():
    wrap_target()
    pull(block)                     # one bin length per bin: the host feeding
    mov(x, osr)                     # this is what gates the counter
    label("sync_low")               # start the clock on an edge, not mid-period
    jmp(pin, "sync_low")
    label("sync_high")
    jmp(pin, "start")
    jmp("sync_high")
    label("start")
    mov(y, invert(null))            # timer counts down from 0xFFFFFFFF
    label("wait_low")               # both loops are 3 cycles: an even tick
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
    mov(isr, invert(y))             # ticks elapsed over N periods
    push(noblock)
    wrap()

@rp2.asm_pio()
def _jnw_width():
    wrap_target()
    wait(0, pin, 0)                 # start from a known low
    wait(1, pin, 0)                 # rising edge of the response pulse
    mov(y, invert(null))
    label("hi")                     # 2-cycle loop -> 15.6 ns per tick
    jmp(pin, "cont")
    jmp("done")
    label("cont")
    jmp(y_dec, "hi")
    label("done")
    mov(isr, invert(y))
    push(noblock)
    wrap()

_jnw = {}

@rp2.asm_pio(set_init=rp2.PIO.OUT_LOW)
def _jnw_reset():
    pull(block)                     # high<<16 | low, in 32-cycle ticks
    mov(isr, osr)                   # master copy: out() consumes the OSR
    wrap_target()
    mov(osr, isr)
    out(x, 16)
    set(pins, 1)
    label("h")
    jmp(x_dec, "h") [31]
    out(y, 16)
    set(pins, 0)
    label("l")
    jmp(y_dec, "l") [31]
    wrap()

@rp2.asm_pio()
def _jnw_wbin():
    wrap_target()
    pull(block)                     # N-1 responses per bin
    mov(x, osr)
    mov(y, invert(null))            # accumulates high time across the whole bin
    label("pulse")
    wait(0, pin, 0)
    wait(1, pin, 0)
    label("hi")
    jmp(pin, "cont")                # 2-cycle loop -> 15.6 ns per tick
    jmp("next")
    label("cont")
    jmp(y_dec, "hi")
    label("next")
    jmp(x_dec, "pulse")
    mov(isr, invert(y))             # total ticks of N widths
    push(noblock)
    wrap()

def _jnw_init(freq):
    # Create the three state machines once and leave them running. Everything
    # is built here and never rebuilt: adding a program to PIO0 while other
    # machines are live stops them, and a read that re-added programs every
    # time worked alone but silently returned zero GR07 bins once the sensor
    # set had changed. SM1 blocks on its pull until the host feeds it a bin
    # length, so an always-active machine still only counts when asked to.
    _jnw_stop()
    rp2.PIO(0).gpio_base(16)
    rp2.PIO(1).gpio_base(16)
    sm0 = rp2.StateMachine(0, _jnw_stretch, freq=freq,
                           in_base=Pin(_SENSOR), set_base=Pin(_TICK))
    sm1 = rp2.StateMachine(1, _jnw_bintimer, freq=freq, jmp_pin=Pin(_TICK))
    # wait() reads in_base, the polling loop reads jmp_pin: set both.
    sm2 = rp2.StateMachine(2, _jnw_width, freq=freq,
                           in_base=Pin(_GR06), jmp_pin=Pin(_GR06))
    # The reset train has to come from hardware. Driven from the CPU it stops
    # whenever the host is between calls, and GR06 then has nothing to measure
    # for a whole round trip - a hole in the trace on every update.
    # On the second block with the accumulator: PIO0 is already full (the
    # stretcher, the GR07 counter and the per-pulse timer come to 28 of its 32
    # instruction slots), and this program needs nine more.
    sm3 = rp2.StateMachine(5, _jnw_reset, freq=freq, set_base=Pin(_UI))
    # A second block, because PIO0 is full: this one sums N responses and
    # pushes one word per bin, so the FIFO holds bins rather than pulses and
    # covers the round trip the same way the GR07 counter does.
    sm4 = rp2.StateMachine(4, _jnw_wbin, freq=freq,
                           in_base=Pin(_GR06), jmp_pin=Pin(_GR06))
    sm0.active(1)
    sm1.active(1)
    sm2.active(1)
    sm4.active(1)
    _jnw["sm"] = (sm0, sm1, sm2, sm3, sm4)
    print("pio ready")

def _jnw_reset_arm(packed):
    # The shape is baked in at the pull, so changing it means starting over -
    # rare, and re-init is the only way that is certainly clean.
    if _jnw.get("reset") == packed:
        return
    sm3 = _jnw["sm"][3]
    sm3.active(0)
    sm3.put(packed)
    sm3.active(1)
    _jnw["reset"] = packed

def _jnw_reset_off():
    sm3 = _jnw["sm"][3]
    sm3.active(0)
    Pin(_UI, Pin.OUT).value(0)      # hand the pin back, deasserted
    _jnw["reset"] = 0

def _jnw_stop():
    for _sm in _jnw.get("sm", ()):
        try:
            _sm.active(0)
        except Exception:
            pass
    _jnw["sm"] = ()
    _jnw["q"] = 0
    _jnw["n7"] = 0
    _jnw["q6"] = 0
    _jnw["n6"] = 0
    _jnw["reset"] = 0
    try:
        Pin(_UI, Pin.OUT).value(0)
        for _p in (0, 1):
            rp2.PIO(_p).remove_program()
            rp2.PIO(_p).gpio_base(0)
        Pin(_TICK, Pin.IN)
    except Exception as e:
        print("cleanup:", e)

def _jnw_collect(n7, bins7, n6, bins6, evmax, budget_us, window_us):
    sm0, sm1, sm2, sm3, sm4 = _jnw["sm"]

    def _requeue(sm, key_n, key_q, n, want):
        # A bin length is only changed by throwing away what was measured with
        # the old one, so do it only when it really changed.
        q = _jnw.get(key_q, 0)
        if _jnw.get(key_n) != n:
            _t = time.ticks_add(time.ticks_us(), 300000)
            while q > 0 and time.ticks_diff(_t, time.ticks_us()) > 0:
                if sm.rx_fifo():
                    sm.get()
                    q -= 1
            while sm.rx_fifo():
                sm.get()
            q = 0
            _jnw[key_n] = n
        while want and q < 3 and sm.tx_fifo() < 4:
            sm.put(n - 1)
            q += 1
        _jnw[key_q] = q
        return q

    q7 = _requeue(sm1, "n7", "q", n7, bins7)
    q6 = _requeue(sm4, "n6", "q6", n6, bins6)
    b7 = array.array("I", bytes(4 * bins7)) if bins7 else array.array("I")
    b6 = array.array("I", bytes(4 * bins6)) if bins6 else array.array("I")
    be = array.array("I", bytes(4 * evmax)) if evmax else array.array("I")
    if not evmax:
        while sm2.rx_fifo():            # per-pulse widths nobody asked for
            sm2.get()
    i = 0
    j = 0
    e = 0
    t0 = time.ticks_us()
    deadline = time.ticks_add(t0, budget_us)
    w_end = time.ticks_add(t0, window_us)
    # Collect for a wall-clock window, not for a fixed number of bins: every
    # counter keeps running between calls, and those bins are waiting in the
    # FIFOs when we come back.
    while (time.ticks_diff(w_end, time.ticks_us()) > 0
           and time.ticks_diff(deadline, time.ticks_us()) > 0):
        if i < bins7 and sm1.rx_fifo():
            b7[i] = sm1.get()
            i += 1
            q7 -= 1
            if q7 < 3 and sm1.tx_fifo() < 4:
                sm1.put(n7 - 1)         # put() blocks on a full FIFO
                q7 += 1
        if j < bins6 and sm4.rx_fifo():
            b6[j] = sm4.get()
            j += 1
            q6 -= 1
            if q6 < 3 and sm4.tx_fifo() < 4:
                sm4.put(n6 - 1)
                q6 += 1
        if e < evmax and sm2.rx_fifo():
            be[e] = sm2.get()
            e += 1
    # Sweep up what is already measured: that backlog is what makes the trace
    # continuous across the round trip.
    while i < bins7 and sm1.rx_fifo():
        b7[i] = sm1.get()
        i += 1
        q7 -= 1
    while j < bins6 and sm4.rx_fifo():
        b6[j] = sm4.get()
        j += 1
        q6 -= 1
    t1 = time.ticks_us()
    # Leave every queue full so nothing idles while the host is away.
    while bins7 and q7 < 3 and sm1.tx_fifo() < 4:
        sm1.put(n7 - 1)
        q7 += 1
    while bins6 and q6 < 3 and sm4.tx_fifo() < 4:
        sm4.put(n6 - 1)
        q6 += 1
    _jnw["q"] = q7
    _jnw["q6"] = q6
    print("r", i, j, e, time.ticks_diff(t1, t0))
    print(ubinascii.b2a_base64(b7[:i]).decode().strip() or "-")
    print(ubinascii.b2a_base64(b6[:j]).decode().strip() or "-")
    print(ubinascii.b2a_base64(be[:e]).decode().strip() or "-")
"""


def board_code() -> str:
    """The board-side source, with the pin numbers filled in."""
    return (BOARD_SETUP
            .replace("SENSOR_PIN", str(SENSOR_GPIO))
            .replace("TICK_PIN", str(TICK_GPIO))
            .replace("GR06_PIN", str(GR06_GPIO))
            .replace("UI_PIN", str(UI_GPIO)))


class BoardMeasurement:
    """Drives the PIO counters over the demo board's REPL."""

    def __init__(self, board: TTBoard, log: Optional[Callable[[str], None]] = None) -> None:
        self.board = board
        self._log = log or (lambda _m: None)
        self._ready = False
        self._sysclk = 0
        #: last measured rate, used to size the next bin in periods
        self._rate_hz = NOMINAL_RATE_HZ
        #: bin length in periods, per requested bin duration; held steady
        self._n7: Dict[float, int] = {}
        #: reset-train shape currently armed on the board, 0 when stopped
        self._reset_packed = 0

    # ------------------------------------------------------------------ setup
    def setup(self) -> str:
        """Upload the PIO programs and start the machines. Safe to re-run."""
        self._sysclk = int(self.board.exec_eval("__import__('machine').freq()"))
        self.board.exec(board_code(), timeout=20)
        self.board.exec(f"_jnw_init({self._sysclk})", timeout=20)
        self._reset_packed = 0
        self._ready = True
        return (f"PIO counters running, sysclk {self._sysclk/1e6:.0f} MHz "
                f"({self.tick_s(GR07_TICK_CYCLES)*1e9:.1f} ns tick)")

    def close(self) -> None:
        """Stop the machines and hand the pins back."""
        if not self._ready or not self.board.connected:
            return
        try:
            self.board.exec("_jnw_stop()", timeout=10)
        except Exception as exc:                 # the port may already be gone
            self._log(f"board counters not stopped cleanly: {exc}")
        self._ready = False

    def reset_shape(self, high_us: int, low_us: int):
        """Pack the ResetTemp06 train for the PIO generator.

        Returns ``(packed, period_s, clamped)``. Each phase is a 16-bit count
        of 250 ns ticks, so the longest phase is about 16 ms; anything longer
        is clamped rather than silently wrapping to a few microseconds.
        """
        tick = RESET_TICK_CYCLES / (self._sysclk or 128_000_000)
        hi = max(1, int(round(high_us * 1e-6 / tick)))
        lo = max(1, int(round(low_us * 1e-6 / tick)))
        clamped = hi > RESET_MAX_TICKS or lo > RESET_MAX_TICKS
        hi = min(hi, RESET_MAX_TICKS)
        lo = min(lo, RESET_MAX_TICKS)
        return (hi << 16) | lo, (hi + lo) * tick, clamped

    def _periods_for(self, seconds: float) -> int:
        """Periods that fill ``seconds``, held steady against rate drift.

        Changing the bin length costs the backlog: bins already measured with
        the old one have to be thrown away. Recomputing it from the live rate
        every read would do that every read, which is one round trip of missing
        trace each time - so only follow the rate when it has really moved.
        """
        target = max(MIN_PERIODS_PER_BIN, seconds * self._rate_hz)
        held = self._n7.get(seconds)
        if held is not None and abs(held - target) < 0.02 * target:
            return held
        n = max(MIN_PERIODS_PER_BIN, int(round(target)))
        self._n7[seconds] = n
        return n

    def tick_s(self, cycles: int) -> float:
        return cycles / (self._sysclk or 128_000_000)

    # ---------------------------------------------------------------- reading
    def read(self, settings, cals) -> Dict[str, Reading]:
        """One measurement window, reduced per sensor. Mirrors Acquisition.read."""
        if not self.board.connected:
            raise BoardError("not connected to the demo board")
        if not self._ready:
            self.setup()
        keys = settings.sensor_keys
        if not isinstance(cals, dict):
            cals = {keys[0]: cals}
        window_s = max(0.01, float(getattr(settings, "window_s", 0.25)))

        want7 = "GR07" in keys
        want6 = "GR06" in keys

        bin_s = settings.bin_s
        if want7:
            if bin_s > 0:
                n7 = self._periods_for(bin_s)
                # A ceiling, not a target: the window's own bins plus whatever
                # the counter measured while the host was between calls.
                bins7 = max(1, int(round(window_s / bin_s))) + BACKLOG_BINS
            else:
                # Binning off: the whole window is one reciprocal measurement.
                n7 = self._periods_for(window_s)
                bins7 = 1
        else:
            n7 = bins7 = 0

        if want6:
            packed, period_s, clamped = self.reset_shape(
                settings.reset_high_us, settings.reset_low_us
            )
            if clamped:
                self._log("reset phase longer than 16 ms is not supported by the "
                          "PIO generator; clamped")
            if packed != self._reset_packed:
                self.board.exec(f"_jnw_reset_arm({packed})", timeout=10)
                self._reset_packed = packed
            n6 = max(1, int(round((bin_s or window_s) / period_s)))
            bins6 = max(1, int(round(window_s / (n6 * period_s)))) + BACKLOG_BINS
            evmax = int(window_s / period_s) + BACKLOG_BINS
        else:
            if self._reset_packed:
                self.board.exec("_jnw_reset_off()", timeout=10)
                self._reset_packed = 0
            period_s = 0.0
            n6 = bins6 = evmax = 0

        budget_us = int(window_s * 3e6 + 1e6)
        call = (f"_jnw_collect({n7}, {bins7}, {n6}, {bins6}, {evmax}, "
                f"{budget_us}, {int(window_s * 1e6)})")
        try:
            out = self.board.exec(call, timeout=window_s * 3 + 15)
        except BoardError as exc:
            # A power cycle takes the definitions with it; rebuild and retry once.
            if "NameError" not in str(exc) and "KeyError" not in str(exc):
                raise
            self._log("demo board lost its PIO setup - reloading")
            self._ready = False
            self.setup()
            out = self.board.exec(call, timeout=window_s * 3 + 15)
        ticks7, ticks6, widths6, elapsed_s = self._parse(out)

        readings: Dict[str, Reading] = {}
        if want7:
            readings["GR07"] = self._gr07(
                ticks7, n7, cals.get("GR07") or Calibration("GR07")
            )
        if want6:
            readings["GR06"] = self._gr06(
                ticks6, widths6, n6, period_s,
                cals.get("GR06") or Calibration("GR06"),
            )
        return readings

    def _parse(self, out: str):
        lines = [l for l in out.strip().splitlines() if l]
        head = next((i for i, l in enumerate(lines) if l.startswith("r ")), None)
        if head is None or len(lines) < head + 4:
            raise BoardError(f"unexpected reply from the board:\n{out[:400]}")
        for extra in lines[:head]:
            self._log(f"board: {extra}")
        n7, n6, nev, elapsed_us = (int(x) for x in lines[head].split()[1:5])

        def decode(line: str, count: int) -> np.ndarray:
            if line == "-" or count == 0:
                return np.empty(0)
            raw = base64.b64decode(line)
            return np.array(struct.unpack(f"<{count}I", raw[:4 * count]), dtype=float)

        return (decode(lines[head + 1], n7), decode(lines[head + 2], n6),
                decode(lines[head + 3], nev), elapsed_us / 1e6)

    # --------------------------------------------------------------- reducing
    def _gr07(self, ticks: np.ndarray, n7: int, cal: Calibration) -> Reading:
        """Bins of exactly ``n7`` periods, each timed by the reciprocal counter."""
        r = Reading(t_wall=time.time(), sensor="GR07", duration_s=0.0)
        ticks = ticks[ticks > 0]
        if ticks.size == 0:
            r.note = ("no pulses on uo_out[0] - is the project selected and "
                      "the clock running?")
            return r
        tick_s = self.tick_s(GR07_TICK_CYCLES)
        dur = ticks * tick_s                       # seconds per bin
        rate = n7 / dur                            # mean rate over each bin
        temps = np.asarray(cal.temp_c(rate), dtype=float)
        total = float(dur.sum())
        self._rate_hz = float(rate.mean())

        r.duration_s = total
        r.n = int(n7 * ticks.size)
        r.mean_rate_hz = float(rate.mean())
        r.mean_s = 1.0 / r.mean_rate_hz if r.mean_rate_hz else float("nan")
        r.mean_temp_c = float(cal.temp_c(r.mean_rate_hz))
        # The counter never sees single periods, so the per-event spread is
        # inferred from the bin-to-bin spread: exact for white noise, an
        # overestimate when the sensor wanders (which it does below ~50 Hz).
        # Reporting it this way keeps every consumer's sigma/sqrt(n) correct.
        scale = np.sqrt(n7)
        if ticks.size > 1:
            finite = temps[np.isfinite(temps)]
            r.std_temp_c = float(finite.std(ddof=1) * scale) if finite.size > 1 else 0.0
            r.std_s = float((1.0 / rate).std(ddof=1) * scale)
        else:
            r.std_temp_c = 0.0
            r.std_s = 0.0
        r.event_rate_hz = r.mean_rate_hz
        # Bin centres follow the measured durations, so a bin that ran long
        # lands where it actually happened rather than on a nominal grid.
        r.bin_t = np.cumsum(dur) - dur / 2.0
        r.bin_rate_hz = rate
        r.bin_temp_c = temps
        r.bin_n = np.full(ticks.size, float(n7))
        r.bin_s = float(dur.mean())
        # What one timing quantum is worth here is the PIO tick, not a sample
        # period; resolution_k reads this back through timing_lsb_s.
        r.sample_rate = int(round(1.0 / tick_s))
        r.note = f"{ticks.size} bins x {n7} periods, PIO reciprocal counter"
        return r

    def _gr06(self, ticks: np.ndarray, widths_ticks: np.ndarray, n6: int,
              period_s: float, cal: Calibration) -> Reading:
        """Bins of ``n6`` responses, plus whatever per-pulse widths we caught.

        The bins come from the PIO accumulator and never stop, so they are what
        the trace is drawn from. The per-pulse widths only arrive while the host
        is actually reading, which is all the per-event views need - they show
        the latest window, not a continuous record.
        """
        r = Reading(t_wall=time.time(), sensor="GR06", duration_s=0.0)
        ticks = ticks[ticks > 0]
        if ticks.size == 0:
            r.note = ("no response on uo_out[2] - is ResetTemp06 reaching "
                      "ui_in[0]?")
            return r
        tick_s = self.tick_s(GR06_TICK_CYCLES)
        width = ticks * tick_s / n6                # mean high time in each bin
        rate = 1.0 / width
        temps = np.asarray(cal.temp_c(rate), dtype=float)
        dur = n6 * period_s                        # the generator sets the pace

        r.duration_s = float(dur * ticks.size)
        r.n = int(n6 * ticks.size)
        r.mean_rate_hz = float(rate.mean())
        r.mean_s = float(width.mean())
        r.mean_temp_c = float(cal.temp_c(r.mean_rate_hz))
        scale = np.sqrt(n6)
        if ticks.size > 1:
            finite = temps[np.isfinite(temps)]
            r.std_temp_c = float(finite.std(ddof=1) * scale) if finite.size > 1 else 0.0
            r.std_s = float(width.std(ddof=1) * scale)
        else:
            r.std_temp_c = 0.0
            r.std_s = 0.0
        r.event_rate_hz = 1.0 / period_s if period_s > 0 else float("nan")
        r.bin_t = (np.arange(ticks.size) + 0.5) * dur
        r.bin_rate_hz = rate
        r.bin_temp_c = temps
        r.bin_n = np.full(ticks.size, float(n6))
        r.bin_s = float(dur)
        r.sample_rate = int(round(1.0 / tick_s))

        ev = widths_ticks[widths_ticks > 0] * tick_s
        if ev.size:
            r.event_s = ev
            r.event_t = np.arange(ev.size, dtype=float) * period_s
            r.rate_hz = 1.0 / ev
            r.temp_c = np.asarray(cal.temp_c(r.rate_hz), dtype=float)
        r.note = (f"{ticks.size} bins x {n6} responses at "
                  f"{1e6*period_s:.0f} us, PIO reset train")
        return r
