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


#: Selector for measuring both sensors in one capture. They sit on different
#: pins, so a single two-channel capture covers both - same time base, same
#: thermal environment, which is what makes them comparable.
BOTH = "BOTH"

#: Default Saleae channel per sensor, matching uo_out[0] and uo_out[2].
DEFAULT_CHANNELS = {"GR07": 0, "GR06": 2}

#: Where the timing comes from. The demo board measures the chip with its own
#: PIO and needs no other instrument, so it is the default; Logic 2 is what you
#: pick when you want the per-event streams a 500 MS/s capture can give.
SOURCE_BOARD = "board"
SOURCE_LOGIC = "logic"
SOURCES = [
    ("Demo board (RP2350 PIO counter)", SOURCE_BOARD),
    ("Logic 2 (Saleae, per-event)", SOURCE_LOGIC),
]


@dataclass
class AcquireSettings:
    """Everything the GUI can change about how a reading is taken."""

    sensor: str = "GR07"
    #: SOURCE_BOARD or SOURCE_LOGIC - which instrument does the timing
    source: str = SOURCE_BOARD
    channels: Dict[str, int] = field(default_factory=lambda: dict(DEFAULT_CHANNELS))
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
    #: Bin width for the live trace. Each capture is re-reduced into bins this
    #: wide so the plot shows structure inside a capture rather than one dot.
    #: 0 disables binning and reverts to one point per capture.
    bin_ms: float = 1.0
    #: Board source only: seconds of counting per REPL round trip. There is no
    #: arming and no capture buffer to size, so this is purely how often the
    #: trace grows - not a property of the measurement.
    window_s: float = 0.25

    @property
    def uses_board_timing(self) -> bool:
        return self.source == SOURCE_BOARD

    @property
    def bin_s(self) -> float:
        return self.bin_ms / 1000.0

    @property
    def is_dual(self) -> bool:
        return self.sensor == BOTH

    @property
    def sensor_keys(self) -> List[str]:
        """Sensors this setting measures, in display order."""
        return list(SENSORS) if self.is_dual else [self.sensor]

    @property
    def spec(self) -> SensorSpec:
        """Primary sensor spec - GR07 leads in dual mode."""
        return SENSORS[self.sensor_keys[0]]

    @property
    def channel(self) -> int:
        return self.channels.get(self.sensor_keys[0], 0)

    def channel_of(self, key: str) -> int:
        return self.channels.get(key, DEFAULT_CHANNELS.get(key, 0))

    @property
    def needs_stimulus(self) -> bool:
        """True if any selected sensor has to be driven to say anything."""
        return any(SENSORS[k].needs_stimulus for k in self.sensor_keys)

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

    #: The capture re-reduced into short time bins, so the live trace can show
    #: structure inside a capture instead of one averaged dot. Times are seconds
    #: from the start of the capture.
    bin_t: np.ndarray = field(repr=False, default_factory=lambda: np.empty(0))
    bin_temp_c: np.ndarray = field(repr=False, default_factory=lambda: np.empty(0))
    bin_rate_hz: np.ndarray = field(repr=False, default_factory=lambda: np.empty(0))
    bin_n: np.ndarray = field(repr=False, default_factory=lambda: np.empty(0))
    bin_s: float = 0.0

    @property
    def ok(self) -> bool:
        return self.n > 0

    @property
    def events_per_bin(self) -> float:
        """Median events per bin - how much each plotted point is worth."""
        if self.bin_n.size == 0:
            return float("nan")
        return float(np.median(self.bin_n))

    @property
    def bin_sigma_c(self) -> float:
        """Expected noise on a single binned point, from the per-event spread."""
        ev = self.events_per_bin
        if not np.isfinite(ev) or ev < 1 or not np.isfinite(self.std_temp_c):
            return float("nan")
        return float(self.std_temp_c / np.sqrt(ev))


#: Refuse to drop more than this fraction of a capture as "outliers".
MAX_REJECT_FRACTION = 0.2


def bin_series(
    rel_t: np.ndarray, rate: np.ndarray, duration: float, bin_s: float
) -> tuple:
    """Average ``rate`` into fixed time bins across the capture.

    Averaging happens in the *rate* domain, like everywhere else, because rate
    is the quantity linear in temperature; converting each event to degrees and
    then averaging would bias the result.

    Returns ``(centre_times, mean_rate, count)`` with empty bins carrying NaN so
    a plot can break its line rather than interpolate across a hole.
    """
    if bin_s <= 0 or rel_t.size == 0 or duration <= 0:
        return (np.empty(0), np.empty(0), np.empty(0))
    nb = max(1, int(np.ceil(duration / bin_s)))
    idx = np.clip((rel_t / bin_s).astype(np.int64), 0, nb - 1)
    count = np.bincount(idx, minlength=nb).astype(float)
    total = np.bincount(idx, weights=rate, minlength=nb)
    with np.errstate(invalid="ignore", divide="ignore"):
        mean = np.where(count > 0, total / count, np.nan)
    centres = (np.arange(nb) + 0.5) * bin_s
    return (centres, mean, count)


def reduce_train(
    train: EdgeTrain,
    spec: SensorSpec,
    cal: Calibration,
    band: float = 0.4,
    bin_s: float = 0.0,
) -> Reading:
    """Turn one channel's edges into a :class:`Reading`."""
    if spec.observable == "period":
        t, s = train.periods()
    elif spec.observable == "high":
        t, s = train.high_widths()
    else:
        t, s = train.low_widths()
    return reduce_events(
        spec.key,
        t - train.begin_time if t.size else t,
        s,
        train.duration,
        cal,
        band=band,
        bin_s=bin_s,
        empty_note=(
            f"no {spec.observable} events on the selected channel "
            f"({train.num_edges} edges seen)"
        ),
    )


def reduce_events(
    sensor: str,
    event_t: np.ndarray,
    s: np.ndarray,
    duration: float,
    cal: Calibration,
    band: float = 0.4,
    bin_s: float = 0.0,
    empty_note: str = "no events",
) -> Reading:
    """Reduce a per-event series to a :class:`Reading`.

    Split out of :func:`reduce_train` so the demo board's PIO counter reaches
    the same statistics from its own timing rather than a second, subtly
    different implementation. ``event_t`` is relative to the start of the
    capture and ``s`` is the observable in seconds.
    """
    r = Reading(
        t_wall=time.time(),
        sensor=sensor,
        duration_s=duration,
        n=0,
    )
    if s.size == 0:
        r.note = empty_note
        return r
    t = np.asarray(event_t, dtype=float)
    s = np.asarray(s, dtype=float)

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

    r.event_t = t
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
    r.event_rate_hz = float(s.size / duration) if duration > 0 else float("nan")

    if bin_s > 0:
        centres, mean_rate, count = bin_series(r.event_t, rate, duration, bin_s)
        r.bin_t = centres
        r.bin_rate_hz = mean_rate
        r.bin_temp_c = np.asarray(cal.temp_c(mean_rate), dtype=float)
        r.bin_n = count
        r.bin_s = float(bin_s)
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
        self._meas = None                 # BoardMeasurement, built on first use
        self._log = log or (lambda msg: None)

    # ------------------------------------------------------------- lifecycle
    def open(self) -> Dict[str, str]:
        """Connect to whatever is available; returns a status per instrument.

        Raises if Logic 2 could not be reached, but only after the board has
        been tried - ``last_status`` then still describes both.
        """
        status = {}
        self.last_status = status
        cs = CaptureSettings(
            channels=[self.settings.channel_of(k) for k in self.settings.sensor_keys],
            sample_rate=self.settings.sample_rate,
            threshold_volts=self.settings.threshold_volts,
            duration_s=self.settings.duration_s,
        )
        # Connect the two instruments independently. A Logic 2 failure used to
        # abort open() before the board was touched, so a disabled automation
        # server also left the project clock stopped after a power cycle - and
        # then nothing worked, for two unrelated reasons at once.
        logic_error = None
        if self.settings.uses_board_timing:
            # Nothing to connect: the RP2350 does the timing. Touching Logic 2
            # here would only produce an error about an instrument this session
            # was never going to use.
            self.logic = None
            status["logic"] = "not used - timing from the demo board"
        else:
            self.logic = LogicCapture(cs)
            try:
                status["logic"] = self.logic.connect()
                self._log(f"Logic 2: {status['logic']}")
            except Exception as exc:
                logic_error = exc
                status["logic"] = f"failed: {exc}"
                self._log(f"Logic 2 unavailable: {exc}")

        if self.use_board:
            try:
                self.board = TTBoard(port=self.board_port)
                info = self.board.connect()
                status["board"] = f"{info.banner} | {info.project}"
                self._log(f"Demo board: {info.port} @ {info.clock_hz/1e6:.0f} MHz project clock")
                if self.settings.uses_board_timing:
                    self._log(self.meas.setup())
            except Exception as exc:
                self.board = None
                status["board"] = f"unavailable: {exc}"
                self._log(f"Demo board unavailable: {exc}")
                if self.settings.uses_board_timing:
                    # Here the board *is* the instrument, so this is fatal in
                    # the way a missing Logic 2 is for the other source.
                    raise
        else:
            status["board"] = "disabled"

        if logic_error is not None:
            # Reported after the board so the chip still gets configured, but
            # still raised: without a capture there is nothing to measure.
            raise logic_error
        return status

    def close(self) -> None:
        if self.logic is not None:
            self.logic.close()
            self.logic = None
        if self._meas is not None:
            self._meas.close()
            self._meas = None
        if self.board is not None:
            self.board.disconnect()
            self.board = None

    @property
    def board_ready(self) -> bool:
        return self.board is not None and self.board.connected

    @property
    def meas(self):
        """The demo board's PIO measurement driver, built on first use."""
        from .boardmeas import BoardMeasurement

        if self._meas is None or self._meas.board is not self.board:
            self._meas = BoardMeasurement(self.board, log=self._log)
        return self._meas

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
        if not actual:
            # A stopped clock is silent failure for GR07: the pin simply never
            # toggles and the capture looks like a wiring fault.
            msgs.append("WARNING: project clock is stopped - GR07 will be dead")
        # Leave ResetTemp06 deasserted; GR06 asserts it per burst. Under board
        # timing the pin belongs to the PIO reset generator instead - taking it
        # back here would switch the pad to SIO and kill the stimulus.
        if not st.uses_board_timing:
            for key in st.sensor_keys:
                bit = SENSORS[key].ui_bit
                if bit is not None:
                    self.board.set_ui_in(bit, 0)
        return "; ".join(msgs)

    # --------------------------------------------------------------- reading
    def _sync_logic_settings(self, channels) -> None:
        st = self.settings
        self.logic.settings.channels = list(channels)
        self.logic.settings.sample_rate = st.sample_rate
        self.logic.settings.threshold_volts = st.threshold_volts
        self.logic.settings.duration_s = st.duration_s

    def read(self, cals) -> Dict[str, Reading]:
        """Take one measurement and reduce it for every selected sensor.

        Always returns a dict keyed by sensor, with one entry in single-sensor
        mode and two when measuring both. In dual mode the two sensors come out
        of the *same* capture, so they share a time base and a thermal
        environment - which is the whole point of comparing them.

        ``cals`` may be one Calibration (single mode) or a dict of them.
        """
        if self.settings.uses_board_timing:
            if not self.board_ready:
                raise LogicError("not connected to the demo board")
            return self.meas.read(self.settings, cals)
        if self.logic is None:
            raise LogicError("not connected to Logic 2")
        st = self.settings
        keys = st.sensor_keys
        if not isinstance(cals, dict):
            cals = {keys[0]: cals}

        channels = [st.channel_of(k) for k in keys]
        self._sync_logic_settings(channels)

        stimulating = False
        n_pulses = 0
        if st.needs_stimulus:
            driven = next(k for k in keys if SENSORS[k].needs_stimulus)
            if not self.board_ready:
                raise LogicError(
                    f"{driven} produces nothing without a ResetTemp06 burst, "
                    f"but the demo board is not connected"
                )
            # Cover the arming latency of start_capture plus the capture itself,
            # so the whole capture window sees pulses rather than a dead line.
            period_us = st.reset_high_us + st.reset_low_us + TTBoard.PULSE_LOOP_OVERHEAD_US
            n_pulses = max(1, int((st.duration_s + 1.0) * 1e6 / period_us))
            self.board.pulse_ui_in_begin(
                SENSORS[driven].ui_bit, n_pulses, st.reset_high_us, st.reset_low_us
            )
            stimulating = True

        try:
            trains = self.logic.capture()
        finally:
            if stimulating:
                try:
                    self.board.pulse_ui_in_end(n_pulses, st.reset_high_us, st.reset_low_us)
                except Exception as exc:
                    self._log(f"reset burst did not finish cleanly: {exc}")

        if self.logic.last_rate_note:
            self._log(self.logic.last_rate_note)
            self.logic.last_rate_note = ""

        out: Dict[str, Reading] = {}
        for key in keys:
            spec = SENSORS[key]
            train = trains[st.channel_of(key)]
            cal = cals.get(key) or Calibration(key)
            reading = reduce_train(
                train, spec, cal, band=st.outlier_band, bin_s=st.bin_s
            )
            reading.sample_rate = self.logic.actual_sample_rate
            if spec.needs_stimulus and reading.n:
                reading.note = f"{n_pulses} reset pulses, {reading.n} responses captured"
            out[key] = reading
        return out

    def detect_channels(self, channels: Optional[List[int]] = None) -> Dict[int, dict]:
        """Report which Saleae channels are carrying a signal (wiring aid)."""
        if self.logic is None:
            raise LogicError("not connected to Logic 2")
        chans = channels if channels is not None else list(range(8))
        return self.logic.scan_channels(chans, duration_s=0.01, sample_rate=100_000_000)
