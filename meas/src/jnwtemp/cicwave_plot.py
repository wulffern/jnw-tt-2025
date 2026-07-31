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
        self._user_framed = False

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

    def set_series(self, name: str, t: np.ndarray, y: np.ndarray) -> None:
        """Create or update one named series."""
        if not AVAILABLE:
            return
        t = np.asarray(t, dtype=float)
        y = np.asarray(y, dtype=float)
        if t.size == 0:
            return
        frame = pd.DataFrame({self._x_column: t, name: y})

        if name not in self._waves:
            wf = WaveFile(f"{name}.csv", self._x_column, df=frame)
            wave = PgWave(wf, name, self._x_column)
            self._wfiles[name] = wf
            self._waves[name] = wave
            self.plot.show_wave(wave)
        else:
            self._wfiles[name].df = frame
            self._waves[name].reload()

        if not self._user_framed:
            self.plot.autoSize()

    def show_traces(self, series: dict) -> None:
        """Draw one or two named series. Shared interface with TemperaturePlot."""
        for name, (t, v) in series.items():
            self.set_series(name, t, v)
        for stale in [k for k in self._waves if k not in series]:
            self.drop_series(stale)

    # --- no-ops so the two plot implementations are interchangeable ---------
    def set_recording(self, on: bool) -> None:
        pass

    def clear_data(self) -> None:
        self.clear()

    def clear(self) -> None:
        for name, wave in list(self._waves.items()):
            try:
                self.plot.removeLine(wave)
            except Exception:
                pass
        self._waves.clear()
        self._wfiles.clear()
        self._user_framed = False

    def drop_series(self, name: str) -> None:
        wave = self._waves.pop(name, None)
        self._wfiles.pop(name, None)
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
