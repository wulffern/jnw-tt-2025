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
"""The demo-board window: one USB cable, no other instrument.

The same plots, tiles, calibration and recording as the Saleae window - only
the controls differ, and they differ because the instrument does. There is no
capture length, sample rate or threshold here: the RP2350's PIO counts the
chip's output continuously and reports one number per trace bin. What is left
to choose is the sensor and how finely to bin it.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import QComboBox, QFormLayout, QGroupBox, QPushButton, QSpinBox, QWidget

from .acquire import SENSORS, SOURCE_BOARD, AcquireSettings
from .board import MAX_PROJECT_CLOCK_HZ, find_ports
from .main_window import MainWindow
from .plots import STATUS_WARN, TEXT_MUTED


class BoardWindow(MainWindow):
    """MainWindow with the demo board as the instrument instead of Logic 2."""

    def __init__(self, board_port: Optional[str] = None, use_board: bool = True) -> None:
        super().__init__(board_port=board_port, use_board=True)
        self.setWindowTitle(
            "JNW-TEMP - demo board only (TT project 258, RP2350 PIO counter)"
        )
        self.settings.source = SOURCE_BOARD
        self._append_log(
            "Timing from the demo board: two PIO state machines count uo_out[0] "
            "and time uo_out[2]. No Saleae needed.\n"
            "Per-event views (conversion noise, phase noise, raw timing) need "
            "--source logic for GR07; GR06 has them either way."
        )

    def _make_settings(self) -> AcquireSettings:
        """Ten-millisecond bins, tenth-of-a-second updates.

        The counter is free to bin as finely as 1 ms, but the GUI is what has
        to keep up: at 1 ms the trace grows by a thousand points a second per
        sensor and the paint loop, not the instrument, sets the frame rate. At
        10 ms the trace still resolves everything the sensor does - its own
        noise floor is 90 mK at 1 ms and the Allan deviation flattens by 20 ms -
        and the window updates ten times a second, which reads as continuous.
        """
        return AcquireSettings(source=SOURCE_BOARD, bin_ms=10.0, window_s=0.1)

    # ------------------------------------------------------------------- UI
    def _group_measure(self) -> QWidget:
        """Sensor and trace bin. No capture length: counting never stops."""
        meas = QGroupBox("Measure")
        form = QFormLayout(meas)
        self._sensor_row(form)
        self._trace_bin_row(form)
        self._run_row(form)
        return meas

    def _group_setup(self) -> QWidget:
        """Only what the board itself needs - no Saleae channel, rate or threshold."""
        setup = QWidget()
        form = QFormLayout(setup)
        form.setContentsMargins(0, 4, 0, 0)

        self.combo_port = QComboBox()
        self.combo_port.addItem("auto", None)
        for port in find_ports():
            self.combo_port.addItem(port, port)
        form.addRow("Serial port", self.combo_port)

        self.spin_clock = QSpinBox()
        self.spin_clock.setRange(1, MAX_PROJECT_CLOCK_HZ // 1_000_000)
        self.spin_clock.setValue(self.settings.clock_hz // 1_000_000)
        self.spin_clock.setSuffix(" MHz")
        self.spin_clock.setToolTip(
            "GR07's comparator trip is re-timed by this clock, so run it fast."
        )
        self.spin_clock.valueChanged.connect(self._push_settings)
        form.addRow("Project clock", self.spin_clock)

        self.spin_reset_high = QSpinBox()
        self.spin_reset_high.setRange(1, 10_000)
        self.spin_reset_high.setValue(self.settings.reset_high_us)
        self.spin_reset_high.setSuffix(" us")
        self.spin_reset_high.valueChanged.connect(self._push_settings)
        form.addRow("Reset high", self.spin_reset_high)

        self.spin_reset_low = QSpinBox()
        self.spin_reset_low.setRange(1, 100_000)
        self.spin_reset_low.setValue(self.settings.reset_low_us)
        self.spin_reset_low.setSuffix(" us")
        self.spin_reset_low.valueChanged.connect(self._push_settings)
        form.addRow("Reset low", self.spin_reset_low)

        self.btn_apply_chip = QPushButton("Apply to chip")
        self.btn_apply_chip.clicked.connect(self._on_apply_chip)
        self.btn_apply_chip.setEnabled(False)
        form.addRow(self.btn_apply_chip)

        self.btn_export_capture = QPushButton("Export last capture...")
        self.btn_export_capture.setToolTip(
            "Per-event data of the most recent capture. GR06 has it (one width "
            "per reset pulse); GR07 does not - the counter reports one value "
            "per bin. Use 'Record to CSV' for the binned trace."
        )
        self.btn_export_capture.clicked.connect(self._on_export_capture)
        form.addRow(self.btn_export_capture)
        return setup

    # ------------------------------------------------------------- settings
    def _collect_instrument_settings(self, st: AcquireSettings) -> None:
        st.source = SOURCE_BOARD

    def _sync_channel_widget(self, keys) -> None:
        """No Saleae channel to point anywhere."""

    def _use_board_now(self) -> bool:
        return True

    # ------------------------------------------------------------ instrument
    def _on_opened(self, status: dict) -> None:
        """Chips for a one-instrument session: the board is what must be up."""
        board = status.get("board", "")
        self.chip_logic.set_state("off", "not used")
        if board in ("disabled",) or board.startswith("unavailable"):
            self.chip_board.set_state("bad", board)
            self.chip_project.set_state("warn", "not verified")
            self.btn_run.setEnabled(False)
            self.status_label.setText("Demo board unavailable - see log")
        else:
            self.chip_board.set_state("ok", board.split("|")[0].strip())
            self.chip_project.set_state("ok", "tt_um_jnw_wulffern (258)")
            self.btn_apply_chip.setEnabled(True)
            self.btn_run.setEnabled(True)
            self.status_label.setText("Connected - press Start")
        self._instruments = {"logic2": "not used", "demo_board": board}
        self._append_log(f"Board: {board}")

    def _update_bin_label(self, keys, readings) -> None:
        """Same as the base, but a bin here is a count of periods, not events."""
        super()._update_bin_label(keys, readings)
        if "GR07" in keys and readings["GR07"].bin_t.size:
            r = readings["GR07"]
            self.lbl_bin.setText(
                f"{r.bin_t.size} bins/update at {r.bin_s*1e3:.2f} ms\n"
                f"{r.bin_n[0]:.0f} periods per bin, reciprocal counter"
            )
            self.lbl_bin.setStyleSheet(
                f"color:{STATUS_WARN if r.bin_n[0] < 16 else TEXT_MUTED}; font-size:11px;"
            )
