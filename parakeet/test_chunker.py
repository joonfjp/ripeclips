"""Chunking regression tests; no ASR models or VAD downloads required."""
import unittest
from unittest.mock import patch

import numpy as np

from parakeet_service import chunker


class ChunkerTests(unittest.TestCase):
    def _chunk(self, duration, segments):
        wav = np.zeros(duration * chunker.TARGET_SR, dtype=np.float32)
        spans = [(s * chunker.TARGET_SR, e * chunker.TARGET_SR) for s, e in segments]
        with (
            patch.object(chunker, "CHUNK_TARGET_SEC", 60),
            patch.object(chunker, "CHUNK_MAX_SEC", 75),
            patch.object(chunker, "CHUNK_MIN_SEC", 20),
            patch.object(chunker, "_silero_speech_segments", return_value=spans),
        ):
            ranges = chunker.auto_chunk(wav)
        return ranges, chunker.slice_chunks(wav, ranges)

    def test_final_force_cut_does_not_invent_a_tail(self):
        # The short first span makes the packer extend through a long second
        # span and force-cut. Test both trailing silence and speech up to EOF.
        for duration in (90, 100):
            with self.subTest(duration=duration):
                ranges, pieces = self._chunk(duration, [(0, 10), (12, 90)])
                sr = chunker.TARGET_SR
                self.assertEqual(ranges, [(0, 60 * sr), (60 * sr, 90 * sr)])
                self.assertEqual([p.size for p in pieces], [60 * sr, 30 * sr])

    def test_speech_after_force_cut_is_retained(self):
        ranges, pieces = self._chunk(110, [(0, 10), (12, 90), (95, 100)])
        sr = chunker.TARGET_SR
        self.assertEqual(
            ranges, [(0, 60 * sr), (60 * sr, 90 * sr), (90 * sr, 100 * sr)]
        )
        self.assertEqual([p.size for p in pieces], [60 * sr, 30 * sr, 10 * sr])


if __name__ == "__main__":
    unittest.main()
