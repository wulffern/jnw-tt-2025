#!/usr/bin/env python3
"""sonify against the historical recordings in data/.

The loader and the synthesis are tested on the real files, not
fixtures: the single-channel recorder format, the dual-channel
chamber trace, and the sidecar all exist in data/ exactly as the
instruments wrote them, and the point of the loader is to read
those. Tests that need a file skip when it is absent, so a sparse
checkout still passes.
"""

import os
import sys
import unittest
import wave

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))

from jnwtemp import sonify as sf  # noqa: E402

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
SINGLE = os.path.join(DATA, "jnwtemp-GR07-20260804-213435.csv")
DUAL = os.path.join(DATA, "2026-08-03_chamber_trace.csv")
BOTH = os.path.join(DATA, "jnwtemp-BOTH-20260803-170642.csv")


def _need(path):
    if not os.path.exists(path):
        raise unittest.SkipTest(f"historical file not present: {path}")


class LoadRecording(unittest.TestCase):

    def test_single_channel_recorder_csv(self):
        _need(SINGLE)
        rec = sf.load_recording(SINGLE)
        self.assertIn("GR07", rec.channels)
        f = rec.rate_hz()
        self.assertGreater(len(f), 100)
        #- GR07 sits near 1 MHz on every recording of it
        self.assertGreater(np.nanmedian(f), 1e5)
        self.assertLess(np.nanmedian(f), 1e7)
        self.assertEqual(len(rec.t_s), len(f))
        #- the loader returns the file as written, and the real file
        #- steps its clock back sub-millisecond where captures were
        #- stitched -- sonify() sorts; here it only has to be rare
        d = np.diff(rec.t_s)
        self.assertLess(np.mean(d < 0), 0.01)
        self.assertGreater(np.median(d), 0)
        #- the sidecar sits beside this file and identifies the format
        self.assertIsNotNone(rec.meta)
        self.assertIn("jnwtemp", rec.meta.get("format", ""))

    def test_dual_channel_chamber_trace(self):
        _need(DUAL)
        rec = sf.load_recording(DUAL)
        self.assertIn("GR07", rec.channels)
        self.assertIn("GR06", rec.channels)
        #- the two oscillators are an order of magnitude apart, which
        #- is how a swapped column would show
        self.assertGreater(np.nanmedian(rec.rate_hz("GR07")),
                           3 * np.nanmedian(rec.rate_hz("GR06")))

    def test_both_recorder_csv_keeps_shared_columns(self):
        _need(BOTH)
        rec = sf.load_recording(BOTH)
        self.assertIn("GR07", rec.channels)
        self.assertIn("GR06", rec.channels)

    def test_unknown_channel_is_loud(self):
        _need(SINGLE)
        rec = sf.load_recording(SINGLE)
        with self.assertRaises(KeyError):
            rec.channel("GR99")


class Sonify(unittest.TestCase):

    def test_length_and_range(self):
        _need(SINGLE)
        rec = sf.load_recording(SINGLE)
        audio = sf.sonify(rec.t_s, rec.rate_hz(), duration_s=2.0,
                          sample_rate=8000)
        self.assertEqual(len(audio), 16000)
        self.assertTrue(np.all(np.isfinite(audio)))
        self.assertLessEqual(np.abs(audio).max(), 1.0)
        #- a sine at healthy amplitude, not silence
        self.assertGreater(np.abs(audio).max(), 0.9)

    def test_flat_trace_lands_mid_band(self):
        #- constant frequency must not divide by zero; the pitch is
        #- the log midpoint of the band, counted by zero crossings
        t = np.linspace(0, 1, 1000)
        f = np.full_like(t, 42.0)
        rate = 44100
        audio = sf.sonify(t, f, duration_s=1.0, sample_rate=rate,
                          lo_hz=220.0, hi_hz=880.0)
        crossings = np.sum(np.diff(np.signbit(audio)))
        pitch = crossings / 2.0
        self.assertAlmostEqual(pitch, np.sqrt(220.0 * 880.0), delta=15)

    def test_span_maps_to_band_edges(self):
        #- first half at the trace minimum, second half at the
        #- maximum: the two halves must count lo_hz and hi_hz
        rate = 44100
        t = np.linspace(0, 2, 2000)
        f = np.where(t < 1.0, 1000.0, 2000.0)
        audio = sf.sonify(t, f, duration_s=2.0, sample_rate=rate,
                          lo_hz=200.0, hi_hz=800.0)
        half = len(audio) // 2
        lo = np.sum(np.diff(np.signbit(audio[: half - rate // 10]))) / 2 / 0.9
        hi = np.sum(np.diff(np.signbit(audio[half + rate // 10:]))) / 2 / 0.9
        self.assertAlmostEqual(lo, 200.0, delta=20)
        self.assertAlmostEqual(hi, 800.0, delta=25)

    def test_real_time_duration(self):
        t = np.linspace(0, 3.0, 300)
        f = np.linspace(10.0, 20.0, 300)
        audio = sf.sonify(t, f, duration_s=None, sample_rate=1000)
        self.assertEqual(len(audio), 3000)

    def test_wav_roundtrip(self):
        _need(SINGLE)
        import tempfile
        rec = sf.load_recording(SINGLE)
        audio = sf.sonify(rec.t_s, rec.rate_hz(), duration_s=0.5,
                          sample_rate=8000)
        with tempfile.TemporaryDirectory() as d:
            path = sf.write_wav(os.path.join(d, "t.wav"), audio, 8000)
            with wave.open(path, "rb") as wf:
                self.assertEqual(wf.getnchannels(), 1)
                self.assertEqual(wf.getframerate(), 8000)
                self.assertEqual(wf.getnframes(), len(audio))


class LiveMapperTest(unittest.TestCase):

    def test_first_rate_lands_mid_band(self):
        m = sf.LiveMapper(lo_hz=220.0, hi_hz=880.0)
        self.assertAlmostEqual(m.pitch(1e6), np.sqrt(220.0 * 880.0), places=6)

    def test_jitter_stays_small_and_monotonic(self):
        #- ppm-level jitter inside the seeded band must move the pitch
        #- by far less than the band, and in the rate's direction
        m = sf.LiveMapper(lo_hz=220.0, hi_hz=880.0)
        mid = m.pitch(1e6)
        up = m.pitch(1e6 + 2.0)     # +2 ppm
        down = m.pitch(1e6 - 2.0)
        self.assertGreater(up, mid)
        self.assertLess(down, mid)
        self.assertLess(up / down, 1.3)

    def test_band_expands_to_real_drift(self):
        #- a thermal step far outside the seeded band becomes the new
        #- edge, so it maps to the top of the audible band
        m = sf.LiveMapper(lo_hz=220.0, hi_hz=880.0)
        m.pitch(1e6)
        self.assertAlmostEqual(m.pitch(1.001e6), 880.0, places=6)
        self.assertAlmostEqual(m.pitch(1e6 - 1000.0), 220.0, places=6)

    def test_nan_rate_is_skipped(self):
        m = sf.LiveMapper()
        self.assertIsNone(m.pitch(float("nan")))


class ToneSynthTest(unittest.TestCase):

    def test_silent_until_first_pitch(self):
        s = sf.ToneSynth(sample_rate=8000)
        self.assertTrue(np.all(s.render(1000) == 0.0))

    def test_fades_in_and_holds_pitch(self):
        s = sf.ToneSynth(sample_rate=8000, gain=0.3)
        s.set_pitch(440.0)
        audio = s.render(8000)
        #- clickless start: the very first samples are still quiet
        self.assertLess(np.abs(audio[:8]).max(), 0.05)
        self.assertAlmostEqual(np.abs(audio[4000:]).max(), 0.3, delta=0.02)
        #- 440 Hz counted by zero crossings over the settled second half
        crossings = np.sum(np.diff(np.signbit(audio[4000:])))
        self.assertAlmostEqual(crossings / 2 / 0.5, 440.0, delta=10)

    def test_phase_continuous_across_blocks(self):
        #- rendered in many small blocks with a retune in the middle,
        #- the waveform must never jump by more than one sample of the
        #- highest pitch - a phase break would show as a step toward 2
        s = sf.ToneSynth(sample_rate=8000, gain=1.0)
        s.set_pitch(440.0)
        blocks = []
        for i in range(60):
            if i == 30:
                s.set_pitch(880.0)
            blocks.append(s.render(160))
        audio = np.concatenate(blocks)
        self.assertLess(np.abs(np.diff(audio)).max(),
                        1.5 * 2 * np.pi * 880.0 / 8000)

    def test_mute_fades_to_silence(self):
        s = sf.ToneSynth(sample_rate=8000)
        s.set_pitch(440.0)
        s.render(8000)
        s.mute()
        s.render(8000)
        self.assertTrue(np.all(s.render(100) == 0.0))


if __name__ == "__main__":
    unittest.main()
