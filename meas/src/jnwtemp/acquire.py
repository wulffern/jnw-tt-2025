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
"""The measurement loop: drive the chip, capture its edges, produce readings.

This is the closed loop. For GR07 the chip free-runs and we only listen. For
GR06 nothing happens unless ResetTemp06 is toggled, so the host starts a pulse
burst on the RP2350 and captures the chip's response to its own stimulus.

Kept free of Qt so it can be driven from a script or notebook as well as the GUI.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import numpy as np

from .board import TTBoard
from .edges import EdgeTrain
from .logic import CaptureSettings, LogicCapture, LogicError
from .temperature import Calibration


@dataclass(frozen=True)
class SensorSpec:
    """How one of the two sensors on JNW-TEMP is measured."""

    key: str
    label: str
    uo_bit: int
    observable: str  # 'period' | 'high' | 'low'
    needs_stimulus: bool
    ui_bit: Optional[int]
    doc: str

    @property
    def unit_label(self) -> str:
        return "Period" if self.observable == "period" else "Pulse width"


#: uo/ui bit assignments come from info.yaml; the measurement recipes come from
#: the "How to test" section of docs/info.md.
SENSORS: Dict[str, SensorSpec] = {
    "GR07": SensorSpec(
        key="GR07",
        label="GR07 - Pwm07 (free-running)",
        uo_bit=0,
        observable="period",
        needs_stimulus=False,
        ui_bit=None,
        doc=(
            "Free-running PWM on uo_out[0]. The comparator trip is re-timed by "
            "the project clock, so run the clock as fast as it goes (64 MHz). "
            "Period is inversely proportional to absolute temperature."
        ),
    ),
    "GR06": SensorSpec(
        key="GR06",
        label="GR06 - Pwm06 (reset-triggered)",
        uo_bit=2,
        observable="high",
        needs_stimulus=True,
        ui_bit=0,
        doc=(
            "Drive ResetTemp06 on ui_in[0] high then low; each release produces "
            "one pulse on uo_out[2] whose width is inversely proportional to "
            "absolute temperature. The host generates the reset burst, so this "
            "is a closed stimulus/response loop."
        ),
    ),
}


@dataclass
class AcquireSettings:
    """Everything the GUI can change about how a reading is taken."""

    sensor: str = "GR07"
    channel: int = 0
    sample_rate: int = 500_000_000
    threshold_volts: float = 1.2
    duration_s: float = 0.05
    clock_hz: int = 64_000_000
    # GR06 stimulus shape
    reset_high_us: int = 20
    reset_low_us: int = 200
    #: keep events within +/- this fraction of the median interval; wide
    #: enough to pass any real dither, tight enough to catch a missed edge
    outlier_band: float = 0.4

    @property
    def spec(self) -> SensorSpec:
        return SENSORS[self.sensor]

    def timing_lsb_s(self, actual_sample_rate: int = 0) -> float:
        """Dominant timing quantum.

        For GR07 the comparator output is re-timed by the project clock, so one
        clock period - not the Saleae sample period - sets the step size; the
        measured periods land on a two-level staircase 1/clock_hz apart and it
        is the dither between those levels that yields sub-step resolution.
        """
        sample_lsb = 1.0 / (actual_sample_rate or self.sample_rate)
        if self.spec.observable == "period" and self.clock_hz > 0:
            return max(sample_lsb, 1.0 / self.clock_hz)
        return sample_lsb


@dataclass
class Reading:
    """One capture, reduced to a temperature and the raw per-event series."""

    t_wall: float
    sensor: str
    duration_s: float
    #: time of each event within the capture, and the observable in seconds
    event_t: np.ndarray = field(repr=False, default_factory=lambda: np.empty(0))
    event_s: np.ndarray = field(repr=False, default_factory=lambda: np.empty(0))
    rate_hz: np.ndarray = field(repr=False, default_factory=lambda: np.empty(0))
    temp_c: np.ndarray = field(repr=False, default_factory=lambda: np.empty(0))
    n: int = 0
    n_rejected: int = 0
    mean_s: float = float("nan")
    std_s: float = float("nan")
    mean_rate_hz: float = float("nan")
    mean_temp_c: float = float("nan")
    std_temp_c: float = float("nan")
    event_rate_hz: float = float("nan")  # how many events per second of capture
    #: rate the capture actually ran at, which the device may have lowered
    sample_rate: int = 0
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.n > 0


#: Refuse to drop more than this fraction of a capture as "outliers".
MAX_REJECT_FRACTION = 0.2


def reduce_train(
    train: EdgeTrain, spec: SensorSpec, cal: Calibration, band: float = 0.4
) -> Reading:
    """Turn one channel's edges into a :class:`Reading`."""
    if spec.observable == "period":
        t, s = train.periods()
    elif spec.observable == "high":
        t, s = train.high_widths()
    else:
        t, s = train.low_widths()

    r = Reading(
        t_wall=time.time(),
        sensor=spec.key,
        duration_s=train.duration,
        n=0,
    )
    if s.size == 0:
        r.note = (
            f"no {spec.observable} events on the selected channel "
            f"({train.num_edges} edges seen)"
        )
        return r

    # Reject only *gross* outliers, by ratio to the median.
    #
    # A spread-based (MAD/sigma) rule is wrong here: GR07's period is re-timed by
    # the project clock, so it is bimodal - alternating between N and N+1 clock
    # cycles - and a MAD rule reads the far cluster as outliers and deletes it.
    # That destroys exactly the dither the resolution depends on and biases the
    # mean by a whole clock period (~4 K).
    #
    # What actually goes wrong physically is a missed or spurious edge, which
    # doubles or halves the interval. A ratio band catches those and is blind to
    # any amount of legitimate dither.
    med = float(np.median(s))
    if med > 0:
        keep = (s >= med * (1.0 - band)) & (s <= med * (1.0 + band))
    else:
        keep = np.ones(s.size, dtype=bool)
    # Backstop: if this would discard a large fraction, the band is not
    # describing the data and silently dropping half the events would be worse
    # than keeping them. Keep everything and say so.
    if keep.sum() < (1.0 - MAX_REJECT_FRACTION) * s.size:
        r.note = (
            f"outlier band would drop {s.size - int(keep.sum())}/{s.size} events; "
            f"kept all - check the channel and threshold"
        )
        keep = np.ones(s.size, dtype=bool)

    r.n_rejected = int((~keep).sum())
    t, s = t[keep], s[keep]
    rate = 1.0 / s
    temp = np.asarray(cal.temp_c(rate), dtype=float)

    r.event_t = t - train.begin_time
    r.event_s = s
    r.rate_hz = rate
    r.temp_c = temp
    r.n = int(s.size)
    r.mean_s = float(s.mean())
    r.std_s = float(s.std(ddof=1)) if s.size > 1 else 0.0
    # Average in the rate domain: it is the quantity linear in temperature.
    r.mean_rate_hz = float(rate.mean())
    r.mean_temp_c = float(cal.temp_c(r.mean_rate_hz))
    finite_temp = temp[np.isfinite(temp)]
    r.std_temp_c = float(finite_temp.std(ddof=1)) if finite_temp.size > 1 else 0.0
    r.event_rate_hz = float(s.size / train.duration) if train.duration > 0 else float("nan")
    return r


class Acquisition:
    """Owns the Logic 2 connection and (optionally) the demo board.

    Both connections are lazy and independent: the app is useful with the Saleae
    alone, and :meth:`configure_board` is what upgrades it to a closed loop.
    """

    def __init__(
        self,
        settings: AcquireSettings,
        board_port: Optional[str] = None,
        use_board: bool = True,
        log: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.settings = settings
        self.use_board = use_board
        self.board_port = board_port
        self.logic: Optional[LogicCapture] = None
        self.board: Optional[TTBoard] = None
        self._log = log or (lambda msg: None)

    # ------------------------------------------------------------- lifecycle
    def open(self) -> Dict[str, str]:
        """Connect to whatever is available; returns a status per instrument."""
        status = {}
        cs = CaptureSettings(
            channels=[self.settings.channel],
            sample_rate=self.settings.sample_rate,
            threshold_volts=self.settings.threshold_volts,
            duration_s=self.settings.duration_s,
        )
        self.logic = LogicCapture(cs)
        status["logic"] = self.logic.connect()
        self._log(f"Logic 2: {status['logic']}")

        if self.use_board:
            try:
                self.board = TTBoard(port=self.board_port)
                info = self.board.connect()
                status["board"] = f"{info.banner} | {info.project}"
                self._log(f"Demo board: {info.port} @ {info.clock_hz/1e6:.0f} MHz project clock")
            except Exception as exc:
                self.board = None
                status["board"] = f"unavailable: {exc}"
                self._log(f"Demo board unavailable: {exc}")
        else:
            status["board"] = "disabled"
        return status

    def close(self) -> None:
        if self.logic is not None:
            self.logic.close()
            self.logic = None
        if self.board is not None:
            self.board.disconnect()
            self.board = None

    @property
    def board_ready(self) -> bool:
        return self.board is not None and self.board.connected

    # ---------------------------------------------------------- board config
    def configure_board(self) -> str:
        """Put the chip in the state the selected sensor needs."""
        if not self.board_ready:
            return "no board"
        st = self.settings
        msgs = []
        proj = self.board.exec_eval("str(tt)")
        if "tt_um_jnw_wulffern" not in proj:
            msgs.append(self.board.select_project())
        actual = self.board.set_clock_hz(st.clock_hz)
        msgs.append(f"project clock {actual/1e6:.1f} MHz")
        if actual != st.clock_hz:
            st.clock_hz = actual
        # Leave ResetTemp06 deasserted; GR06 asserts it per burst.
        if st.spec.ui_bit is not None:
            self.board.set_ui_in(st.spec.ui_bit, 0)
        return "; ".join(msgs)

    # --------------------------------------------------------------- reading
    def _sync_logic_settings(self) -> None:
        st = self.settings
        self.logic.settings.channels = [st.channel]
        self.logic.settings.sample_rate = st.sample_rate
        self.logic.settings.threshold_volts = st.threshold_volts
        self.logic.settings.duration_s = st.duration_s

    def read(self, cal: Calibration) -> Reading:
        """Take one measurement: stimulate if needed, capture, reduce."""
        if self.logic is None:
            raise LogicError("not connected to Logic 2")
        st = self.settings
        spec = st.spec
        self._sync_logic_settings()

        stimulating = False
        n_pulses = 0
        if spec.needs_stimulus:
            if not self.board_ready:
                raise LogicError(
                    f"{spec.key} produces nothing without a ResetTemp06 burst, "
                    f"but the demo board is not connected"
                )
            # Cover the arming latency of start_capture plus the capture itself,
            # so the whole capture window sees pulses rather than a dead line.
            period_us = st.reset_high_us + st.reset_low_us + TTBoard.PULSE_LOOP_OVERHEAD_US
            n_pulses = max(1, int((st.duration_s + 1.0) * 1e6 / period_us))
            self.board.pulse_ui_in_begin(spec.ui_bit, n_pulses, st.reset_high_us, st.reset_low_us)
            stimulating = True

        try:
            trains = self.logic.capture()
        finally:
            if stimulating:
                try:
                    self.board.pulse_ui_in_end(n_pulses, st.reset_high_us, st.reset_low_us)
                except Exception as exc:
                    self._log(f"reset burst did not finish cleanly: {exc}")

        train = trains[st.channel]
        reading = reduce_train(train, spec, cal, band=st.outlier_band)
        reading.sample_rate = self.logic.actual_sample_rate
        if self.logic.last_rate_note:
            self._log(self.logic.last_rate_note)
            self.logic.last_rate_note = ""
        if stimulating and reading.n:
            reading.note = f"{n_pulses} reset pulses, {reading.n} responses captured"
        return reading

    def detect_channels(self, channels: Optional[List[int]] = None) -> Dict[int, dict]:
        """Report which Saleae channels are carrying a signal (wiring aid)."""
        if self.logic is None:
            raise LogicError("not connected to Logic 2")
        chans = channels if channels is not None else list(range(8))
        return self.logic.scan_channels(chans, duration_s=0.01, sample_rate=100_000_000)
