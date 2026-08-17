#!/usr/bin/env python3
"""Stream the live rate as a tone while acquiring.

The offline path (sonify.py) renders a finished trace to a WAV; this
module is the realtime counterpart: a QAudioSink in pull mode draws
samples from a ToneSynth, and each capture's mean rate retargets the
pitch through a LiveMapper. QtMultimedia ships inside the PySide6
wheel, so this costs no new dependency - but the import still lives
here, in its own module, so sonify.py stays importable without the
GUI stack and a platform with no audio backend only loses the live
tone.
"""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import QIODevice, QObject
from PySide6.QtMultimedia import QAudioFormat, QAudioSink, QMediaDevices

from .sonify import DEFAULT_SAMPLE_RATE, LiveMapper, ToneSynth

#: Sink buffer in samples. Small enough that a retargeted pitch is heard
#: within ~0.1 s of the reading, large enough not to underrun while the
#: GUI repaints.
BUFFER_SAMPLES = 4096


class _ToneDevice(QIODevice):
    """Pull-mode source: the sink reads, the synth renders."""

    def __init__(self, synth: ToneSynth, parent=None):
        super().__init__(parent)
        self._synth = synth

    def readData(self, maxlen: int) -> bytes:  # noqa: N802 (Qt naming)
        n = min(max(maxlen // 2, 1), 4 * BUFFER_SAMPLES)
        audio = self._synth.render(n)
        pcm = (np.clip(audio, -1.0, 1.0) * 32767.0).astype("<i2")
        return pcm.tobytes()

    def writeData(self, data) -> int:  # noqa: N802 (Qt naming)
        return 0

    def bytesAvailable(self) -> int:  # noqa: N802 (Qt naming)
        return 2 * BUFFER_SAMPLES + super().bytesAvailable()

    def isSequential(self) -> bool:  # noqa: N802 (Qt naming)
        return True


class LiveSonifier(QObject):
    """Own the audio pipeline; feed() it rates, it plays pitches.

    Raises RuntimeError from the constructor when there is no output
    device or the device rejects 44.1 kHz mono 16-bit, so the caller
    can uncheck the box and say why.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        fmt = QAudioFormat()
        fmt.setSampleRate(DEFAULT_SAMPLE_RATE)
        fmt.setChannelCount(1)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        dev = QMediaDevices.defaultAudioOutput()
        if dev.isNull():
            raise RuntimeError("no audio output device")
        if not dev.isFormatSupported(fmt):
            raise RuntimeError("audio output rejects 44.1 kHz mono 16-bit")
        self.synth = ToneSynth()
        self.mapper = LiveMapper()
        self._device = _ToneDevice(self.synth, self)
        self._device.open(QIODevice.OpenModeFlag.ReadOnly)
        self._sink = QAudioSink(dev, fmt, self)
        self._sink.setBufferSize(2 * BUFFER_SAMPLES)
        self._sink.start(self._device)

    def feed(self, rate_hz: float) -> None:
        """One measured rate in; the tone glides to its pitch."""
        pitch = self.mapper.pitch(rate_hz)
        if pitch is not None:
            self.synth.set_pitch(pitch)

    def hush(self) -> None:
        """Fade to silence but keep the pipeline; feed() resumes it."""
        self.synth.mute()

    def stop(self) -> None:
        self._sink.stop()
        self._device.close()
