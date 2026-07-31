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
"""Qt thread that runs the acquisition loop and streams readings to the GUI.

Both the gRPC connection to Logic 2 and the serial port must live entirely in
the thread that created them, so everything instrument-facing happens inside
:meth:`AcquireThread.run` and the GUI talks to it only through a command queue
and Qt signals.
"""

from __future__ import annotations

import copy
import queue
import time
from dataclasses import replace
from typing import Optional

from PySide6.QtCore import QThread, Signal

from .acquire import Acquisition, AcquireSettings, Reading
from .temperature import Calibration


class AcquireThread(QThread):
    """Owns the instruments; emits one :class:`Reading` per capture."""

    #: connection results, {'logic': str, 'board': str}
    opened = Signal(dict)
    #: a completed measurement: {sensor_key: Reading}, one or two entries
    readingReady = Signal(object)
    #: channel scan results from a 'detect' command
    detected = Signal(dict)
    #: free-form log line for the GUI's message area
    logMessage = Signal(str)
    #: short status line ("Capturing...", "Idle")
    statusMessage = Signal(str)
    #: a fatal or recoverable error, as text
    errorMessage = Signal(str)
    #: emitted when the running/paused state changes
    runStateChanged = Signal(bool)

    def __init__(
        self,
        settings: AcquireSettings,
        calibration: Calibration,
        board_port: Optional[str] = None,
        use_board: bool = True,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._settings = copy.deepcopy(settings)
        # A calibration per sensor, so dual mode converts each with its own.
        self._cals = copy.deepcopy(calibration) if isinstance(calibration, dict) \
            else {getattr(calibration, "sensor", "GR07"): copy.deepcopy(calibration)}
        self._board_port = board_port
        self._use_board = use_board
        self._commands: "queue.Queue[tuple]" = queue.Queue()
        self._acq = None        # created inside run(), never touched from the GUI
        self._running = False   # actively capturing
        self._alive = True      # thread should keep going
        self._consecutive_errors = 0

    # ------------------------------------------------------- GUI-side API
    def submit(self, name: str, payload=None) -> None:
        """Queue a command for the acquisition thread."""
        self._commands.put((name, payload))

    def start_acquiring(self) -> None:
        self.submit("run", True)

    def stop_acquiring(self) -> None:
        self.submit("run", False)

    def update_settings(self, settings: AcquireSettings) -> None:
        self.submit("settings", copy.deepcopy(settings))

    def update_calibration(self, cal) -> None:
        """Accepts one Calibration or a {sensor: Calibration} dict."""
        self.submit("calibration", copy.deepcopy(cal))

    def request_detect(self, channels=None) -> None:
        self.submit("detect", channels)

    def request_configure(self) -> None:
        self.submit("configure", None)

    def shutdown(self) -> None:
        """Ask the thread to finish; safe to call from the GUI thread."""
        self._alive = False
        self.submit("quit", None)

    # ------------------------------------------------------------ internals
    def _drain_commands(self, block_for: float = 0.0) -> None:
        deadline = time.time() + block_for
        while True:
            try:
                timeout = max(0.0, deadline - time.time())
                name, payload = self._commands.get(timeout=timeout) if timeout else self._commands.get_nowait()
            except queue.Empty:
                return
            self._handle(name, payload)
            block_for = 0.0  # once something arrived, only drain what is queued

    def _handle(self, name: str, payload) -> None:
        if name == "quit":
            self._alive = False
            self._running = False
        elif name == "run":
            if bool(payload) != self._running:
                self._running = bool(payload)
                self.runStateChanged.emit(self._running)
                self.statusMessage.emit("Acquiring" if self._running else "Paused")
        elif name == "settings":
            was_stimulus = self._settings.needs_stimulus
            self._settings = payload
            if self._acq is not None:
                self._acq.settings = self._settings
                # Sensor changes can need a different chip state (clock, reset level).
                if self._settings.needs_stimulus != was_stimulus:
                    self._safe_configure()
        elif name == "calibration":
            if isinstance(payload, dict):
                self._cals.update(payload)
            else:
                self._cals[getattr(payload, "sensor", "GR07")] = payload
        elif name == "configure":
            self._safe_configure()
        elif name == "detect":
            self._safe_detect(payload)

    def _safe_configure(self) -> None:
        if self._acq is None:
            return
        try:
            msg = self._acq.configure_board()
            self.logMessage.emit(f"Board configured: {msg}")
        except Exception as exc:
            self.errorMessage.emit(f"Board configuration failed: {exc}")

    def _safe_detect(self, channels) -> None:
        if self._acq is None:
            return
        was_running = self._running
        self._running = False
        try:
            self.statusMessage.emit("Scanning channels...")
            report = self._acq.detect_channels(channels)
            self.detected.emit(report)
            live = [f"D{c}={v['freq']/1e3:.1f} kHz" for c, v in sorted(report.items()) if v["edges"] > 2]
            self.logMessage.emit("Active channels: " + (", ".join(live) if live else "none"))
        except Exception as exc:
            self.errorMessage.emit(f"Channel scan failed: {exc}")
        finally:
            self._running = was_running
            self.statusMessage.emit("Acquiring" if was_running else "Paused")

    # ----------------------------------------------------------------- run
    def run(self) -> None:  # noqa: D102 - QThread entry point
        self._acq = None
        try:
            self._acq = Acquisition(
                self._settings,
                board_port=self._board_port,
                use_board=self._use_board,
                log=self.logMessage.emit,
            )
            try:
                status = self._acq.open()
            except Exception as exc:
                # open() reports per-instrument status before raising, so the
                # board may well be connected even though Logic 2 is not.
                status = getattr(self._acq, "last_status", None) or {
                    "logic": f"failed: {exc}", "board": "not attempted"}
                self.errorMessage.emit(str(exc))
            self.opened.emit(status)
            self._safe_configure()
        except Exception as exc:
            self.errorMessage.emit(str(exc))
            self.opened.emit({"logic": f"failed: {exc}", "board": "not attempted"})
            # Stay alive so the GUI can fix Logic 2 and hit Connect again.

        try:
            while self._alive:
                if not self._running or self._acq is None:
                    self._drain_commands(block_for=0.1)
                    continue
                self._drain_commands()
                if not self._running:
                    continue
                try:
                    self.statusMessage.emit("Capturing...")
                    reading = self._acq.read(self._cals)
                    self._consecutive_errors = 0
                    self.readingReady.emit(reading)
                    self.statusMessage.emit("Acquiring")
                except Exception as exc:
                    self._consecutive_errors += 1
                    self.errorMessage.emit(str(exc))
                    if self._consecutive_errors >= 3:
                        self._running = False
                        self.runStateChanged.emit(False)
                        self.statusMessage.emit("Paused after repeated errors")
                        self._consecutive_errors = 0
                    else:
                        time.sleep(0.5)
        finally:
            if self._acq is not None:
                self._acq.close()
            self.statusMessage.emit("Disconnected")
