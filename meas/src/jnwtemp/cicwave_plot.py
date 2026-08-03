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
"""Live temperature trace rendered by cicwave's waveform plot.

cicwave already has the measurement ergonomics this app wants - A/B cursors with
a delta readout, and the keymap muscle memory (z/Z zoom, a/b cursors, f fit,
l legend) - so the trace is drawn by its ``PgWavePlot`` rather than a
reimplementation.

The bridge is small: ``WaveFile`` accepts an in-memory DataFrame ("virtual"
mode) and exposes a ``df`` setter, and ``PgWave.reload()`` re-reads its column
from that frame. Swapping the frame and reloading each wave therefore updates
the curves in place, leaving cursors, zoom and legend untouched.

Import is optional: without cicwave installed :data:`AVAILABLE` is False and the
app falls back to its own plot.
"""

from __future__ import annotations

from typing import Dict, Optional

import numpy as np

try:  # cicwave is an optional dependency
    import pandas as pd
    from cicwave.wave_pg import PgWave, PgWavePlot
    from cicwave.wavefiles import WaveFile
    AVAILABLE = True
except Exception:  # pragma: no cover - depends on the environment
    AVAILABLE = False
    PgWavePlot = object  # type: ignore

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QVBoxLayout, QWidget

from .plots import MAX_PLOT_POINTS, SERIES_3, TRACE_ANTIALIAS, envelope_decimate

#: Series name for an independent thermometer drawn alongside the sensors -
#: the chamber. Held apart from the sensor series everywhere: it is not one of
#: them, it is what they are being compared to, so ``show_traces`` must not
#: retire it just because it is absent from the sensor set.
REFERENCE_NAME = "chamber"
REFERENCE_UNIT = "°C"

#: Keys cicwave's own window binds, mirrored here so the muscle memory carries
#: over. Kept as data so the help text and the handler cannot drift apart.
#: Cursor-readout height inside the app. Two series of statistics fit.
READOUT_MAX_PX = 52

KEYMAP = [
    ("z", "zoom in"),
    ("Z", "zoom out"),
    ("a", "cursor A"),
    ("b", "cursor B"),
    ("f", "fit to data"),
    ("l", "legend"),
]


class LiveWavePlot(QWidget):
    """A cicwave ``PgWavePlot`` fed from live numpy arrays.

    Series are created on first use and then updated in place. The view is
    auto-fitted only until the user zooms or places a cursor; after that it is
    left alone, because a plot that keeps re-framing itself is useless for
    reading values off.
    """

    def __init__(self, x_column: str = "time_s", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._x_column = x_column
        self._waves: Dict[str, object] = {}
        self._wfiles: Dict[str, object] = {}
        self._colors: Dict[str, str] = {}
        self._units: Dict[str, str] = {}
        self._user_framed = False
        #: Shown in cicwave's legend and readout as the source of each wave.
        self._source_name = "jnwtemp"

        self.plot = PgWavePlot()
        # PgWavePlot is laid out for a standalone window, where the cursor
        # readout can afford a large share of the height. Here it is one of
        # three stacked panes, so cap it: two series need about two lines.
        readout = getattr(self.plot, "readout", None)
        if readout is not None:
            readout.setMaximumHeight(READOUT_MAX_PX)
        status = getattr(self.plot, "status", None)
        if status is not None:
            status.setMaximumHeight(18)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.plot)

        # Any manual framing hands control to the user for good.
        vb = getattr(self.plot, "plot", None)
        if vb is not None:
            try:
                vb.getViewBox().sigRangeChangedManually.connect(self._on_user_range)
            except Exception:
                pass

    # ------------------------------------------------------------------ data
    def _on_user_range(self, *_) -> None:
        self._user_framed = True

    def set_series(
        self, name: str, t: np.ndarray, y: np.ndarray, unit: str = ""
    ) -> None:
        """Create or update one named series.

        ``unit`` is applied explicitly rather than left to cicwave's name-based
        inference: nothing in a name like "GR07" says degrees Celsius, and an
        unlabelled axis is worse than a wrong one.
        """
        if not AVAILABLE:
            return
        t = np.asarray(t, dtype=float)
        y = np.asarray(y, dtype=float)
        # Thin to what a plot can actually show. The board counter produces a
        # point per millisecond per sensor, and handing pyqtgraph a curve that
        # long stalls the paint loop for seconds - the GUI stops responding
        # while the trace keeps growing. The envelope keeps the true min/max at
        # every x, so nothing visible is lost; cicwave's cursor readout and its
        # statistics then describe the thinned series.
        t, y = envelope_decimate(t, y, MAX_PLOT_POINTS)
        frame = pd.DataFrame({self._x_column: t, name: y})

        if name in self._waves and self._units.get(name, "") != unit:
            self._reset_waves()

        if name not in self._waves:
            wf = WaveFile(self._source_name, self._x_column, df=frame)
            wave = PgWave(wf, name, self._x_column)
            if unit:
                wave.yunit = unit
                wave._yclean = name
                wave.ylabel = f"{name} ({unit})"
            self._wfiles[name] = wf
            self._waves[name] = wave
            self._units[name] = unit
            result = self.plot.show_wave(wave)
            curve = getattr(wave, "curve", None)
            if curve is not None:
                # Same trade as the fallback plot: a live trace does not need
                # antialiased diagonals, and it is the single biggest cost in
                # the repaint.
                curve.opts["antialias"] = TRACE_ANTIALIAS
            # cicwave hands out the next palette entry on every add, so a sensor
            # that is removed and re-added (switching Both -> GR07 -> Both)
            # would come back a different colour. Pin it to whatever it had the
            # first time, for the life of the window.
            if result:
                chosen = result[1]
                remembered = self._colors.setdefault(name, chosen)
                if remembered != chosen:
                    self._recolour(wave, remembered)
        else:
            self._wfiles[name].df = frame
            self._waves[name].reload()

        if not self._user_framed and t.size:
            self.plot.autoSize()

    @staticmethod
    def _recolour(wave, colour: str) -> None:
        """Repaint a wave, keeping the pen width cicwave chose."""
        curve = getattr(wave, "curve", None)
        if curve is None:
            return
        try:
            pen = curve.opts.get("pen")
            width = pen.width() if pen is not None else 2
            from pyqtgraph import mkPen
            curve.setPen(mkPen(colour, width=width))
            wave.color = colour
        except Exception:
            pass

    def show_traces(self, series: dict, units=None) -> None:
        """Draw one or two named series. Shared interface with TemperaturePlot.

        ``units`` is either one unit for all series or a per-series mapping -
        an uncalibrated sensor is plotted as a rate while a calibrated one is
        in degrees, and cicwave gives each unit its own y axis.
        """
        want = {
            name: (units.get(name, "") if isinstance(units, dict) else (units or ""))
            for name in series
        }
        # A wave's unit picks the y axis it lands on, and cicwave keeps that
        # axis until every wave is gone. So changing quantity - or dropping a
        # sensor - is a rebuild, not a reload: otherwise the retired unit stays
        # on the left axis while the new curve is drawn against the right one.
        stale = [k for k in self._waves
                 if k not in series and k != REFERENCE_NAME]
        changed = any(self._units.get(n, u) != u for n, u in want.items())
        if stale or changed:
            self._reset_waves()
        for name, (t, v) in series.items():
            self.set_series(name, t, v, unit=want[name])

    def _reset_waves(self) -> None:
        """Drop every wave, and cicwave's axis bookkeeping along with it.

        Colours are remembered in ``_colors``, so a sensor comes back the same
        colour it went away with.
        """
        try:
            self.plot.removeAll()
            # removeAll clears the right axis by relabelling it, and in
            # pyqtgraph setting a label shows the axis again - leaving an empty
            # strip until some series needs a second unit. Put it back away.
            self.plot.plot.hideAxis("right")
        except Exception:  # pragma: no cover - defensive, cicwave internals
            pass
        self._waves.clear()
        self._wfiles.clear()
        self._units.clear()
        # Whatever the user had framed was in the old quantity's scale.
        self._user_framed = False

    def set_reference(self, t: np.ndarray, temp: np.ndarray) -> None:
        """Draw the chamber's own thermometer as a further named series.

        A wave rather than a bare curve so it joins the legend and the cursor
        readout: comparing the sensors against the chamber at a cursor is the
        whole point of showing it. Its colour is pinned before the first add so
        it does not take a sensor's palette entry.
        """
        self._colors.setdefault(REFERENCE_NAME, SERIES_3)
        self.set_series(REFERENCE_NAME, t, temp, unit=REFERENCE_UNIT)

    def clear_reference(self) -> None:
        # Dropped rather than emptied: an empty wave would keep a degrees axis
        # alive while the sensors are being shown as a rate.
        if REFERENCE_NAME in self._waves:
            self.drop_series(REFERENCE_NAME)

    # --- no-ops so the two plot implementations are interchangeable ---------
    def set_recording(self, on: bool) -> None:
        pass

    def clear_data(self) -> None:
        self.clear()

    def clear(self) -> None:
        """Empty every series, keeping the curves alive.

        Removing and re-adding waves would hand each one whatever colour is next
        in cicwave's palette, so a sensor changes colour every time the history
        is cleared. Feeding empty data keeps the curve, its colour, and its
        place in the legend.
        """
        empty = np.empty(0)
        for name in list(self._waves):
            self.set_series(name, empty, empty, unit=self._units.get(name, ""))
        self._user_framed = False

    def drop_series(self, name: str) -> None:
        wave = self._waves.pop(name, None)
        self._wfiles.pop(name, None)
        self._units.pop(name, None)
        # self._colors deliberately survives: a sensor keeps its colour for the
        # life of the window, even across being removed and re-added.
        if wave is not None:
            try:
                self.plot.removeLine(wave)
            except Exception:
                pass

    # ------------------------------------------------------------- keyboard
    def handle_key(self, text: str, ctrl: bool = False) -> bool:
        """Apply cicwave's binding for ``text``. Returns True if handled."""
        p = self.plot
        if text == "z" and not ctrl:
            p.zoomIn()
        elif text == "Z" or (text.lower() == "z" and ctrl):
            p.zoomOut()
        elif text.lower() == "a":
            p.placeCursorA()
        elif text.lower() == "b":
            p.placeCursorB()
        elif text.lower() == "f":
            self._user_framed = False
            p.autoSize()
        elif text.lower() == "l":
            p.toggleLegend()
        else:
            return False
        return True

    def refit(self) -> None:
        self._user_framed = False
        if AVAILABLE and self._waves:
            self.plot.autoSize()
