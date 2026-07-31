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
    DEFAULT_CHANNELS as SENSOR_CHANNELS,
    SENSORS,
    AcquireSettings,
    Reading,
)
from .board import MAX_PROJECT_CLOCK_HZ, find_ports
from .logic import OFFERED_RATES, THRESHOLDS_V
from .plots import (
    GRID,
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
from .spectrum import History, allan_deviation, integrated_noise, welch_psd
from .temperature import CalibrationStore, resolution_k
from .worker import AcquireThread


#: What the lower pane shows. The raw per-event trace lives here rather than in
#: a permanent third pane: it is worth looking at occasionally, not constantly,
#: and two panes leave the temperature trace room to be read.
SPECTRUM_MODES = [
    ("Within capture (conversion noise)", "fast"),
    ("Long term (drift + 1/f)", "slow"),
    ("Allan deviation", "allan"),
    ("Last capture - raw timing", "raw"),
]


class StatusChip(QLabel):
    """A small colored pill showing the state of one instrument."""

    def __init__(self, name: str) -> None:
        super().__init__()
        self._name = name
        self.set_state("unknown", "not connected")

    def set_state(self, state: str, detail: str = "") -> None:
        color = {
            "ok": STATUS_GOOD,
            "warn": STATUS_WARN,
            "bad": STATUS_BAD,
        }.get(state, TEXT_MUTED)
        self.setText(f"{self._name}: {detail}" if detail else self._name)
        self.setToolTip(detail)
        self.setStyleSheet(
            f"QLabel {{ color:{color}; border:1px solid {color}; border-radius:6px;"
            f" padding:3px 10px; background:{SURFACE_2}; font-size:11px; }}"
        )


class MainWindow(QWidget):
    def __init__(self, board_port: Optional[str] = None, use_board: bool = True) -> None:
        super().__init__()
        self.setWindowTitle("JNW-TEMP - live temperature sensor demo (TT project 258)")

        self.settings = AcquireSettings()
        self.cal_store = CalibrationStore()
        self._board_port = board_port
        self._use_board = use_board
        self.thread: Optional[AcquireThread] = None

        # Two histories, deliberately.
        #  trace:   one point per time bin (~1 ms) - what the plot shows. It is
        #           NOT uniformly sampled: captures are bursts separated by dead
        #           time, so it must not be fed to the PSD or Allan routines.
        #  history: one averaged point per capture - uniform enough in time to
        #           be the basis for the long-term spectrum and Allan deviation.
        self.traces: dict = {}     # sensor -> fine binned trace (plot only)
        self.histories: dict = {}  # sensor -> per-capture means (spectra, Allan)
        self._t0 = time.time()
        self._last: Optional[Reading] = None
        self._readings = 0
        self.recorder: Optional[TemperatureRecorder] = None
        #: identities reported by the instruments at connect, for provenance
        self._instruments: dict = {}

        self._build_ui()
        self._apply_dark_theme()
        self.resize(1500, 940)
        self._refresh_calibration_labels()

    # ------------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        self.chip_logic = StatusChip("Logic 2")
        self.chip_board = StatusChip("Demo board")
        self.chip_project = StatusChip("Project")
        self.status_label = QLabel("Idle")
        self.status_label.setStyleSheet(f"color:{TEXT_MUTED}; font-size:11px;")

        self.btn_connect = QPushButton("Connect")
        self.btn_connect.clicked.connect(self._on_connect)
        self.btn_connect.setFixedWidth(110)

        top = QHBoxLayout()
        top.setContentsMargins(8, 6, 8, 0)
        top.setSpacing(8)
        for chip in (self.chip_logic, self.chip_board, self.chip_project):
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
        panel = QWidget()
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(0, 6, 6, 0)
        outer.setSpacing(8)

        # Connect lives in the status bar, beside the chips it affects.
        meas = QGroupBox("Measure")
        sf = QFormLayout(meas)
        self.combo_sensor = QComboBox()
        for key, spec in SENSORS.items():
            self.combo_sensor.addItem(spec.label, key)
        # Both sensors come out of one two-channel capture, so they share a time
        # base - the only way the comparison means anything.
        self.combo_sensor.addItem("Both - GR07 + GR06 together", BOTH)
        self.combo_sensor.currentIndexChanged.connect(self._on_sensor_changed)
        sf.addRow("Sensor", self.combo_sensor)

        self.spin_duration = QDoubleSpinBox()
        self.spin_duration.setRange(0.005, 5.0)
        self.spin_duration.setDecimals(3)
        self.spin_duration.setSingleStep(0.05)
        self.spin_duration.setSuffix(" s")
        self.spin_duration.setValue(self.settings.duration_s)
        self.spin_duration.valueChanged.connect(self._push_settings)
        sf.addRow("Capture", self.spin_duration)

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
        sf.addRow("Trace bin", self.spin_bin)

        self.lbl_bin = QLabel("")
        self.lbl_bin.setWordWrap(True)
        self.lbl_bin.setMinimumHeight(30)
        self.lbl_bin.setStyleSheet(f"color:{TEXT_MUTED}; font-size:11px;")
        sf.addRow(self.lbl_bin)

        self.btn_run = QPushButton("Start")
        self.btn_run.setEnabled(False)
        self.btn_run.clicked.connect(self._on_run_toggled)
        sf.addRow(self.btn_run)
        outer.addWidget(meas)

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
        outer.addWidget(calib)

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
        outer.addWidget(rec)

        disp = QGroupBox("Display")
        df = QFormLayout(disp)
        self.combo_spec = QComboBox()
        for label, key in SPECTRUM_MODES:
            self.combo_spec.addItem(label, key)
        self.combo_spec.currentIndexChanged.connect(self._redraw_spectrum)
        df.addRow("Bottom plot", self.combo_spec)
        self.btn_reset = QPushButton("Clear history")
        self.btn_reset.clicked.connect(self._on_clear_history)
        df.addRow(self.btn_reset)
        outer.addWidget(disp)

        # ---- setup: wiring and one-time configuration --------------------
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

        outer.addWidget(self._collapsible("SETUP / WIRING", setup))

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        self.log.setMinimumHeight(90)
        self.log.setStyleSheet(
            f"QPlainTextEdit {{ background:{SURFACE_2}; color:{TEXT_MUTED};"
            f" border:1px solid {GRID}; border-radius:6px; font-size:11px; }}"
        )
        outer.addWidget(self.log, 1)

        # The panel must never dictate the window height: on a laptop screen the
        # full stack is taller than the display, so it scrolls instead.
        scroll = QScrollArea()
        scroll.setWidget(panel)
        scroll.setWidgetResizable(True)
        scroll.setFixedWidth(356)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        return scroll

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
        st.channels[st.sensor_keys[0]] = self.spin_channel.value()
        st.sample_rate = int(self.combo_rate.currentData())
        st.threshold_volts = float(self.combo_threshold.currentData())
        st.duration_s = float(self.spin_duration.value())
        st.clock_hz = self.spin_clock.value() * 1_000_000
        st.reset_high_us = self.spin_reset_high.value()
        st.reset_low_us = self.spin_reset_low.value()
        st.bin_ms = float(self.spin_bin.value())
        return st

    def _push_settings(self) -> None:
        st = self._collect_settings()
        if self.thread is not None:
            self.thread.update_settings(st)

    def _on_bin_changed(self) -> None:
        # Points already in the trace were binned at the old width; mixing
        # resolutions in one series would misrepresent the noise.
        self.trace.clear()
        self.plot_temp.clear_data()
        self._push_settings()

    def _on_sensor_changed(self) -> None:
        key = self.combo_sensor.currentData()
        keys = list(SENSORS) if key == BOTH else [key]
        self.spin_channel.blockSignals(True)
        self.spin_channel.setValue(self.settings.channels.get(keys[0], 0))
        self.spin_channel.blockSignals(False)
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
            use_board=self.chk_board.isChecked(),
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
        self.plot_temp.show_traces(
            {f"{k}_temp_c": self._trace_for(k).arrays() for k in keys}
        )
        if hasattr(self.plot_temp, "setTitle"):
            self.plot_temp.setTitle(
                "Estimated temperature — " + " · ".join(keys),
                color=TEXT_SECONDARY, size="10pt",
            )
        self.plot_obs.update_series(
            primary.event_t, primary.event_s, SENSORS[keys[0]].unit_label
        )
        self._redraw_spectrum()
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
        """Add this capture's binned points, preceded by a gap marker.

        The capture covers only part of the wall-clock cycle; the rest is arming
        and export. A NaN between captures makes the plot break there rather than
        drawing a line across time when nothing was measured.
        """
        trace = self._trace_for(key)
        cal = self.cal_store.get(key)
        if reading.bin_t.size == 0:
            trace.append(
                reading.t_wall - self._t0,
                reading.mean_temp_c if cal.calibrated else float("nan"),
            )
            return
        # t_wall is stamped at the end of the reduction, so the capture started
        # roughly duration_s earlier; bin_t is relative to that start.
        start = reading.t_wall - self._t0 - reading.duration_s
        if len(trace):
            trace.append(start - reading.bin_s, float("nan"))
        temps = reading.bin_temp_c if cal.calibrated else np.full(
            reading.bin_t.size, np.nan
        )
        trace.extend(start + reading.bin_t, temps)

    def _redraw_spectrum(self) -> None:
        mode = self.combo_spec.currentData()
        if mode == "raw":
            self.lower.setCurrentWidget(self.plot_obs)
            return
        self.lower.setCurrentWidget(self.plot_spec)
        if mode != "allan":
            self.plot_spec.restore_frequency_axis()

        if mode == "fast":
            r = self._last
            if r is None or r.n < 32:
                self.plot_spec.clear_data()
                return
            series = r.temp_c if self.cal.calibrated else r.event_s * 1e9
            unit = "degC" if self.cal.calibrated else "ns"
            fs = r.event_rate_hz
            f, psd = welch_psd(series, fs)
            rms = integrated_noise(f, psd)
            self.plot_spec.update_psd(
                f,
                psd,
                f"Conversion noise within one capture - {rms:.4g} {unit} rms, "
                f"{r.n} events at {fs/1e3:.1f} kHz",
                f"PSD [{unit}^2/Hz]",
            )
        elif mode == "slow":
            t, v = self._history_for(self.settings.sensor_keys[0]).arrays()
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
            t, v = self._history_for(self.settings.sensor_keys[0]).arrays()
            v = v[np.isfinite(v)]
            dt = self._history_for(self.settings.sensor_keys[0]).mean_dt()
            if v.size < 16 or not np.isfinite(dt):
                self.plot_spec.clear_data()
                return
            taus, devs = allan_deviation(v, dt)
            self.plot_spec.update_allan(taus, devs)

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
            cal.add_point(self.spin_ref.value(), r.mean_rate_hz, note=f"n={r.n}")
            done.append(f"{key} -> {r.mean_rate_hz/1e3:.4f} kHz ({cal.describe()})")
        if not done:
            QMessageBox.warning(self, "Calibrate", "No usable reading yet.")
            return
        self.cal_store.save()
        if self.thread is not None:
            self.thread.update_calibration(self.cals)
        self._append_log(
            f"Calibrated at {self.spin_ref.value():.2f} degC: " + "; ".join(done)
        )
        # Past history was computed with the old model, so it is no longer valid.
        for h in list(self.traces.values()) + list(self.histories.values()):
            h.clear()
        self._refresh_calibration_labels()

    def _on_clear_cal(self) -> None:
        for key in self.settings.sensor_keys:
            self.cal_store.get(key).clear()
        self.cal_store.save()
        if self.thread is not None:
            self.thread.update_calibration(self.cals)
        cal = self.cal
        for h in list(self.traces.values()) + list(self.histories.values()):
            h.clear()
        self._refresh_calibration_labels()
        self._append_log(f"Cleared calibration for {cal.sensor}")

    def _on_clear_history(self) -> None:
        for h in list(self.traces.values()) + list(self.histories.values()):
            h.clear()
        self._t0 = time.time()
        self._readings = 0
        self.plot_temp.clear_data()
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
        self.cal_store.save()
        super().closeEvent(event)
