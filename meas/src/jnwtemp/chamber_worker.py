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
"""Qt thread that owns the chamber socket, polls it, and runs sweeps.

The TCP socket must live entirely in the thread that created it, so - exactly
like :class:`~jnwtemp.worker.AcquireThread` for the instruments - everything
chamber-facing happens inside :meth:`ChamberThread.run` and the GUI talks to it
only through a command queue and Qt signals.

A *sweep* is a state machine, not a blocking loop: the thread keeps polling and
emitting status while it walks a list of setpoints, so the GUI stays live and
the acquisition keeps recording through the whole run. Each point is held until
the actual temperature has stayed within a tolerance of the setpoint for a soak
time (it has *stabilised*), then kept there for a dwell time (the measurement
plateau), then the next setpoint is commanded.
"""

from __future__ import annotations

import queue
import time
from dataclasses import dataclass, field
from typing import List, Optional

from PySide6.QtCore import QThread, Signal

from .chamber import DEFAULT_ADDRESS, ChamberError, ChamberStatus, VotschChamber


@dataclass
class SweepPlan:
    """A temperature sweep specified as a range with a step."""

    start_c: float
    stop_c: float
    step_c: float
    tol_c: float = 0.3
    soak_s: float = 120.0
    dwell_s: float = 300.0

    def setpoints(self) -> List[float]:
        """The ordered setpoints, inclusive of both ends."""
        step = abs(self.step_c) or 1.0
        if self.stop_c < self.start_c:
            step = -step
        pts = []
        n = int(round((self.stop_c - self.start_c) / step)) + 1
        for i in range(max(1, n)):
            pts.append(round(self.start_c + i * step, 3))
        # Guarantee the endpoint is present even when the span is not an exact
        # multiple of the step.
        if abs(pts[-1] - self.stop_c) > 1e-6:
            pts.append(round(self.stop_c, 3))
        return pts


@dataclass
class _SweepState:
    plan: SweepPlan
    points: List[float]
    index: int = 0
    phase: str = "settling"          # settling -> stabilising -> dwelling
    stable_since: Optional[float] = None
    dwell_until: Optional[float] = None
    commanded: bool = False
    started_at: float = field(default_factory=time.time)


class ChamberThread(QThread):
    """Owns the chamber; polls it and drives sweeps off a command queue."""

    #: connection result: the first status, or an error string
    opened = Signal(object)
    #: a fresh :class:`ChamberStatus`, roughly every poll interval
    statusChanged = Signal(object)
    #: sweep progress: a dict the GUI turns into a label
    sweepChanged = Signal(dict)
    #: free-form log line for the GUI's message area
    logMessage = Signal(str)
    #: recoverable error, as text
    errorMessage = Signal(str)

    def __init__(
        self,
        host: str,
        port: int,
        address: int = DEFAULT_ADDRESS,
        poll_s: float = 2.0,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._host = host
        self._port = port
        self._address = address
        self._poll_s = poll_s
        self._commands: "queue.Queue[tuple]" = queue.Queue()
        self._chamber: Optional[VotschChamber] = None
        self._alive = True
        self._sweep: Optional[_SweepState] = None
        self._last: Optional[ChamberStatus] = None

    # ------------------------------------------------------- GUI-side API
    def submit(self, name: str, payload=None) -> None:
        self._commands.put((name, payload))

    def set_temp(self, temp_c: float, on: bool = True) -> None:
        self.submit("set", (float(temp_c), bool(on)))

    def stop_chamber(self) -> None:
        self.submit("stop_chamber", None)

    def start_sweep(self, plan: SweepPlan) -> None:
        self.submit("sweep", plan)

    def stop_sweep(self) -> None:
        self.submit("stop_sweep", None)

    def shutdown(self) -> None:
        self._alive = False
        self.submit("quit", None)

    # ------------------------------------------------------------ internals
    def _drain_commands(self, block_for: float = 0.0) -> None:
        deadline = time.time() + block_for
        while True:
            try:
                timeout = max(0.0, deadline - time.time())
                name, payload = (
                    self._commands.get(timeout=timeout)
                    if timeout
                    else self._commands.get_nowait()
                )
            except queue.Empty:
                return
            self._handle(name, payload)
            block_for = 0.0

    def _handle(self, name: str, payload) -> None:
        if name == "quit":
            self._alive = False
        elif name == "set":
            self._sweep = None            # a manual set overrides any sweep
            temp_c, on = payload
            self._apply_set(temp_c, on, note=f"setpoint {temp_c:.1f} °C")
        elif name == "stop_chamber":
            self._sweep = None
            self._apply_set(None, False, note="chamber off")
        elif name == "sweep":
            self._begin_sweep(payload)
        elif name == "stop_sweep":
            if self._sweep is not None:
                self.logMessage.emit("Sweep cancelled")
            self._sweep = None
            self._emit_sweep({"phase": "idle", "message": "sweep stopped"})

    def _apply_set(self, temp_c, on: bool, note: str) -> bool:
        """Command the chamber. Returns False if it refused, having said so."""
        if self._chamber is None:
            return False
        try:
            status = (
                self._chamber.stop()
                if temp_c is None and not on
                else self._chamber.set_temp(temp_c, on=on)
            )
            self._publish(status)
            self.logMessage.emit(f"Chamber: {note}")
            return True
        except ChamberError as exc:
            self.errorMessage.emit(str(exc))
            return False

    # ---------------------------------------------------------------- sweep
    def _begin_sweep(self, plan: SweepPlan) -> None:
        if self._chamber is None:
            self.errorMessage.emit("Connect the chamber before starting a sweep")
            return
        points = plan.setpoints()
        self._sweep = _SweepState(plan=plan, points=points)
        self.logMessage.emit(
            "Sweep started: "
            + " → ".join(f"{p:g}" for p in points)
            + f" °C, soak {plan.soak_s:g}s within ±{plan.tol_c:g} °C, "
            f"dwell {plan.dwell_s:g}s"
        )
        self._emit_sweep({"phase": "starting", "message": "sweep starting"})

    def _service_sweep(self, status: ChamberStatus) -> None:
        sw = self._sweep
        if sw is None:
            return
        target = sw.points[sw.index]
        now = time.time()

        if not sw.commanded:
            if not self._apply_set(target, True, note=f"sweep → {target:g} °C"):
                # The chamber is not taking setpoints, so every later point
                # would fail the same way. Stop rather than walk a sweep that
                # is not happening.
                self._sweep = None
                self.logMessage.emit("Sweep aborted: chamber refused the setpoint")
                self._emit_sweep(
                    {"phase": "idle", "message": "sweep aborted - chamber refused"}
                )
                return
            sw.commanded = True
            sw.phase = "stabilising"
            sw.stable_since = None

        in_band = abs(status.actual_c - target) <= sw.plan.tol_c

        if sw.phase == "stabilising":
            if in_band:
                if sw.stable_since is None:
                    sw.stable_since = now
                if now - sw.stable_since >= sw.plan.soak_s:
                    sw.phase = "dwelling"
                    sw.dwell_until = now + sw.plan.dwell_s
                    self.logMessage.emit(
                        f"Stabilised at {target:g} °C "
                        f"(actual {status.actual_c:.2f}); dwelling "
                        f"{sw.plan.dwell_s:g}s"
                    )
            else:
                sw.stable_since = None      # fell out of band, soak restarts
        elif sw.phase == "dwelling":
            if sw.dwell_until is not None and now >= sw.dwell_until:
                self._advance_sweep()
                return

        self._emit_sweep(self._sweep_progress(status))

    def _advance_sweep(self) -> None:
        sw = self._sweep
        if sw is None:
            return
        if sw.index + 1 >= len(sw.points):
            self.logMessage.emit("Sweep complete")
            self._apply_set(None, False, note="sweep finished, chamber off")
            self._sweep = None
            self._emit_sweep({"phase": "done", "message": "sweep complete"})
            return
        sw.index += 1
        sw.commanded = False
        sw.stable_since = None
        sw.dwell_until = None

    def _sweep_progress(self, status: ChamberStatus) -> dict:
        sw = self._sweep
        assert sw is not None
        target = sw.points[sw.index]
        remaining = 0.0
        if sw.phase == "dwelling" and sw.dwell_until is not None:
            remaining = max(0.0, sw.dwell_until - time.time())
        return {
            "phase": sw.phase,
            "index": sw.index,
            "total": len(sw.points),
            "target_c": target,
            "actual_c": status.actual_c,
            "dwell_remaining_s": remaining,
            "message": (
                f"point {sw.index + 1}/{len(sw.points)} → {target:g} °C: "
                f"{sw.phase}"
                + (f", {remaining:.0f}s left" if remaining else "")
            ),
        }

    def _emit_sweep(self, payload: dict) -> None:
        self.sweepChanged.emit(payload)

    # ------------------------------------------------------------- publish
    def _publish(self, status: ChamberStatus) -> None:
        self._last = status
        self.statusChanged.emit(status)

    # ----------------------------------------------------------------- run
    def run(self) -> None:  # noqa: D102 - QThread entry point
        try:
            self._chamber = VotschChamber(self._host, self._port, self._address)
            status = self._chamber.connect()
            self.opened.emit(status)
            self._publish(status)
        except ChamberError as exc:
            self._chamber = None
            self.opened.emit(f"failed: {exc}")
            self.errorMessage.emit(str(exc))
            return

        next_poll = 0.0
        try:
            while self._alive:
                # A sweep needs a snappier loop than a passive readout, so poll
                # faster while one is running.
                interval = 1.0 if self._sweep is not None else self._poll_s
                self._drain_commands(block_for=min(0.25, interval))
                if not self._alive:
                    break
                if time.time() < next_poll:
                    continue
                next_poll = time.time() + interval
                try:
                    status = self._chamber.status()
                    self._publish(status)
                    if self._sweep is not None:
                        self._service_sweep(status)
                except ChamberError as exc:
                    self.errorMessage.emit(str(exc))
                    time.sleep(1.0)
        finally:
            if self._chamber is not None:
                self._chamber.close()
                self._chamber = None
