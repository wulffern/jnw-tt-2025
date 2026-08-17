#!/usr/bin/env python3
"""Turn a recorded frequency-vs-time trace into sound.

The chip's oscillator sits near 1 MHz and wobbles by parts in a
thousand as the temperature moves - far outside hearing on both
counts. What the ear IS good at is pitch over time, so the trace is
mapped, not played: the recording's own frequency span becomes an
audible band (log-spaced, because pitch perception is logarithmic),
and the whole recording is compressed onto a listening duration. A
15-minute chamber run becomes a ten-second glide; the thermal steps
are melody and the jitter is vibrato.

The synthesis is phase-continuous: the audio frequency curve is
integrated to a phase and the phase is what feeds the sine. Setting
sample values from f(t) directly would step the frequency between
samples and every step is a click.

This module is also the historical-data loader: `load_recording`
reads any CSV the recorder or the chamber scripts have written -
plain columns (rate_hz) or channel-prefixed ones (GR07_rate_hz) -
plus the .meta.json sidecar when one sits beside the file. Pure
numpy and stdlib, so it imports without the GUI stack.
"""

from __future__ import annotations

import csv
import json
import math
import os
import wave
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np

#: Audible band the recording's frequency span is mapped onto. A4 to
#: A6: high enough that small wobbles read as pitch, low enough not
#: to be shrill on laptop speakers.
DEFAULT_LO_HZ = 220.0
DEFAULT_HI_HZ = 880.0

DEFAULT_SAMPLE_RATE = 44100

#: Raised-cosine fade at both ends, so the tone does not start or
#: stop with a click.
FADE_S = 0.02


@dataclass
class Recording:
    """One loaded CSV: a shared time axis and per-channel columns."""

    path: str
    t_s: np.ndarray
    #: channel name -> {column name -> array}; the plain single-channel
    #: format loads as one channel named after the sidecar or the file.
    channels: Dict[str, Dict[str, np.ndarray]]
    meta: Optional[dict] = None
    columns: List[str] = field(default_factory=list)

    def channel(self, name: Optional[str] = None) -> Dict[str, np.ndarray]:
        """The named channel, or the only/first one when unnamed."""
        if name:
            if name not in self.channels:
                raise KeyError(
                    f"no channel {name!r} in {self.path}; "
                    f"have {sorted(self.channels)}")
            return self.channels[name]
        first = sorted(self.channels)[0]
        return self.channels[first]

    def rate_hz(self, name: Optional[str] = None) -> np.ndarray:
        ch = self.channel(name)
        if "rate_hz" not in ch:
            raise KeyError(f"channel has no rate_hz column in {self.path}")
        return ch["rate_hz"]


def _channel_from_filename(path: str) -> str:
    """jnwtemp-GR07-20260804-213435.csv -> GR07; anything else -> CH0."""
    base = os.path.basename(path)
    for tok in base.replace(".", "-").split("-"):
        if tok.upper().startswith("GR") and tok[2:].isdigit():
            return tok.upper()
    return "CH0"


def load_recording(path: str) -> Recording:
    """Read a recorder or chamber CSV and its sidecar.

    Two shapes exist in data/: the recorder's single-channel files
    carry bare columns (t_rel_s, temp_c, rate_hz, ...), and the dual
    or chamber files carry channel-prefixed ones (GR07_rate_hz).
    Both come back as Recording.channels[name][column]; non-numeric
    cells load as NaN so a truncated last line cannot kill a replay.
    """
    with open(path, newline="") as fi:
        reader = csv.reader(fi)
        header = next(reader)
        rows = [r for r in reader if r]
    if not rows:
        raise ValueError(f"{path}: no data rows")

    def col(i):
        out = np.empty(len(rows))
        for j, r in enumerate(rows):
            try:
                out[j] = float(r[i])
            except (ValueError, IndexError):
                out[j] = np.nan
        return out

    data = {name: col(i) for i, name in enumerate(header)}

    tname = "t_rel_s" if "t_rel_s" in data else header[0]
    t = data[tname]

    channels: Dict[str, Dict[str, np.ndarray]] = {}
    plain: Dict[str, np.ndarray] = {}
    for name, arr in data.items():
        if name in (tname, "t_unix"):
            continue
        if "_" in name:
            prefix, rest = name.split("_", 1)
            #: a prefix is a channel when it repeats with rate_hz
            if f"{prefix}_rate_hz" in data:
                channels.setdefault(prefix, {})[rest] = arr
                continue
        plain[name] = arr

    meta = None
    sidecar = os.path.splitext(path)[0] + ".meta.json"
    if os.path.exists(sidecar):
        try:
            with open(sidecar) as fi:
                meta = json.load(fi)
        except (OSError, json.JSONDecodeError):
            meta = None

    if plain and "rate_hz" in plain:
        name = _channel_from_filename(path)
        channels.setdefault(name, {}).update(plain)
    elif plain:
        #: columns like chamber_set_c belong to no channel; keep them
        #: reachable without inventing one per column
        channels.setdefault("_extra", {}).update(plain)

    if not channels:
        raise ValueError(f"{path}: no rate_hz column in {header}")

    return Recording(path=path, t_s=t, channels=channels, meta=meta,
                     columns=header)


def sonify(
    t_s: np.ndarray,
    f_hz: np.ndarray,
    duration_s: Optional[float] = 10.0,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    lo_hz: float = DEFAULT_LO_HZ,
    hi_hz: float = DEFAULT_HI_HZ,
    f_ref: Optional[tuple] = None,
) -> np.ndarray:
    """Map a frequency trace onto an audible tone.

    duration_s compresses the recording onto that many seconds of
    audio; None keeps real time. The trace's own span (or `f_ref` as
    (lo, hi), for comparable pitch across recordings) maps onto
    lo_hz..hi_hz on a log scale. A flat trace lands mid-band rather
    than dividing by zero. Returns float samples in [-1, 1].
    """
    t_s = np.asarray(t_s, dtype=float)
    f_hz = np.asarray(f_hz, dtype=float)
    ok = np.isfinite(t_s) & np.isfinite(f_hz)
    t_s, f_hz = t_s[ok], f_hz[ok]
    if t_s.size < 2:
        raise ValueError("need at least two finite samples to sonify")
    #- the recorder's clock steps back by sub-millisecond amounts
    #- where captures were stitched (26 places in one real file);
    #- np.interp needs ascending time, so sort stably rather than
    #- demand what the instrument does not deliver
    order = np.argsort(t_s, kind="stable")
    t_s, f_hz = t_s[order], f_hz[order]

    span_s = float(t_s[-1] - t_s[0])
    if span_s <= 0:
        raise ValueError("time axis does not advance")
    if duration_s is None:
        duration_s = span_s
    n = max(int(round(duration_s * sample_rate)), 2)

    #: the audio clock walks the recording clock, however compressed
    t_audio = np.linspace(t_s[0], t_s[-1], n)
    f_meas = np.interp(t_audio, t_s, f_hz)

    if f_ref is not None:
        f_lo, f_hi = float(f_ref[0]), float(f_ref[1])
    else:
        f_lo, f_hi = float(f_meas.min()), float(f_meas.max())
    if f_hi <= f_lo:
        x = np.full(n, 0.5)
    else:
        x = np.clip((f_meas - f_lo) / (f_hi - f_lo), 0.0, 1.0)

    #: log interpolation between the band edges: equal frequency
    #: steps become equal musical intervals
    pitch = lo_hz * (hi_hz / lo_hz) ** x

    phase = 2.0 * math.pi * np.cumsum(pitch) / sample_rate
    audio = np.sin(phase)

    nf = min(int(FADE_S * sample_rate), n // 2)
    if nf > 0:
        ramp = 0.5 - 0.5 * np.cos(np.linspace(0.0, math.pi, nf))
        audio[:nf] *= ramp
        audio[-nf:] *= ramp[::-1]
    return audio


#: Fraction of the first live rate used as the starting band width. The
#: offline path scales the whole trace's span onto two octaves; live, the
#: span is unknown at the start, and mapping the first few captures'
#: jitter onto two octaves would be all swoop and no signal. 50 ppm is
#: several times the capture-to-capture jitter seen on GR07, so jitter
#: starts as vibrato and thermal drift still fills the band quickly.
LIVE_MIN_SPAN = 50e-6

#: Pitch glide time constant for the live tone. Long enough to smooth the
#: step between captures into a slur, short enough that a thermal step
#: reads as a note change rather than a drift.
LIVE_GLIDE_S = 0.15


class LiveMapper:
    """Map rates onto the audible band as they arrive, one at a time.

    The live counterpart of sonify()'s autoscale: the band starts as
    ±LIVE_MIN_SPAN/2 around the first rate and only ever widens, so
    the mapping of what has already been heard never flips direction.
    Same log mapping as offline: equal frequency steps, equal
    intervals.
    """

    def __init__(self, lo_hz: float = DEFAULT_LO_HZ,
                 hi_hz: float = DEFAULT_HI_HZ,
                 min_span: float = LIVE_MIN_SPAN):
        self.lo_hz = float(lo_hz)
        self.hi_hz = float(hi_hz)
        self.min_span = float(min_span)
        self._lo: Optional[float] = None
        self._hi: Optional[float] = None

    def pitch(self, f_hz: float) -> Optional[float]:
        """Audible pitch for one measured rate; None for a NaN rate."""
        f = float(f_hz)
        if not math.isfinite(f):
            return None
        if self._lo is None:
            half = max(abs(f) * self.min_span, 1e-12) / 2.0
            self._lo, self._hi = f - half, f + half
        self._lo = min(self._lo, f)
        self._hi = max(self._hi, f)
        x = (f - self._lo) / (self._hi - self._lo)
        return self.lo_hz * (self.hi_hz / self.lo_hz) ** x


class ToneSynth:
    """Phase-continuous sine generator for the live tone, block by block.

    render(n) produces the next n samples; set_pitch() retargets the
    tone and the pitch glides there exponentially (LIVE_GLIDE_S), so
    capture-to-capture steps slur instead of clicking - the same
    reason the offline path integrates frequency to phase. The level
    ramps over FADE_S on start and mute() for the same clickless ends
    the offline fade provides.

    set_pitch() and mute() are single attribute writes, atomic under
    the GIL, so the audio thread may call render() with no lock.
    """

    def __init__(self, sample_rate: int = DEFAULT_SAMPLE_RATE,
                 glide_s: float = LIVE_GLIDE_S, gain: float = 0.3):
        self.sample_rate = int(sample_rate)
        self.glide_s = float(glide_s)
        self.gain = float(gain)
        self._pitch: Optional[float] = None
        self._target: Optional[float] = None
        self._phase = 0.0
        self._level = 0.0
        self._on = False

    def set_pitch(self, hz: float) -> None:
        if self._pitch is None:
            self._pitch = float(hz)
        self._target = float(hz)
        self._on = True

    def mute(self) -> None:
        """Fade to silence; the next set_pitch() fades back in."""
        self._on = False

    def render(self, n: int) -> np.ndarray:
        if n <= 0:
            return np.empty(0)
        if self._pitch is None:
            return np.zeros(n)
        target = self._target if self._target is not None else self._pitch
        dt = n / self.sample_rate
        end = target + (self._pitch - target) * math.exp(-dt / self.glide_s)
        pitch = np.linspace(self._pitch, end, n)
        self._pitch = end

        phase = self._phase + 2.0 * math.pi * np.cumsum(pitch) / self.sample_rate
        self._phase = float(phase[-1] % (2.0 * math.pi))
        audio = np.sin(phase)

        want = self.gain if self._on else 0.0
        step = self.gain * dt / FADE_S
        end_level = self._level + min(max(want - self._level, -step), step)
        audio *= np.linspace(self._level, end_level, n)
        self._level = end_level
        return audio


def write_wav(path: str, audio: np.ndarray,
              sample_rate: int = DEFAULT_SAMPLE_RATE) -> str:
    """16-bit mono WAV via the stdlib - no audio dependency."""
    pcm = np.clip(audio, -1.0, 1.0)
    pcm = (pcm * 32767.0).astype("<i2")
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm.tobytes())
    return path


def play_wav(path: str) -> None:
    """Hand the file to the platform's player, blocking until done.

    afplay on macOS, aplay/paplay on Linux, the shell association on
    Windows. Raises RuntimeError when nothing is available - the WAV
    is on disk either way.
    """
    import subprocess
    import sys

    if sys.platform == "darwin":
        subprocess.run(["afplay", path], check=True)
        return
    if sys.platform.startswith("linux"):
        for player in ("paplay", "aplay"):
            try:
                subprocess.run([player, path], check=True)
                return
            except (OSError, subprocess.CalledProcessError):
                continue
        raise RuntimeError("no paplay/aplay found to play " + path)
    if sys.platform.startswith("win"):
        os.startfile(path)  # noqa: S606 - opens the associated player
        return
    raise RuntimeError("no player known for this platform")
