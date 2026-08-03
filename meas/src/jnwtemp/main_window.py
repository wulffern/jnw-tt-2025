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
"""The live demo window: instrument status, controls, and three plots."""

from __future__ import annotations

import os
import time
from typing import Optional

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .acquire import (
    BOTH,
    SOURCE_LOGIC,
    DEFAULT_CHANNELS as SENSOR_CHANNELS,
    SENSORS,
    AcquireSettings,
    Reading,
)
from .board import MAX_PROJECT_CLOCK_HZ, find_ports
from .chamber import DEFAULT_ADDRESS, DEFAULT_HOST, DEFAULT_PORT
from .chamber_worker import ChamberThread, SweepPlan
from .logic import OFFERED_RATES, THRESHOLDS_V
from .plots import (
    GRID,
    SENSOR_COLORS,
    SERIES_1,
    SERIES_2,
    STATUS_BAD,
    STATUS_GOOD,
    STATUS_WARN,
    SURFACE,
    SURFACE_2,
    TEXT_MUTED,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
    ObservablePlot,
    SpectrumPlot,
    StatTile,
    TemperaturePlot,
)
from .cicwave_plot import AVAILABLE as CICWAVE_PLOT, KEYMAP, LiveWavePlot
from .recorder import TemperatureRecorder, export_capture
from .spectrum import (
    History,
    allan_deviation,
    integrated_noise,
    phase_noise,
    welch_psd,
)
from .temperature import CalibrationStore, resolution_k
from .worker import AcquireThread


#: What the temperature trace plots. All of these are affine functions of the
#: measured rate - the curve has the same shape in each - so they are a choice
#: rather than overlays: two of them on one plot would say nothing the axis
#: label does not already say.
TRACE_MODES = [
    ("Temperature", "temp"),
    ("Sensor rate", "rate"),
    ("Frequency error from first (ppm)", "ppm"),
    ("Frequency error from first (Hz)", "hz"),
]

#: Unit of each trace mode, which is also the y axis it lands on.
TRACE_UNITS = {"temp": "°C", "rate": "kHz", "ppm": "ppm", "hz": "Hz"}

#: How often the chamber is interrogated. Fast enough that its curve reads as a
#: line next to the sensors rather than a staircase, and still far slower than
#: a chamber can actually move; the socket is on its own thread, so the trace
#: never waits for it.
CHAMBER_POLL_S = 1.0

#: What the lower pane shows. The raw per-event trace lives here rather than in
#: a permanent third pane: it is worth looking at occasionally, not constantly,
#: and two panes leave the temperature trace room to be read.
#: Floor on how often the lower pane is recomputed while readings stream in.
#: A spectrum of one window is a diagnostic you read, not a meter you watch;
#: recomputing and repainting it at the reading rate was pure overhead.
SPECTRUM_MIN_INTERVAL_S = 1.0

SPECTRUM_MODES = [
    ("Within capture (conversion noise)", "fast"),
    ("Long term (drift + 1/f)", "slow"),
    ("Allan deviation", "allan"),
    ("Phase noise (both)", "phase"),
    ("Last capture - raw timing", "raw"),
]

#: What the demo board offers instead. Its counters never stop, so the trace is
#: one uninterrupted record and a spectrum of the whole of it beats a spectrum
#: of one averaged point per update - same quantity, a hundred times the
#: sampling. "Long term" is dropped because this is it, done properly.
BOARD_SPECTRUM_MODES = [
    ("Whole record (drift + 1/f)", "trace"),
    ("Within capture (per-event)", "fast"),
    ("Allan deviation", "allan"),
    ("Phase noise (per-event)", "phase"),
    ("Last capture - raw timing", "raw"),
]


#: A chip never grows past this. Instrument errors arrive as whole exception
#: strings - a gRPC failure is a paragraph - and a QLabel sized to one would
#: drag the window wider than the screen. The full text is in the tooltip.
CHIP_MAX_PX = 260


class StatusChip(QLabel):
    """A small colored pill showing the state of one instrument."""

    def __init__(self, name: str) -> None:
        super().__init__()
        self._name = name
        self.setMaximumWidth(CHIP_MAX_PX)
        self.set_state("unknown", "not connected")

    def set_state(self, state: str, detail: str = "") -> None:
        color = {
            "ok": STATUS_GOOD,
            "warn": STATUS_WARN,
            "bad": STATUS_BAD,
        }.get(state, TEXT_MUTED)
        detail = " ".join(detail.split())          # collapse multi-line errors
        full = f"{self._name}: {detail}" if detail else self._name
        # Elide against the chip's own budget, less the padding and border.
        self.setText(self.fontMetrics().elidedText(
            full, Qt.TextElideMode.ElideRight, CHIP_MAX_PX - 24))
        self.setToolTip(full)
        self.setStyleSheet(
            f"QLabel {{ color:{color}; border:1px solid {color}; border-radius:6px;"
            f" padding:3px 10px; background:{SURFACE_2}; font-size:11px; }}"
        )


class MainWindow(QWidget):
    def __init__(self, board_port: Optional[str] = None, use_board: bool = True) -> None:
        super().__init__()
        self.setWindowTitle("JNW-TEMP - live temperature sensor demo (TT project 258)")

        self.settings = self._make_settings()
        self.cal_store = CalibrationStore()
        self._board_port = board_port
        self._use_board = use_board
        self.thread: Optional[AcquireThread] = None

        # Two histories, deliberately.
        #  trace:   one point per time bin (~1 ms) - what the plot shows. It is
        #           NOT uniformly sampled: captures are bursts separated by dead
        #           time, so it must not be fed to the PSD or Allan routines.
        #           Held as the raw rate in kHz and converted when drawn, so the
        #           trace survives a calibration instead of being invalidated
        #           by it, and the plotted quantity is free to change.
        #  history: one averaged point per capture - uniform enough in time to
        #           be the basis for the long-term spectrum and Allan deviation.
        self.traces: dict = {}     # sensor -> fine binned rate in kHz (plot only)
        self.histories: dict = {}  # sensor -> per-capture means (spectra, Allan)
        #: rate each sensor started at, the reference for the ppm/Hz trace modes
        self._ref_rate: dict = {}
        #: seconds since _t0 that the lower pane analyses from. A spectrum of
        #: the whole record is only meaningful while the record is one thing:
        #: warm a finger on the chip and the step dominates every average for
        #: minutes afterwards, so it has to be possible to start again without
        #: throwing away the trace you were watching.
        self._spec_from = 0.0
        self._t0 = time.time()
        self._last: Optional[Reading] = None
        self._readings = 0
        self.recorder: Optional[TemperatureRecorder] = None
        #: identities reported by the instruments at connect, for provenance
        self._instruments: dict = {}
        #: The Vötsch chamber runs in its own thread, like the instruments.
        self.chamber_thread: Optional[ChamberThread] = None
        #: Latest chamber reading, kept so a manual calibration and the recorder
        #: can both stamp the temperature the chamber is actually holding.
        self._chamber_state: Optional[object] = None
        #: The chamber's own thermometer over time, drawn under the sensors as a
        #: reference. Same clock as the sensor traces, so it is directly
        #: comparable; it is not a History of a *measurement* and so takes no
        #: part in the statistics or the spectrum.
        self._chamber_trace = History(maxlen=200_000)
        self._sweeping = False

        self._build_ui()
        self._apply_dark_theme()
        self.resize(*self._initial_size())
        self._refresh_calibration_labels()

    #: Lower-pane modes offered by this window; the instrument decides which
    #: spectra are meaningful.
    spectrum_modes = SPECTRUM_MODES

    def _make_settings(self) -> AcquireSettings:
        """Defaults for this window's instrument."""
        return AcquireSettings(source=SOURCE_LOGIC)

    #: What the window would like to be, given a screen big enough for it.
    PREFERRED_SIZE = (1500, 940)

    def _initial_size(self) -> tuple:
        """The preferred size, clipped to what the screen can actually show.

        The side panel scrolls, but only inside the window: a window taller
        than the display puts its lower edge - and the bottom of the panel with
        it - somewhere that cannot be reached or scrolled to. So the opening
        size follows the available geometry, which already excludes the taskbar.
        """
        want_w, want_h = self.PREFERRED_SIZE
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            # Slack for the window frame and title bar, which availableGeometry
            # does not account for.
            want_w = min(want_w, avail.width() - 20)
            want_h = min(want_h, avail.height() - 60)
        return max(640, want_w), max(480, want_h)

    # ------------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        self.chip_logic = StatusChip("Logic 2")
        self.chip_board = StatusChip("Demo board")
        self.chip_project = StatusChip("Project")
        self.chip_chamber = StatusChip("Chamber")
        self.status_label = QLabel("Idle")
        self.status_label.setStyleSheet(f"color:{TEXT_MUTED}; font-size:11px;")

        self.btn_connect = QPushButton("Connect")
        self.btn_connect.clicked.connect(self._on_connect)
        self.btn_connect.setFixedWidth(110)

        top = QHBoxLayout()
        top.setContentsMargins(8, 6, 8, 0)
        top.setSpacing(8)
        for chip in (self.chip_logic, self.chip_board, self.chip_project,
                     self.chip_chamber):
            top.addWidget(chip)
        top.addWidget(self.status_label)
        top.addStretch(1)
        top.addWidget(self.btn_connect)

        # --- hero readouts
        self.tile_temp = StatTile("ESTIMATED TEMPERATURE", " °C", SERIES_1)
        self.tile_rate = StatTile("SENSOR RATE", " kHz", TEXT_PRIMARY)
        self.tile_noise = StatTile("NOISE (1 sigma)", " °C", TEXT_PRIMARY)
        tiles = QHBoxLayout()
        tiles.setContentsMargins(8, 6, 8, 0)
        tiles.setSpacing(8)
        tiles.addWidget(self.tile_temp, 2)
        tiles.addWidget(self.tile_rate, 1)
        tiles.addWidget(self.tile_noise, 1)

        # --- plots
        # cicwave's waveform plot brings A/B cursors with a delta readout and
        # the keymap this project already has muscle memory for, so use it for
        # the trace when it is installed and fall back otherwise.
        self.plot_temp = LiveWavePlot("time_s") if CICWAVE_PLOT else TemperaturePlot()
        self.plot_obs = ObservablePlot()
        self.plot_spec = SpectrumPlot()
        # The lower pane is one slot showing whichever view is selected, so the
        # temperature trace gets the height instead of a permanently visible
        # third plot.
        self.lower = QStackedWidget()
        self.lower.addWidget(self.plot_spec)
        self.lower.addWidget(self.plot_obs)

        plots = QSplitter(Qt.Orientation.Vertical)
        plots.addWidget(self.plot_temp)
        plots.addWidget(self.lower)
        plots.setSizes([520, 380])

        left = QVBoxLayout()
        left.setContentsMargins(0, 0, 0, 0)
        left.addLayout(tiles)
        left.addWidget(plots, 1)
        left_w = QWidget()
        left_w.setLayout(left)

        body = QHBoxLayout()
        body.addWidget(left_w, 1)
        body.addWidget(self._build_controls(), 0)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 8, 8)
        root.addLayout(top, 0)
        root.addLayout(body, 1)

    def _collapsible(self, title: str, inner: QWidget) -> QWidget:
        """A section that folds away. Used for controls set once and forgotten.

        Everything in here has a working default - the sample rate is negotiated
        with the device, the threshold suits both logic levels, the clock runs as
        fast as it goes - so showing it permanently costs panel height for
        settings nobody touches twice.
        """
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        btn = QToolButton()
        btn.setText(title)
        btn.setCheckable(True)
        btn.setChecked(False)
        btn.setArrowType(Qt.ArrowType.RightArrow)
        btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        btn.setStyleSheet(
            f"QToolButton {{ border:none; color:{TEXT_MUTED}; font-size:11px;"
            f" letter-spacing:1px; padding:2px 0; }}"
            f"QToolButton:hover {{ color:{SERIES_1}; }}"
        )
        inner.setVisible(False)

        def toggled(on: bool) -> None:
            inner.setVisible(on)
            btn.setArrowType(Qt.ArrowType.DownArrow if on else Qt.ArrowType.RightArrow)

        btn.toggled.connect(toggled)
        lay.addWidget(btn)
        lay.addWidget(inner)
        return box

    def _build_controls(self) -> QWidget:
        """Assemble the right-hand panel from its groups.

        The groups are separate methods so a window for a different instrument
        can replace the ones that are instrument-specific and inherit the rest;
        see :class:`~jnwtemp.board_window.BoardWindow`.
        """
        panel = QWidget()
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(0, 6, 6, 0)
        outer.setSpacing(8)
        for group in (self._group_measure(), self._group_calibration(),
                      self._group_chamber(), self._group_record(),
                      self._group_display()):
            outer.addWidget(group)
        outer.addWidget(self._collapsible("SETUP / WIRING", self._group_setup()))
        outer.addWidget(self._build_log(), 1)

        # The panel must never dictate the window height: on a laptop screen the
        # full stack is taller than the display, so it scrolls instead.
        scroll = QScrollArea()
        scroll.setWidget(panel)
        scroll.setWidgetResizable(True)
        scroll.setFixedWidth(356)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        return scroll

    def _build_log(self) -> QWidget:
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        self.log.setMinimumHeight(90)
        self.log.setStyleSheet(
            f"QPlainTextEdit {{ background:{SURFACE_2}; color:{TEXT_MUTED};"
            f" border:1px solid {GRID}; border-radius:6px; font-size:11px; }}"
        )
        return self.log

    def _sensor_row(self, form) -> None:
        """The sensor selector, shared by every window."""
        self.combo_sensor = QComboBox()
        for key, spec in SENSORS.items():
            self.combo_sensor.addItem(spec.label, key)
        # Both sensors are measured together, so they share a time base - the
        # only way the comparison means anything.
        self.combo_sensor.addItem("Both - GR07 + GR06 together", BOTH)
        self.combo_sensor.currentIndexChanged.connect(self._on_sensor_changed)
        form.addRow("Sensor", self.combo_sensor)

    def _trace_bin_row(self, form) -> None:
        # Each capture is re-reduced into bins this wide, so the trace shows
        # what happened *inside* a capture instead of one averaged dot.
        self.spin_bin = QDoubleSpinBox()
        self.spin_bin.setRange(0.0, 500.0)
        self.spin_bin.setDecimals(2)
        self.spin_bin.setSingleStep(0.5)
        self.spin_bin.setSuffix(" ms")
        self.spin_bin.setSpecialValueText("off (1 pt/capture)")
        self.spin_bin.setValue(self.settings.bin_ms)
        self.spin_bin.setToolTip(
            "Time resolution of the temperature trace. Smaller bins show more "
            "noise; each point is worth fewer events, so it is also noisier."
        )
        self.spin_bin.valueChanged.connect(self._on_bin_changed)
        form.addRow("Trace bin", self.spin_bin)

        self.lbl_bin = QLabel("")
        self.lbl_bin.setWordWrap(True)
        self.lbl_bin.setMinimumHeight(30)
        self.lbl_bin.setStyleSheet(f"color:{TEXT_MUTED}; font-size:11px;")
        form.addRow(self.lbl_bin)

    def _run_row(self, form) -> None:
        self.btn_run = QPushButton("Start")
        self.btn_run.setEnabled(False)
        self.btn_run.clicked.connect(self._on_run_toggled)
        form.addRow(self.btn_run)

    def _group_measure(self) -> QWidget:
        # Connect lives in the status bar, beside the chips it affects.
        meas = QGroupBox("Measure")
        sf = QFormLayout(meas)
        self._sensor_row(sf)

        self.spin_duration = QDoubleSpinBox()
        self.spin_duration.setRange(0.005, 5.0)
        self.spin_duration.setDecimals(3)
        self.spin_duration.setSingleStep(0.05)
        self.spin_duration.setSuffix(" s")
        self.spin_duration.setValue(self.settings.duration_s)
        self.spin_duration.valueChanged.connect(self._push_settings)
        sf.addRow("Capture", self.spin_duration)

        self._trace_bin_row(sf)
        self._run_row(sf)
        return meas

    def _group_calibration(self) -> QWidget:
        calib = QGroupBox("Calibration")
        kf = QFormLayout(calib)
        self.spin_ref = QDoubleSpinBox()
        self.spin_ref.setRange(-50.0, 150.0)
        self.spin_ref.setDecimals(2)
        self.spin_ref.setValue(23.0)
        self.spin_ref.setSuffix(" \u00b0C")
        kf.addRow("Reference", self.spin_ref)

        self.btn_calibrate = QPushButton("Calibrate here")
        self.btn_calibrate.clicked.connect(self._on_calibrate)
        self.btn_clear_cal = QPushButton("Clear")
        self.btn_clear_cal.clicked.connect(self._on_clear_cal)
        row = QHBoxLayout()
        row.addWidget(self.btn_calibrate)
        row.addWidget(self.btn_clear_cal)
        kf.addRow(row)

        self.lbl_cal = QLabel("uncalibrated")
        self.lbl_cal.setWordWrap(True)
        # Two wrapped lines of model + resolution; without a floor the group box
        # shrinks to one line and clips the rest.
        self.lbl_cal.setMinimumHeight(46)
        self.lbl_cal.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.lbl_cal.setStyleSheet(f"color:{TEXT_MUTED}; font-size:11px;")
        kf.addRow(self.lbl_cal)
        return calib

    def _group_chamber(self) -> QWidget:
        """Vötsch VT chamber: connect, hold a setpoint, and sweep temperature.

        The chamber is a second, independent instrument. It is connected and
        driven on its own thread so its ~2 s polling never stalls the trace, and
        its temperature is folded into the recording so a sweep and the sensor
        readings land in one file.
        """
        box = QGroupBox("Temperature chamber")
        form = QFormLayout(box)

        self.edit_chamber_host = QLineEdit(DEFAULT_HOST)
        self.spin_chamber_port = QSpinBox()
        self.spin_chamber_port.setRange(1, 65535)
        self.spin_chamber_port.setValue(DEFAULT_PORT)
        addr = QHBoxLayout()
        addr.setContentsMargins(0, 0, 0, 0)
        addr.addWidget(self.edit_chamber_host, 1)
        addr.addWidget(self.spin_chamber_port, 0)
        form.addRow("Address", addr)

        # This unit answers on 01; 00 (the usual default) is met with silence.
        self.spin_chamber_addr = QSpinBox()
        self.spin_chamber_addr.setRange(0, 99)
        self.spin_chamber_addr.setValue(DEFAULT_ADDRESS)
        self.spin_chamber_addr.setToolTip(
            "ASCII-2 device address. The VT on this bench answers on 01, not "
            "the more common 00."
        )
        form.addRow("Device no.", self.spin_chamber_addr)

        self.btn_chamber_connect = QPushButton("Connect chamber")
        self.btn_chamber_connect.clicked.connect(self._on_chamber_connect)
        form.addRow(self.btn_chamber_connect)

        self.lbl_chamber = QLabel("not connected")
        self.lbl_chamber.setWordWrap(True)
        self.lbl_chamber.setMinimumHeight(30)
        self.lbl_chamber.setStyleSheet(f"color:{TEXT_MUTED}; font-size:11px;")
        form.addRow(self.lbl_chamber)

        self.spin_chamber_set = QDoubleSpinBox()
        self.spin_chamber_set.setRange(-80.0, 190.0)
        self.spin_chamber_set.setDecimals(1)
        self.spin_chamber_set.setSingleStep(1.0)
        self.spin_chamber_set.setValue(25.0)
        self.spin_chamber_set.setSuffix(" °C")
        form.addRow("Setpoint", self.spin_chamber_set)

        self.btn_chamber_set = QPushButton("Set + start")
        self.btn_chamber_set.clicked.connect(self._on_chamber_set)
        self.btn_chamber_off = QPushButton("Stop chamber")
        self.btn_chamber_off.clicked.connect(self._on_chamber_off)
        setrow = QHBoxLayout()
        setrow.addWidget(self.btn_chamber_set)
        setrow.addWidget(self.btn_chamber_off)
        form.addRow(setrow)

        self.btn_cal_chamber = QPushButton("Calibrate at chamber temp")
        self.btn_cal_chamber.setToolTip(
            "Add a calibration point using the chamber's actual temperature as "
            "the reference and the latest sensor reading's rate. Let the chamber "
            "stabilise first."
        )
        self.btn_cal_chamber.clicked.connect(self._on_calibrate_chamber)
        form.addRow(self.btn_cal_chamber)

        # --- sweep. The four parameters fold away: they are set once for a run
        # and then left alone, and this group is the tallest in the panel. The
        # button and the progress line stay out where they can be reached.
        sweep_box = QWidget()
        sf = QFormLayout(sweep_box)
        sf.setContentsMargins(0, 0, 0, 0)

        self.spin_sweep_start = self._sweep_spin(-40.0)
        self.spin_sweep_stop = self._sweep_spin(85.0)
        self.spin_sweep_step = self._sweep_spin(10.0, low=-100.0)
        sweep_range = QHBoxLayout()
        sweep_range.setContentsMargins(0, 0, 0, 0)
        for w in (self.spin_sweep_start, self.spin_sweep_stop, self.spin_sweep_step):
            sweep_range.addWidget(w)
        sf.addRow("Sweep °C", sweep_range)
        self.spin_sweep_start.setToolTip("Start temperature")
        self.spin_sweep_stop.setToolTip("Stop temperature (inclusive)")
        self.spin_sweep_step.setToolTip("Step; sign is taken from start→stop")

        self.spin_sweep_tol = QDoubleSpinBox()
        self.spin_sweep_tol.setRange(0.05, 10.0)
        self.spin_sweep_tol.setDecimals(2)
        self.spin_sweep_tol.setValue(0.3)
        self.spin_sweep_tol.setSuffix(" °C")
        sf.addRow("Stabilise ±", self.spin_sweep_tol)

        self.spin_sweep_soak = QSpinBox()
        self.spin_sweep_soak.setRange(0, 7200)
        self.spin_sweep_soak.setValue(120)
        self.spin_sweep_soak.setSuffix(" s")
        self.spin_sweep_soak.setToolTip(
            "How long the actual temperature must stay within tolerance before "
            "the point counts as stabilised."
        )
        sf.addRow("Soak", self.spin_sweep_soak)

        self.spin_sweep_dwell = QSpinBox()
        self.spin_sweep_dwell.setRange(0, 86400)
        self.spin_sweep_dwell.setValue(300)
        self.spin_sweep_dwell.setSuffix(" s")
        self.spin_sweep_dwell.setToolTip(
            "How long to hold each point after it stabilises - the measurement "
            "plateau that gets recorded."
        )
        sf.addRow("Dwell", self.spin_sweep_dwell)
        form.addRow(self._collapsible("SWEEP SETTINGS", sweep_box))

        self.btn_sweep = QPushButton("Start sweep")
        self.btn_sweep.clicked.connect(self._on_sweep_toggled)
        form.addRow(self.btn_sweep)

        self.lbl_sweep = QLabel("idle")
        self.lbl_sweep.setWordWrap(True)
        self.lbl_sweep.setMinimumHeight(30)
        self.lbl_sweep.setStyleSheet(f"color:{TEXT_MUTED}; font-size:11px;")
        form.addRow(self.lbl_sweep)

        self._set_chamber_controls_enabled(False)
        return box

    def _sweep_spin(self, value: float, low: float = -80.0) -> QDoubleSpinBox:
        s = QDoubleSpinBox()
        s.setRange(low, 190.0)
        s.setDecimals(1)
        s.setSingleStep(5.0)
        s.setValue(value)
        s.setSuffix(" °C")
        return s

    def _set_chamber_controls_enabled(self, on: bool) -> None:
        for w in (self.btn_chamber_set, self.btn_chamber_off, self.btn_cal_chamber,
                  self.btn_sweep):
            w.setEnabled(on)

    def _group_record(self) -> QWidget:
        rec = QGroupBox("Record")
        rf = QVBoxLayout(rec)
        self.btn_record = QPushButton("Record to CSV...")
        self.btn_record.clicked.connect(self._on_record)
        self.btn_record_stop = QPushButton("Stop recording")
        self.btn_record_stop.clicked.connect(self._on_record_stop)
        self.btn_record_stop.setEnabled(False)
        self.lbl_record = QLabel("not recording")
        self.lbl_record.setWordWrap(True)
        self.lbl_record.setMinimumHeight(30)
        self.lbl_record.setStyleSheet(f"color:{TEXT_MUTED}; font-size:11px;")
        rf.addWidget(self.btn_record)
        rf.addWidget(self.btn_record_stop)
        rf.addWidget(self.lbl_record)
        return rec

    def _group_display(self) -> QWidget:
        disp = QGroupBox("Display")
        df = QFormLayout(disp)
        self.combo_trace = QComboBox()
        for label, key in TRACE_MODES:
            self.combo_trace.addItem(label, key)
        self.combo_trace.setToolTip(
            "What the top plot shows. The trace itself is stored as the raw "
            "rate, so switching costs nothing and calibrating does not throw "
            "the history away.\n"
            "Frequency error is referred to the first point since the history "
            "was last cleared; for a PTAT 1 ppm is about 0.3 mK."
        )
        self.combo_trace.currentIndexChanged.connect(self._redraw_trace)
        df.addRow("Top plot", self.combo_trace)
        self.combo_spec = QComboBox()
        for label, key in self.spectrum_modes:
            self.combo_spec.addItem(label, key)
        self.combo_spec.currentIndexChanged.connect(self._redraw_spectrum)
        df.addRow("Bottom plot", self.combo_spec)
        self.btn_reset_spec = QPushButton("Restart spectrum")
        self.btn_reset_spec.setToolTip(
            "Analyse the lower pane from now on. The trace, the tiles and the "
            "recording are untouched - this only discards the history the "
            "spectrum and the Allan deviation average over, which is what you "
            "want after the temperature has deliberately moved."
        )
        self.btn_reset_spec.clicked.connect(self._on_reset_spectrum)
        df.addRow(self.btn_reset_spec)
        self.btn_reset = QPushButton("Clear history")
        self.btn_reset.clicked.connect(self._on_clear_history)
        df.addRow(self.btn_reset)
        return disp

    def _group_setup(self) -> QWidget:
        """Wiring and one-time configuration: Saleae channel, rate, threshold."""
        setup = QWidget()
        gf = QFormLayout(setup)
        gf.setContentsMargins(0, 4, 0, 0)

        self.chk_board = QCheckBox("Use demo board (closed loop)")
        self.chk_board.setChecked(self._use_board)
        gf.addRow(self.chk_board)

        self.combo_port = QComboBox()
        self.combo_port.addItem("auto", None)
        for port in find_ports():
            self.combo_port.addItem(port, port)
        gf.addRow("Serial port", self.combo_port)

        self.spin_channel = QSpinBox()
        self.spin_channel.setRange(0, 15)
        self.spin_channel.setValue(self.settings.channel)
        self.spin_channel.valueChanged.connect(self._push_settings)
        gf.addRow("Saleae D", self.spin_channel)

        self.btn_detect = QPushButton("Detect active channels")
        self.btn_detect.clicked.connect(self._on_detect)
        self.btn_detect.setEnabled(False)
        gf.addRow(self.btn_detect)

        self.combo_rate = QComboBox()
        for rate in OFFERED_RATES:
            self.combo_rate.addItem(f"{rate/1e6:.0f} MS/s", rate)
        self.combo_rate.setToolTip(
            "Requested rate. The device is asked for this and negotiates down "
            "if the channel count does not allow it."
        )
        self.combo_rate.currentIndexChanged.connect(self._push_settings)
        gf.addRow("Sample rate", self.combo_rate)

        # The Logic Pro accepts exactly three digital thresholds; a free spin
        # box would just let the user pick one the device rejects.
        self.combo_threshold = QComboBox()
        for v in THRESHOLDS_V:
            self.combo_threshold.addItem(f"{v:.1f} V", v)
        self.combo_threshold.currentIndexChanged.connect(self._push_settings)
        gf.addRow("Threshold", self.combo_threshold)

        self.spin_clock = QSpinBox()
        self.spin_clock.setRange(1, MAX_PROJECT_CLOCK_HZ // 1_000_000)
        self.spin_clock.setValue(self.settings.clock_hz // 1_000_000)
        self.spin_clock.setSuffix(" MHz")
        self.spin_clock.valueChanged.connect(self._push_settings)
        gf.addRow("Project clock", self.spin_clock)

        self.spin_reset_high = QSpinBox()
        self.spin_reset_high.setRange(1, 10_000)
        self.spin_reset_high.setValue(self.settings.reset_high_us)
        self.spin_reset_high.setSuffix(" us")
        self.spin_reset_high.valueChanged.connect(self._push_settings)
        gf.addRow("Reset high", self.spin_reset_high)

        self.spin_reset_low = QSpinBox()
        self.spin_reset_low.setRange(1, 100_000)
        self.spin_reset_low.setValue(self.settings.reset_low_us)
        self.spin_reset_low.setSuffix(" us")
        self.spin_reset_low.valueChanged.connect(self._push_settings)
        gf.addRow("Reset low", self.spin_reset_low)

        self.btn_apply_chip = QPushButton("Apply to chip")
        self.btn_apply_chip.clicked.connect(self._on_apply_chip)
        self.btn_apply_chip.setEnabled(False)
        gf.addRow(self.btn_apply_chip)

        self.btn_export_capture = QPushButton("Export last capture...")
        self.btn_export_capture.setToolTip(
            "Per-event data of the most recent capture (every period/pulse), "
            "for plotting in cicwave. .csv, .parquet or .feather."
        )
        self.btn_export_capture.clicked.connect(self._on_export_capture)
        gf.addRow(self.btn_export_capture)
        return setup

    def _apply_dark_theme(self) -> None:
        self.setStyleSheet(
            f"""
            QWidget {{ background:{SURFACE}; color:{TEXT_SECONDARY}; font-size:12px; }}
            QGroupBox {{ border:1px solid {GRID}; border-radius:8px; margin-top:10px;
                         padding:8px 8px 6px 8px; }}
            QGroupBox::title {{ subcontrol-origin: margin; left:10px; padding:0 4px;
                                color:{TEXT_MUTED}; font-size:11px; letter-spacing:1px; }}
            QPushButton {{ background:{SURFACE_2}; border:1px solid {GRID};
                           border-radius:6px; padding:6px 10px; color:{TEXT_PRIMARY}; }}
            QPushButton:hover:enabled {{ border-color:{SERIES_1}; color:{SERIES_1}; }}
            QPushButton:disabled {{ color:{TEXT_MUTED}; border-color:{GRID}; }}
            QComboBox, QSpinBox, QDoubleSpinBox {{ background:{SURFACE_2};
                border:1px solid {GRID}; border-radius:6px; padding:3px 6px;
                color:{TEXT_PRIMARY}; }}
            QCheckBox {{ color:{TEXT_SECONDARY}; }}
            QSplitter::handle {{ background:{GRID}; }}
            """
        )

    # ------------------------------------------------------------- settings
    def _collect_settings(self) -> AcquireSettings:
        st = self.settings
        st.sensor = self.combo_sensor.currentData()
        st.clock_hz = self.spin_clock.value() * 1_000_000
        st.reset_high_us = self.spin_reset_high.value()
        st.reset_low_us = self.spin_reset_low.value()
        st.bin_ms = float(self.spin_bin.value())
        self._collect_instrument_settings(st)
        return st

    def _collect_instrument_settings(self, st: AcquireSettings) -> None:
        """Fields that only exist for this window's instrument."""
        st.source = SOURCE_LOGIC
        st.channels[st.sensor_keys[0]] = self.spin_channel.value()
        st.sample_rate = int(self.combo_rate.currentData())
        st.threshold_volts = float(self.combo_threshold.currentData())
        st.duration_s = float(self.spin_duration.value())

    def _sync_channel_widget(self, keys) -> None:
        """Point the Saleae channel box at the primary sensor's channel."""
        self.spin_channel.blockSignals(True)
        self.spin_channel.setValue(self.settings.channels.get(keys[0], 0))
        self.spin_channel.blockSignals(False)

    def _use_board_now(self) -> bool:
        return self.chk_board.isChecked()

    def _push_settings(self) -> None:
        st = self._collect_settings()
        if self.thread is not None:
            self.thread.update_settings(st)

    def _on_bin_changed(self) -> None:
        # Points already in the trace were binned at the old width; mixing
        # resolutions in one series would misrepresent the noise.
        for h in self.traces.values():
            h.clear()
        self._ref_rate.clear()
        self.plot_temp.clear_data()
        self._push_settings()

    def _on_sensor_changed(self) -> None:
        key = self.combo_sensor.currentData()
        keys = list(SENSORS) if key == BOTH else [key]
        self._sync_channel_widget(keys)
        stim = any(SENSORS[k].needs_stimulus for k in keys)
        self.spin_reset_high.setEnabled(stim)
        self.spin_reset_low.setEnabled(stim)
        for k in keys:
            self._append_log(f"{k}: {SENSORS[k].doc}")
        self._on_clear_history()
        self._push_settings()
        self._refresh_calibration_labels()

    @property
    def cal(self):
        """Calibration of the primary sensor (GR07 leads in dual mode)."""
        return self.cal_store.get(self.settings.sensor_keys[0])

    @property
    def cals(self) -> dict:
        return {k: self.cal_store.get(k) for k in self.settings.sensor_keys}

    def _refresh_calibration_labels(self) -> None:
        cal = self.cal
        self.lbl_cal.setText(cal.describe())
        st = self.settings
        if cal.calibrated and self._last is not None and self._last.ok:
            lsb = st.timing_lsb_s(self._last.sample_rate)
            res = resolution_k(self._last.mean_rate_hz, lsb, cal)
            self.lbl_cal.setText(
                f"{cal.describe()}\nsingle-event step ≈ {res:.3f} K "
                f"({lsb*1e9:.2f} ns); averaging dithers through it"
            )

    # ------------------------------------------------------------ instrument
    def _on_connect(self) -> None:
        if self.thread is not None:
            self._teardown_thread()
            self.btn_connect.setText("Connect")
            self.btn_run.setEnabled(False)
            self.btn_detect.setEnabled(False)
            self.btn_apply_chip.setEnabled(False)
            for chip in (self.chip_logic, self.chip_board, self.chip_project):
                chip.set_state("unknown", "not connected")
            return

        st = self._collect_settings()
        self.thread = AcquireThread(
            st,
            self.cals,
            board_port=self.combo_port.currentData(),
            use_board=self._use_board_now(),
        )
        self.thread.opened.connect(self._on_opened)
        self.thread.readingReady.connect(self._on_reading)
        self.thread.detected.connect(self._on_detected)
        self.thread.logMessage.connect(self._append_log)
        self.thread.statusMessage.connect(lambda m: self.status_label.setText(m))
        self.thread.errorMessage.connect(self._on_error)
        self.thread.runStateChanged.connect(self._on_run_state)
        self.thread.start()
        self.btn_connect.setText("Disconnect")
        self.status_label.setText("Connecting...")

    def _teardown_thread(self) -> None:
        if self.thread is None:
            return
        self.thread.shutdown()
        if not self.thread.wait(5000):
            self.thread.terminate()
            self.thread.wait(1000)
        self.thread = None

    def _on_opened(self, status: dict) -> None:
        logic = status.get("logic", "")
        board = status.get("board", "")
        if logic.startswith("failed"):
            self.chip_logic.set_state("bad", logic)
            self.btn_run.setEnabled(False)
        else:
            self.chip_logic.set_state("ok", logic)
            self.btn_run.setEnabled(True)
            self.btn_detect.setEnabled(True)
            self.status_label.setText("Connected - press Start")

        if board in ("disabled",) or board.startswith("unavailable"):
            self.chip_board.set_state("warn", board)
            self.chip_project.set_state("warn", "not verified")
        else:
            self.chip_board.set_state("ok", board.split("|")[0].strip())
            self.chip_project.set_state("ok", "tt_um_jnw_wulffern (258)")
            self.btn_apply_chip.setEnabled(True)
        self._instruments = {"logic2": logic, "demo_board": board}
        self._append_log(f"Logic 2: {logic}")
        self._append_log(f"Board: {board}")

    def _on_apply_chip(self) -> None:
        self._push_settings()
        if self.thread is not None:
            self.thread.request_configure()

    def _on_detect(self) -> None:
        if self.thread is not None:
            self.thread.request_detect(list(range(8)))

    def _on_detected(self, report: dict) -> None:
        lines = ["Channel scan:"]
        for ch in sorted(report):
            v = report[ch]
            if v["edges"] < 2:
                lines.append(f"  D{ch}: idle (level {v['level']})")
            else:
                lines.append(
                    f"  D{ch}: {v['edges']} edges, {v['freq']/1e3:.1f} kHz, duty {v['duty']*100:.1f}%"
                )
        self._append_log("\n".join(lines))

    # --------------------------------------------------------------- running
    def _on_run_toggled(self) -> None:
        if self.thread is None:
            return
        if self.btn_run.text() == "Start":
            self._push_settings()
            self.thread.update_calibration(self.cals)
            self.thread.start_acquiring()
        else:
            self.thread.stop_acquiring()

    def _on_run_state(self, running: bool) -> None:
        self.btn_run.setText("Stop" if running else "Start")

    def _on_error(self, msg: str) -> None:
        self._append_log(f"ERROR: {msg}")
        self.status_label.setText("Error - see log")

    def _append_log(self, msg: str) -> None:
        self.log.appendPlainText(msg)

    # ---------------------------------------------------------------- data
    # ---------------------------------------------------------------- data
    def _trace_for(self, key: str) -> History:
        if key not in self.traces:
            self.traces[key] = History(maxlen=300_000)
        return self.traces[key]

    def _history_for(self, key: str) -> History:
        if key not in self.histories:
            self.histories[key] = History(maxlen=100_000)
        return self.histories[key]

    def _on_reading(self, readings) -> None:
        """Handle one capture. ``readings`` is {sensor: Reading}, one or two."""
        if not isinstance(readings, dict):          # single-sensor legacy shape
            readings = {self.settings.sensor_keys[0]: readings}
        self._last_readings = readings
        keys = [k for k in self.settings.sensor_keys if k in readings]
        if not keys:
            return
        primary = readings[keys[0]]
        self._last = primary
        self._readings += 1

        if not primary.ok:
            self._append_log(f"Empty capture: {primary.note}")
            self.tile_temp.set_value(float("nan"))
            return

        st = self.settings
        for key in keys:
            r = readings[key]
            if not r.ok:
                continue
            cal = self.cal_store.get(key)
            self._history_for(key).append(
                r.t_wall - self._t0,
                r.mean_temp_c if cal.calibrated else float("nan"),
            )
            self._append_trace(key, r)

        self._update_tiles(keys, readings)
        self._update_bin_label(keys, readings)

        # Recording is fed the readings themselves, not the plotted history, so
        # a "Clear history" during a run cannot punch a hole in the record.
        if self.recorder is not None and self.recorder.active:
            self.recorder.add(readings if st.is_dual else primary)
            self.plot_temp.set_recording(True)
            self._update_record_label()

        # Plots. The raw-timing and spectrum panes follow the primary sensor;
        # the temperature trace shows every selected sensor.
        self._redraw_trace()
        self._redraw_spectrum_throttled()
        self._refresh_calibration_labels()

    def _update_tiles(self, keys, readings) -> None:
        """Hero numbers. In dual mode the second tile becomes GR06's temperature."""
        primary = readings[keys[0]]
        cal = self.cal_store.get(keys[0])
        dual = len(keys) > 1

        if cal.calibrated:
            self.tile_temp.set_caption(
                f"{keys[0]} TEMPERATURE" if dual else "ESTIMATED TEMPERATURE", " °C"
            )
            self.tile_temp.set_value(
                primary.mean_temp_c,
                f"{primary.n:,} events in {primary.duration_s*1e3:.0f} ms"
                + (f", {primary.n_rejected} rejected" if primary.n_rejected else ""),
            )
            self.tile_temp.set_color(SERIES_1)
        else:
            self.tile_temp.set_value(float("nan"), "calibrate to convert rate to °C")
            self.tile_temp.set_color(STATUS_WARN)

        if dual:
            second = readings[keys[1]]
            cal2 = self.cal_store.get(keys[1])
            self.tile_rate.set_caption(f"{keys[1]} TEMPERATURE", " °C")
            self.tile_rate.set_color(SERIES_2)
            self.tile_rate.set_value(
                second.mean_temp_c if cal2.calibrated else float("nan"),
                f"{second.n:,} events" if cal2.calibrated else "not calibrated",
            )
            self.tile_noise.set_caption("DIFFERENCE", " °C")
            if cal.calibrated and cal2.calibrated:
                self.tile_noise.set_value(
                    second.mean_temp_c - primary.mean_temp_c,
                    f"{keys[1]} − {keys[0]}", decimals=3,
                )
            else:
                self.tile_noise.set_value(float("nan"), "calibrate both")
        else:
            spec = SENSORS[keys[0]]
            self.tile_rate.set_caption("SENSOR RATE", " kHz")
            self.tile_rate.set_color(TEXT_PRIMARY)
            self.tile_rate.set_value(
                primary.mean_rate_hz / 1e3,
                f"{spec.unit_label.lower()} {primary.mean_s*1e9:.1f} ns", decimals=3,
            )
            self.tile_noise.set_caption("NOISE (1 SIGMA)", " °C")
            if cal.calibrated:
                sem = primary.std_temp_c / max(1.0, np.sqrt(primary.n))
                self.tile_noise.set_value(
                    sem, f"per-event sigma {primary.std_temp_c:.3f} °C", decimals=4
                )
            else:
                self.tile_noise.set_caption("JITTER", " ns")
                self.tile_noise.set_value(
                    primary.std_s * 1e9, f"{spec.unit_label.lower()} spread",
                    decimals=3,
                )

    def _update_bin_label(self, keys, readings) -> None:
        """Say what a plotted point is worth, judged by the *thinnest* sensor.

        In dual mode GR07 gets ~900 events per millisecond while GR06 gets ~4,
        so warning on the primary alone would stay silent about a trace that is
        mostly noise.
        """
        binned = [readings[k] for k in keys if readings[k].bin_t.size]
        if not binned:
            self.lbl_bin.setText("one point per capture")
            self.lbl_bin.setStyleSheet(f"color:{TEXT_MUTED}; font-size:11px;")
            return
        first = binned[0]
        duty = first.duration_s / max(first.duration_s, self._cycle_s())
        parts = []
        worst_key, worst_ev = None, float("inf")
        for k in keys:
            r = readings[k]
            if not r.bin_t.size:
                continue
            ev = r.events_per_bin
            sig = r.bin_sigma_c
            bit = f"{k} {ev:.0f} ev"
            if np.isfinite(sig) and self.cal_store.get(k).calibrated:
                bit += f"/{sig*1000:.0f} mK"
            parts.append(bit)
            if ev < worst_ev:
                worst_key, worst_ev = k, ev
        msg = (f"{first.bin_t.size} pts/capture at {first.bin_s*1e3:.2f} ms, "
               f"{duty*100:.0f}% duty\n" + " · ".join(parts))
        if worst_ev < 10:
            msg += f" — {worst_key} too thin, widen the bin"
            self.lbl_bin.setStyleSheet(f"color:{STATUS_WARN}; font-size:11px;")
        else:
            self.lbl_bin.setStyleSheet(f"color:{TEXT_MUTED}; font-size:11px;")
        self.lbl_bin.setText(msg)

    def _cycle_s(self) -> float:
        """Wall-clock seconds between captures, measured from recent readings."""
        h = self.histories.get(self.settings.sensor_keys[0])
        if h is None or len(h) < 3:
            return self.settings.duration_s
        return max(self.settings.duration_s, h.mean_dt())

    def _append_trace(self, key: str, reading: Reading) -> None:
        """Add this capture's binned rate, preceded by a gap marker.

        The capture covers only part of the wall-clock cycle; the rest is arming
        and export. A NaN between captures makes the plot break there rather than
        drawing a line across time when nothing was measured.
        """
        trace = self._trace_for(key)
        if reading.bin_t.size == 0:
            trace.append(reading.t_wall - self._t0, reading.mean_rate_hz / 1e3)
        else:
            # t_wall is stamped at the end of the reduction, so the capture
            # started roughly duration_s earlier; bin_t is relative to that.
            start = reading.t_wall - self._t0 - reading.duration_s
            if len(trace):
                # Only break the line where measurement actually stopped. The
                # board counter runs through the round trip between windows, so
                # a marker at every window boundary would draw gaps that are
                # not there - and t_wall carries a few ms of host jitter, so
                # butt a continuous window against the last point rather than
                # trusting the arithmetic to line up.
                last = trace.t[-1]
                if start - last > 1.5 * reading.bin_s:
                    trace.append(start - reading.bin_s, float("nan"))
                else:
                    start = last + reading.bin_s
            trace.extend(start + reading.bin_t, reading.bin_rate_hz / 1e3)
        self._ref_rate.setdefault(key, reading.mean_rate_hz / 1e3)

    def _trace_values(self, key: str, mode: str, khz: np.ndarray):
        """Convert a stored rate trace to the selected quantity.

        Returns ``(values, unit)``. Temperature needs a calibration; without one
        the rate is shown instead, because an empty plot would hide the one
        thing that says the measurement is alive.
        """
        if mode == "temp":
            cal = self.cal_store.get(key)
            if cal.calibrated:
                return cal.temp_c(khz * 1e3), TRACE_UNITS["temp"]
            mode = "rate"
        if mode in ("ppm", "hz"):
            ref = self._ref_rate.get(key, float("nan"))
            if np.isfinite(ref) and ref != 0:
                err_khz = khz - ref
                if mode == "hz":
                    return err_khz * 1e3, TRACE_UNITS["hz"]
                return err_khz / ref * 1e6, TRACE_UNITS["ppm"]
            mode = "rate"
        return khz, TRACE_UNITS["rate"]

    def _redraw_trace(self) -> None:
        """Draw the top plot in whichever quantity is selected."""
        keys = [k for k in self.settings.sensor_keys if len(self._trace_for(k))]
        if not keys:
            return
        mode = self.combo_trace.currentData() or "temp"
        series, units = {}, {}
        for key in keys:
            t, khz = self._trace_for(key).arrays()
            values, unit = self._trace_values(key, mode, khz)
            series[key] = (t, values)
            units[key] = unit
        self.plot_temp.show_traces(series, units=units)
        self._draw_reference()
        if hasattr(self.plot_temp, "setTitle"):
            fell_back = [k for k in keys if units[k] != TRACE_UNITS[mode]]
            note = (f" — {' · '.join(fell_back)} uncalibrated, shown as rate"
                    if fell_back else "")
            label = dict((key, text) for text, key in TRACE_MODES).get(mode, mode)
            self.plot_temp.setTitle(
                f"{label} — " + " · ".join(keys) + note,
                color=TEXT_SECONDARY, size="10pt",
            )

    def _draw_reference(self) -> None:
        """Put the chamber's thermometer under the sensor traces, or take it away.

        Only in the temperature mode: the other modes are rates and errors, and
        a curve in degrees on a kHz axis would be nonsense. cicwave's plot has
        no reference series, so there it is simply skipped.
        """
        if not hasattr(self.plot_temp, "set_reference"):
            return
        mode = self.combo_trace.currentData() or "temp"
        if mode != "temp" or not len(self._chamber_trace):
            self.plot_temp.clear_reference()
            return
        t, temp = self._chamber_trace.arrays()
        self.plot_temp.set_reference(t, temp)

    #: Welch segments to aim for when the spectrum comes from the trace.
    #:
    #: Welch trades resolution for confidence: one periodogram of the whole
    #: record has ~100% scatter per bin and reads as hair, while K averaged
    #: segments cut it to ~1/sqrt(K). Eight (sixteen with the 50% overlap) puts
    #: the ripple near 25% and still lets the lowest bin follow the record - a
    #: fixed segment length would peg the low-frequency edge for ever, so a run
    #: of minutes would never show anything slower than the first ten seconds.
    TRACE_PSD_SEGMENTS = 8
    #: Bounds on that segment, in samples: enough bins to be a spectrum, not so
    #: many that one segment swallows the record.
    TRACE_PSD_MIN = 256
    TRACE_PSD_MAX = 8192

    def _continuous_psd(self, key: str):
        """Spectrum of the whole binned trace of ``key``.

        The board counter runs continuously, so the trace is a uniformly
        sampled record - a spectrum in its own right, reaching from half the
        bin rate down to a fraction of a hertz, which is where a thermometer
        actually lives. It is not a substitute for the per-event spectrum: that
        one is about the conversion, this one about the temperature.

        Breaks in the record are respected rather than interpolated across:
        each contiguous run is transformed on its own and the results are
        averaged, weighted by length, which is Welch's method applied to a
        record that happens to have holes in it.
        """
        t, khz = self._trace_for(key).arrays()
        if self._spec_from > 0:
            keep = t >= self._spec_from
            t, khz = t[keep], khz[keep]
        if t.size < 8:
            return None
        values, unit = self._trace_values(key, "temp", khz)
        finite = np.isfinite(values) & np.isfinite(t)
        if not finite.any():
            return None
        edges = np.flatnonzero(np.diff(finite.astype(np.int8)))
        starts = np.concatenate(([0], edges + 1))
        stops = np.concatenate((edges + 1, [values.size]))
        runs = [(a, b) for a, b in zip(starts, stops) if finite[a] and b - a >= 8]
        if not runs:
            return None
        dt = float(np.median(np.concatenate([np.diff(t[a:b]) for a, b in runs])))
        if not np.isfinite(dt) or dt <= 0:
            return None
        fs = 1.0 / dt

        longest = max(b - a for a, b in runs)
        nperseg = int(np.clip(2 ** int(np.log2(max(longest // self.TRACE_PSD_SEGMENTS, 1))),
                              self.TRACE_PSD_MIN, self.TRACE_PSD_MAX))
        nperseg = min(nperseg, longest)
        psds, weights = [], []
        for a, b in runs:
            if b - a < nperseg:
                continue
            f, p = welch_psd(values[a:b], fs, nperseg=nperseg)
            if f.size:
                psds.append(p)
                weights.append(b - a)
        if not psds:                      # nothing long enough: use the longest
            a, b = max(runs, key=lambda ab: ab[1] - ab[0])
            f, p = welch_psd(values[a:b], fs)
            if f.size == 0:
                return None
            psds, weights = [p], [b - a]
        psd = np.average(np.vstack(psds), axis=0, weights=weights)
        covered = sum(weights) * dt
        return f, psd, ("degC" if unit == "\u00b0C" else unit), fs, covered

    def _missing_events_note(self, readings, shown) -> str:
        """Name the selected sensors this view cannot show, and why.

        With the demo board GR07 has no per-event series, so a pane titled for
        two sensors would quietly be about one - and the curve that remains is
        GR06, in GR06's colour, which reads as the wrong sensor rather than as
        a missing one.
        """
        missing = [k for k in self.settings.sensor_keys
                   if k in readings and k not in shown]
        if not missing:
            return ""
        return (f"  |  no per-event data for {' · '.join(missing)}"
                f" (demo board reports one value per bin)")

    def _redraw_spectrum_throttled(self) -> None:
        """Redraw the lower pane, but not on every reading.

        A PSD plus a repaint is most of the GUI's per-reading cost, and the
        lower pane is a diagnostic rather than a live meter - at ten readings a
        second, recomputing it ten times a second buys nothing and costs the
        responsiveness of the trace.
        """
        now = time.time()
        if now - getattr(self, "_last_spec_t", 0.0) < SPECTRUM_MIN_INTERVAL_S:
            return
        self._last_spec_t = now
        self._redraw_spectrum()

    def _redraw_spectrum(self) -> None:
        mode = self.combo_spec.currentData()
        if mode == "raw":
            self.lower.setCurrentWidget(self.plot_obs)
            readings = getattr(self, "_last_readings", None) or {}
            keys = [k for k in self.settings.sensor_keys if k in readings]
            if keys and readings[keys[0]].event_s.size:
                r = readings[keys[0]]
                self.plot_obs.update_series(
                    r.event_t, r.event_s, SENSORS[keys[0]].unit_label
                )
            elif keys:
                self.plot_obs.clear_data()
                self.plot_obs.setTitle(
                    f"No per-event data for {keys[0]} - the demo board reports "
                    f"one value per bin",
                    color=TEXT_MUTED, size="10pt",
                )
            return
        self.lower.setCurrentWidget(self.plot_spec)
        if mode != "allan":
            self.plot_spec.restore_frequency_axis()

        readings = getattr(self, "_last_readings", None) or {}
        # These two views are built from the per-event series, which the demo
        # board's counter does not produce for GR07 - it reports one number per
        # bin. Select on what is actually there rather than on the event count,
        # so the pane can say so instead of drawing an empty axis.
        keys = [k for k in self.settings.sensor_keys if k in readings
                and readings[k].ok and readings[k].event_s.size >= 64]
        if not keys and mode in ("fast", "phase"):
            missing = [k for k in self.settings.sensor_keys if k in readings]
            self.plot_spec.clear_data()
            self.plot_spec.clear_second_psd()
            self.plot_spec.setTitle(
                ("No per-event data for " + " \u00b7 ".join(missing) +
                 " - the demo board reports one value per bin; "
                 "run with --source logic for this view")
                if missing else "Waiting for a capture",
                color=TEXT_MUTED, size="10pt",
            )
            return

        if mode == "phase":
            # Timing noise of each sensor, referred to its own carrier.
            if not keys:
                self.plot_spec.clear_data()
                self.plot_spec.clear_second_psd()
                return
            self.plot_spec.set_log_mode(True, False)
            self.plot_spec.setLabel("left", "L(f) [dBc/Hz]", color=TEXT_MUTED)
            bits = []
            for i, k in enumerate(keys):
                f, lf, f0 = phase_noise(readings[k].event_s)
                if f.size == 0:
                    continue
                self.plot_spec.set_curve_color(i, SENSOR_COLORS.get(k, SERIES_2))
                if i == 0:
                    self.plot_spec.set_data(f, lf)
                else:
                    self.plot_spec.set_second_psd(f, lf)
                bits.append(f"{k} {f0/1e3:.0f} kHz")
            if len(keys) < 2:
                self.plot_spec.clear_second_psd()
            note = " · GR06 is re-triggered: width jitter, not oscillator phase noise" \
                if "GR06" in keys else ""
            note += self._missing_events_note(readings, keys)
            self.plot_spec.setTitle(
                "Phase noise - " + " · ".join(bits) + note,
                color=TEXT_SECONDARY, size="10pt",
            )
            return

        self.plot_spec.set_log_mode(True, True)

        if mode == "trace":
            # The whole record, for every selected sensor: with the counters
            # running continuously the trace is one uniformly sampled series,
            # so this is the same measurement the tiles show, transformed.
            drawn, bits = 0, []
            for k in self.settings.sensor_keys:
                got = self._continuous_psd(k)
                if got is None:
                    continue
                f, psd, unit, fs, covered = got
                self.plot_spec.set_curve_color(drawn, SENSOR_COLORS.get(k, SERIES_2))
                if drawn == 0:
                    self.plot_spec.update_psd(f, psd, "", f"PSD [{unit}^2/Hz]")
                else:
                    self.plot_spec.set_second_psd(f, psd)
                drawn += 1
                bits.append(f"{k} {integrated_noise(f, psd):.3g} {unit} rms over "
                            f"{f[0]:.2g}-{f[-1]:.0f} Hz ({covered:.0f} s)")
            if drawn == 0:
                self.plot_spec.clear_data()
                self.plot_spec.clear_second_psd()
                self.plot_spec.setTitle("Not enough trace yet",
                                        color=TEXT_MUTED, size="10pt")
                return
            if drawn < 2:
                self.plot_spec.clear_second_psd()
            self.plot_spec.setTitle(
                "Whole record - " + " · ".join(bits),
                color=TEXT_SECONDARY, size="10pt",
            )
            return

        if mode == "fast":
            drawn, bits, unit = 0, [], "degC"
            for k in keys:
                r = readings[k]
                cal = self.cal_store.get(k)
                series = r.temp_c if cal.calibrated else r.event_s * 1e9
                unit = "degC" if cal.calibrated else "ns"
                f, psd = welch_psd(series, r.event_rate_hz)
                label = f"({r.n:,} ev at {r.event_rate_hz/1e3:.0f} kHz)"
                if f.size == 0:
                    continue
                self.plot_spec.set_curve_color(drawn, SENSOR_COLORS.get(k, SERIES_2))
                if drawn == 0:
                    self.plot_spec.update_psd(f, psd, "", f"PSD [{unit}^2/Hz]")
                else:
                    self.plot_spec.set_second_psd(f, psd)
                drawn += 1
                bits.append(f"{k} {integrated_noise(f, psd):.3g} {unit} rms {label}")
            if drawn == 0:
                self.plot_spec.clear_data()
                self.plot_spec.clear_second_psd()
                self.plot_spec.setTitle("Waiting for a capture",
                                        color=TEXT_MUTED, size="10pt")
                return
            if drawn < 2:
                self.plot_spec.clear_second_psd()
            self.plot_spec.setTitle(
                "Conversion noise within one capture - " + " · ".join(bits)
                + self._missing_events_note(readings, keys),
                color=TEXT_SECONDARY, size="10pt",
            )
        elif mode == "slow":
            self.plot_spec.clear_second_psd()
            t, v = self._history_for(self.settings.sensor_keys[0]).arrays()
            if self._spec_from > 0:
                keep = t >= self._spec_from
                t, v = t[keep], v[keep]
            good = np.isfinite(v)
            v = v[good]
            if v.size < 16:
                self.plot_spec.clear_data()
                self.plot_spec.setTitle(
                    f"Long-term spectrum - need 16 readings, have {v.size}",
                    color=TEXT_MUTED,
                    size="10pt",
                )
                return
            dt = self._history_for(self.settings.sensor_keys[0]).mean_dt()
            self.plot_spec.set_curve_color(
                0, SENSOR_COLORS.get(self.settings.sensor_keys[0], SERIES_2))
            f, psd = welch_psd(v, 1.0 / dt if dt > 0 else 1.0)
            rms = integrated_noise(f, psd)
            span = t[good][-1] - t[good][0] if v.size > 1 else 0.0
            self.plot_spec.update_psd(
                f, psd,
                f"Long-term noise - {rms:.4g} °C rms over {span/60:.1f} min "
                f"({v.size} readings, {1/dt if dt>0 else 0:.2f} Hz)",
                "PSD [degC^2/Hz]",
            )
        else:
            self.plot_spec.clear_second_psd()
            t, v = self._history_for(self.settings.sensor_keys[0]).arrays()
            if self._spec_from > 0:
                t, v = t[t >= self._spec_from], v[t >= self._spec_from]
            v = v[np.isfinite(v)]
            dt = self._history_for(self.settings.sensor_keys[0]).mean_dt()
            self.plot_spec.set_curve_color(
                0, SENSOR_COLORS.get(self.settings.sensor_keys[0], SERIES_2))
            if v.size < 16 or not np.isfinite(dt):
                self.plot_spec.clear_data()
                return
            taus, devs = allan_deviation(v, dt)
            self.plot_spec.update_allan(taus, devs)

    # -------------------------------------------------------------- chamber
    def _on_chamber_connect(self) -> None:
        if self.chamber_thread is not None:
            self._teardown_chamber()
            self.btn_chamber_connect.setText("Connect chamber")
            self.chip_chamber.set_state("unknown", "not connected")
            self.lbl_chamber.setText("not connected")
            self._set_chamber_controls_enabled(False)
            self._chamber_state = None
            self._chamber_trace.clear()
            self._draw_reference()
            return

        host = self.edit_chamber_host.text().strip() or DEFAULT_HOST
        port = self.spin_chamber_port.value()
        self.chamber_thread = ChamberThread(host, port, self.spin_chamber_addr.value(),
                                            poll_s=CHAMBER_POLL_S)
        self.chamber_thread.opened.connect(self._on_chamber_opened)
        self.chamber_thread.statusChanged.connect(self._on_chamber_status)
        self.chamber_thread.sweepChanged.connect(self._on_sweep_changed)
        self.chamber_thread.logMessage.connect(self._append_log)
        self.chamber_thread.errorMessage.connect(self._on_chamber_error)
        self.chamber_thread.start()
        self.btn_chamber_connect.setText("Disconnect chamber")
        self.chip_chamber.set_state("warn", f"connecting {host}...")

    def _teardown_chamber(self) -> None:
        if self.chamber_thread is None:
            return
        self.chamber_thread.shutdown()
        if not self.chamber_thread.wait(4000):
            self.chamber_thread.terminate()
            self.chamber_thread.wait(1000)
        self.chamber_thread = None
        self._sweeping = False
        self.btn_sweep.setText("Start sweep")

    def _on_chamber_opened(self, result) -> None:
        if isinstance(result, str):            # a "failed: ..." string
            self.chip_chamber.set_state("bad", result)
            self.lbl_chamber.setText(result)
            self._set_chamber_controls_enabled(False)
            return
        self.chip_chamber.set_state("ok", result.describe())
        self._set_chamber_controls_enabled(True)
        self._instruments["chamber"] = (
            f"Votsch VT @ {self.edit_chamber_host.text().strip()} (ASCII-2)"
        )
        self._append_log(f"Chamber connected: {result.describe()}")

    def _on_chamber_error(self, msg: str) -> None:
        """Surface a refused command where the controls are, not just in the log.

        A chamber that is not in remote mode reports its temperature perfectly
        and ignores every write, so the only sign anything is wrong is this
        message - it must not be easy to miss.
        """
        self._append_log(f"Chamber error: {msg}")
        self.lbl_sweep.setText(msg)
        self.lbl_sweep.setStyleSheet(f"color:{STATUS_BAD}; font-size:11px;")

    def _on_chamber_status(self, status) -> None:
        self._chamber_state = status
        self.chip_chamber.set_state(
            "ok" if status.running else "warn", status.describe()
        )
        self.lbl_chamber.setText(status.describe())
        # One point per poll, on the sensors' own clock, redrawn as it arrives:
        # the reference is slow enough that it costs nothing to keep it live.
        self._chamber_trace.append(time.time() - self._t0, status.actual_c)
        self._draw_reference()
        # Fold the chamber into the live recording so a sweep and the sensor
        # readings share one file.
        if self.recorder is not None and self.recorder.active:
            self.recorder.extra_values.update(
                {
                    "chamber_set_c": f"{status.setpoint_c:.2f}",
                    "chamber_actual_c": f"{status.actual_c:.2f}",
                    "chamber_on": int(bool(status.running)),
                }
            )

    def _on_chamber_set(self) -> None:
        if self.chamber_thread is not None:
            self.chamber_thread.set_temp(self.spin_chamber_set.value(), on=True)

    def _on_chamber_off(self) -> None:
        if self.chamber_thread is not None:
            self.chamber_thread.stop_chamber()

    def _on_calibrate_chamber(self) -> None:
        if self._chamber_state is None:
            QMessageBox.warning(
                self, "Calibrate", "Connect the chamber and wait for a reading first."
            )
            return
        actual = self._chamber_state.actual_c
        self.spin_ref.setValue(actual)     # reflect it in the reference field too
        self._do_calibrate(actual, source="chamber")
        for h in self.histories.values():
            h.clear()
        self._redraw_trace()
        self._refresh_calibration_labels()

    def _on_sweep_toggled(self) -> None:
        if self.chamber_thread is None:
            return
        if self._sweeping:
            self.chamber_thread.stop_sweep()
            return
        plan = SweepPlan(
            start_c=self.spin_sweep_start.value(),
            stop_c=self.spin_sweep_stop.value(),
            step_c=self.spin_sweep_step.value(),
            tol_c=self.spin_sweep_tol.value(),
            soak_s=float(self.spin_sweep_soak.value()),
            dwell_s=float(self.spin_sweep_dwell.value()),
        )
        self.chamber_thread.start_sweep(plan)
        self._sweeping = True
        self.btn_sweep.setText("Stop sweep")
        # A sweep is meaningless unless something is being measured, so make sure
        # the acquisition loop is running.
        if self.thread is not None and self.btn_run.text() == "Start":
            self._on_run_toggled()

    def _on_sweep_changed(self, payload: dict) -> None:
        phase = payload.get("phase", "")
        self.lbl_sweep.setText(payload.get("message", phase))
        if phase in ("done", "idle"):
            self._sweeping = False
            self.btn_sweep.setText("Start sweep")
            color = STATUS_GOOD if phase == "done" else TEXT_MUTED
            self.lbl_sweep.setStyleSheet(f"color:{color}; font-size:11px;")
        else:
            self._sweeping = True
            self.btn_sweep.setText("Stop sweep")
            self.lbl_sweep.setStyleSheet(f"color:{STATUS_WARN}; font-size:11px;")

    # ------------------------------------------------------------ recording
    def _on_record(self) -> None:
        if self.recorder is not None and self.recorder.active:
            return
        default = time.strftime(f"jnwtemp-{self.settings.sensor}-%Y%m%d-%H%M%S.csv")
        path, _ = QFileDialog.getSaveFileName(
            self, "Record temperature to", default, "CSV files (*.csv)"
        )
        if not path:
            return
        cal = self.cal
        if not cal.calibrated:
            QMessageBox.information(
                self,
                "Recording uncalibrated",
                "This sensor has no calibration, so the temperature column will be "
                "empty. The raw rate and timing columns are still recorded and can "
                "be converted later.",
            )
        self.recorder = TemperatureRecorder(path)
        # If the chamber is connected, add its setpoint/actual to every row so a
        # sweep is self-describing, and seed the columns with the latest reading.
        if self.chamber_thread is not None:
            self.recorder.extra_columns = ["chamber_set_c", "chamber_actual_c", "chamber_on"]
            if self._chamber_state is not None:
                self.recorder.extra_values = {
                    "chamber_set_c": f"{self._chamber_state.setpoint_c:.2f}",
                    "chamber_actual_c": f"{self._chamber_state.actual_c:.2f}",
                    "chamber_on": int(bool(self._chamber_state.running)),
                }
        try:
            self.recorder.start(self.settings, cal, self._instruments)
        except OSError as exc:
            self.recorder = None
            QMessageBox.warning(self, "Recording", f"Could not open {path}:\n{exc}")
            return

        self._record_marker_t = None
        self.btn_record.setEnabled(False)
        self.btn_record_stop.setEnabled(True)
        self._append_log(
            f"Recording to {path}\n"
            f"  provenance -> {os.path.basename(self.recorder.sidecar_path)}"
        )
        self._update_record_label()
        if self.thread is not None and self.btn_run.text() == "Start":
            # Recording with the loop paused would silently write nothing.
            self._on_run_toggled()

    def _on_record_stop(self) -> None:
        if self.recorder is None or not self.recorder.active:
            return
        path, rows, dur = self.recorder.stop()
        self.btn_record.setEnabled(True)
        self.btn_record_stop.setEnabled(False)
        mb = os.path.getsize(path) / 1e6 if os.path.exists(path) else 0.0
        self.lbl_record.setText(f"saved {rows:,} rows, {dur/60:.1f} min, {mb:.1f} MB\n{path}")
        self._append_log(
            f"Recording stopped: {rows:,} rows over {dur/60:.2f} min "
            f"({mb:.1f} MB) -> {path}"
        )
        self.plot_temp.set_recording(False)

    def _update_record_label(self) -> None:
        r = self.recorder
        if r is None or not r.active:
            self.lbl_record.setText("not recording")
            return
        mins = r.elapsed() / 60.0
        mb = r.size_bytes() / 1e6
        # Rows land at the trace-bin rate, so the file grows a lot faster than
        # one row per capture. Show the rate rather than let it surprise anyone.
        rate = f", {mb/mins:.1f} MB/min" if mins > 0.2 else ""
        self.lbl_record.setText(
            f"● recording {r.rows:,} rows, {mins:.1f} min, {mb:.1f} MB{rate}\n"
            f"{os.path.basename(r.path)}"
        )
        self.lbl_record.setStyleSheet(f"color:{STATUS_BAD}; font-size:11px;")

    def _on_export_capture(self) -> None:
        r = self._last
        if r is None or not r.ok:
            QMessageBox.warning(self, "Export capture", "No capture to export yet.")
            return
        if r.event_s.size == 0:
            QMessageBox.information(
                self,
                "Export capture",
                f"{r.sensor} has no per-event data to export: the demo board's "
                f"counter reports one value per bin, not one per period. The "
                f"binned trace is still recorded by 'Record to CSV', and "
                f"--source logic gives the per-event series.",
            )
            return
        default = time.strftime(f"jnwtemp-{self.settings.sensor}-capture-%Y%m%d-%H%M%S.csv")
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export last capture (per-event)",
            default,
            "Data files (*.csv *.parquet *.feather);;All files (*)",
        )
        if not path:
            return
        try:
            path, n = export_capture(path, r, self.settings, self.cal, self._instruments)
        except Exception as exc:
            QMessageBox.warning(self, "Export capture", f"Could not export:\n{exc}")
            return
        self._append_log(
            f"Exported {n} events to {path}\n"
            f"  plot in cicwave: cicwave {os.path.basename(path)}"
        )

    # -------------------------------------------------------------- actions
    def _on_calibrate(self) -> None:
        self._do_calibrate(self.spin_ref.value())

    def _do_calibrate(self, ref_c: float, source: str = "reference") -> None:
        readings = getattr(self, "_last_readings", None)
        if not readings:
            QMessageBox.warning(self, "Calibrate", "Take a reading first.")
            return
        # In dual mode both sensors sit on the same die and are therefore at the
        # same temperature, so one reference calibrates both from one capture.
        done = []
        for key, r in readings.items():
            if not r.ok:
                continue
            cal = self.cal_store.get(key)
            cal.add_point(ref_c, r.mean_rate_hz, note=f"n={r.n}, {source}")
            done.append(f"{key} -> {r.mean_rate_hz/1e3:.4f} kHz ({cal.describe()})")
        if not done:
            QMessageBox.warning(self, "Calibrate", "No usable reading yet.")
            return
        self.cal_store.save()
        if self.thread is not None:
            self.thread.update_calibration(self.cals)
        self._append_log(
            f"Calibrated at {ref_c:.2f} degC ({source}): " + "; ".join(done)
        )
        # The per-capture history was computed with the old model, so it is no
        # longer valid. The trace is raw rate and survives - it simply becomes a
        # temperature the moment the calibration lands.
        for h in self.histories.values():
            h.clear()
        self._redraw_trace()
        self._refresh_calibration_labels()

    def _on_clear_cal(self) -> None:
        for key in self.settings.sensor_keys:
            self.cal_store.get(key).clear()
        self.cal_store.save()
        if self.thread is not None:
            self.thread.update_calibration(self.cals)
        cal = self.cal
        # Same as calibrating: only the degrees-per-capture history dies, the
        # rate trace is still the rate trace.
        for h in self.histories.values():
            h.clear()
        self._redraw_trace()
        self._refresh_calibration_labels()
        self._append_log(f"Cleared calibration for {cal.sensor}")

    def _on_reset_spectrum(self) -> None:
        """Start the lower pane's averaging again, from now."""
        self._spec_from = time.time() - self._t0
        self._last_spec_t = 0.0            # redraw at once, do not wait out the throttle
        self._redraw_spectrum()
        self._append_log(f"Spectrum restarted at t = {self._spec_from:.1f} s")

    def _on_clear_history(self) -> None:
        for h in list(self.traces.values()) + list(self.histories.values()):
            h.clear()
        # The reference shares the sensors' clock, so it has to restart with it.
        self._chamber_trace.clear()
        self._ref_rate.clear()
        self._spec_from = 0.0
        self._t0 = time.time()
        self._readings = 0
        self.plot_temp.clear_data()
        self._draw_reference()
        self.plot_spec.clear_data()

    # ------------------------------------------------------------- keyboard
    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        """Forward cicwave's plot bindings, unless a field has focus."""
        from PySide6.QtWidgets import QAbstractSpinBox, QComboBox, QPlainTextEdit

        focus = self.focusWidget()
        if isinstance(focus, (QAbstractSpinBox, QComboBox, QPlainTextEdit)):
            super().keyPressEvent(event)
            return
        ctrl = bool(event.modifiers() & (Qt.KeyboardModifier.ControlModifier
                                         | Qt.KeyboardModifier.MetaModifier))
        if hasattr(self.plot_temp, "handle_key") and \
                self.plot_temp.handle_key(event.text(), ctrl):
            event.accept()
            return
        super().keyPressEvent(event)

    # ------------------------------------------------------------- shutdown
    def closeEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        if self.recorder is not None and self.recorder.active:
            self._on_record_stop()
        self._teardown_thread()
        self._teardown_chamber()
        self.cal_store.save()
        super().closeEvent(event)
