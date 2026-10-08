import importlib.util
import tempfile
import unittest
from pathlib import Path

from video_notes.sampling import scene_candidates, select_candidates


class Sampling(unittest.TestCase):
    def test_each_time_bucket_keeps_coverage_with_early_changes(self):
        candidates = [{"timestamp": t, "score": 1, "reason": "change"} for t in range(10)]
        selected = select_candidates(candidates, 0, 120, 6)
        self.assertEqual(len(selected), 6)
        self.assertGreater(selected[-1]["timestamp"], 100)
        self.assertEqual(selected[-1]["reason"], "uniform_fallback")

    @unittest.skipUnless(importlib.util.find_spec("PIL"), "Pillow optional runtime unavailable")
    def test_transient_flash_is_not_a_stable_slide(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as folder:
            paths = []
            for i, color in enumerate(["white", "black", "white", "white", "red", "red", "red"]):
                path = Path(folder) / f"{i}.jpg"
                Image.new("RGB", (96, 54), color).save(path)
                paths.append(path)
            result = scene_candidates(paths, 60, 5)
            self.assertNotIn(65, [r["timestamp"] for r in result])
            self.assertNotIn(70, [r["timestamp"] for r in result])
            self.assertIn(80, [r["timestamp"] for r in result])

    def test_selection_stays_bounded(self):
        result = select_candidates([], 600, 1800, 12)
        self.assertTrue(all(600 <= r["timestamp"] < 1800 for r in result))
        with self.assertRaises(ValueError): select_candidates([], 0, 100, 0)
