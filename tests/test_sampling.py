import argparse
import contextlib
import importlib.util
import io
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from video_notes import materials, sampling
from video_notes.run import load, sample_times, save
from video_notes.sampling import overview_target, scene_candidates, select_candidates, select_hybrid, select_overview


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

    def test_hybrid_target_scales_with_duration_and_saved_cap(self):
        for duration, cap, expected in ((1, 24, 6), (120, 24, 6), (121, 24, 7),
                                        (600, 24, 24), (600, 48, 30), (1800, 48, 48), (1800, 12, 12)):
            with self.subTest(duration=duration, cap=cap):
                self.assertEqual(overview_target(600, 600 + duration, cap), expected)
        for start, end, cap in ((0, 0, 6), (0, float("nan"), 6), (float("inf"), 100, 6), (0, 10, 0)):
            with self.assertRaises(ValueError): overview_target(start, end, cap)

    def test_hybrid_keeps_all_anchors_and_adds_distinct_change_points(self):
        candidates = [{"timestamp": t, "score": 1, "reason": "stable_visual_change"} for t in range(10, 240, 40)]
        result = select_hybrid(candidates, 0, 240, 24)
        anchors = {row["timestamp"] for row in result if row["reason"] == "uniform_anchor"}
        changes = {row["timestamp"] for row in result if row["reason"] == "stable_visual_change"}
        self.assertEqual(anchors, set(sample_times(0, 240, 6)))
        self.assertEqual(changes, {row["timestamp"] for row in candidates})
        self.assertEqual(len(result), 12)
        self.assertTrue(anchors.isdisjoint(changes))

    def test_missing_nearby_and_early_candidates_supplement_coverage(self):
        for candidates in ([], [{"timestamp": 21, "score": 100, "reason": "change"}],
                           [{"timestamp": t, "score": t, "reason": "change"} for t in range(1, 11)]):
            with self.subTest(candidates=candidates):
                result = select_hybrid(candidates, 0, 120, 24)
                times = [row["timestamp"] for row in result]
                self.assertEqual(len(times), 6)
                self.assertTrue({20, 60, 100}.issubset(times))
                self.assertNotIn(21, times)
                self.assertTrue(any(row["reason"] == "uniform_supplement" for row in result))
                self.assertTrue(all(right - left >= 5 for left, right in zip(times, times[1:])))
                self.assertGreaterEqual(times[-1], 100)

    def test_hybrid_ranking_is_deterministic_and_time_distributed(self):
        candidates = [{"timestamp": t, "score": 1, "reason": "change"} for t in (5, 10, 30, 45, 50, 70, 85, 90, 110)]
        result = select_hybrid(candidates, 0, 120, 24)
        self.assertEqual(result, select_hybrid(list(reversed(candidates)), 0, 120, 24))
        self.assertEqual([row["timestamp"] for row in result if row["reason"] == "change"], [10, 50, 90])

    def test_hybrid_range_unique_count_and_short_clips(self):
        for start, duration, cap in ((0, .01, 24), (0, .1, 24), (600, 1, 24),
                                      (600.0015, 20, 24), (600, 1799, 48), (0, .001, 24)):
            with self.subTest(start=start, duration=duration):
                end = start + duration
                candidates = [{"timestamp": t, "score": 1, "reason": "change"}
                              for t in (start - 1, end, float("nan"), start + duration / 4)]
                result = select_hybrid(candidates, start, end, cap)
                times = [row["timestamp"] for row in result]
                self.assertEqual(times, sorted(set(times)))
                self.assertTrue(all(math.isfinite(t) and start <= t < end for t in times))
                self.assertLessEqual(len(times), overview_target(start, end, cap))
                if duration >= .01:
                    self.assertEqual(len(times), overview_target(start, end, cap))

    def test_explicit_strategies_keep_their_existing_selection_rules(self):
        self.assertEqual(select_overview([], 0, 600, 24, "uniform"),
                         [{"timestamp": t, "reason": "uniform"} for t in sample_times(0, 600, 5)])
        candidates = [{"timestamp": 10, "reason": "change", "score": 1}]
        self.assertEqual(select_overview(candidates, 0, 600, 24, "slides"), select_candidates(candidates, 0, 600, 24))


@unittest.skipUnless(importlib.util.find_spec("PIL"), "Pillow optional runtime unavailable")
class MaterialSampling(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        source = {"source": {"platform": "local"}, "metadata": {"title": "fixture", "duration_seconds": 600},
                  "content": {"segments": []}}
        save(self.directory / "source-in.json", source)
        with contextlib.redirect_stdout(io.StringIO()):
            materials.prepare(argparse.Namespace(out=str(self.directory), video="local-fixture",
                source_json=str(self.directory / "source-in.json"), transcript=None, page=None, use_env_cookie=False,
                start=0, end=None, preset="economy", session_log=None))
        self.args = argparse.Namespace(run=str(self.directory), kind="overview", times=None, media=None, use_env_cookie=False)
        save(self.directory / "sampling-plan.json", {"scan_range": [0, 600], "candidates": [],
            "candidate_frames": 120, "reader_frames": 0, "step_seconds": 5, "method": "pixel_change_stability"})
        media_tool = patch.object(materials.shutil, "which", return_value="/fixture/ffmpeg")
        media_tool.start(); self.addCleanup(media_tool.stop)

    def image(self, command, *, duplicate=False):
        from PIL import Image
        second = float(command[command.index("-ss") + 1])
        color = "white" if duplicate else (int(second) % 256, int(second / 3) % 256, int(second / 7) % 256)
        Image.new("RGB", (120, 80), color).save(command[-1])

    def extract(self, execute):
        with patch.object(materials, "media_input", return_value=("fixture", [])), \
                patch.object(materials, "execute", side_effect=execute), contextlib.redirect_stdout(io.StringIO()):
            materials.frames(self.args)

    def test_partial_extraction_reuses_frozen_selection_without_rescan(self):
        calls = []
        def fail(command):
            calls.append(command)
            if len(calls) == 1:
                return self.image(command)
            raise RuntimeError("fixture extraction interruption")
        with self.assertRaises(RuntimeError): self.extract(fail)
        frozen = load(self.directory / "run.json")
        self.assertEqual(frozen["sampling"]["strategy"], "hybrid")
        self.assertEqual(len(frozen["sampling"]["selected"]), 24)
        self.assertEqual(len(frozen["sampling_history"]["overview"]), 1)
        save(self.directory / "sampling-plan.json", {"scan_range": [0, 600], "candidates": [{"timestamp": 7, "reason": "changed", "score": 10}]})
        with patch.object(materials, "scan", side_effect=AssertionError("No rescan")):
            self.extract(self.image)
        after = load(self.directory / "run.json")
        self.assertEqual(after["sampling"], frozen["sampling"])
        self.assertEqual(len(after["sampling_history"]["overview"]), 24)
        self.assertEqual(after["budget"], frozen["budget"])
        with patch.object(materials, "media_input", side_effect=AssertionError("Cached extraction must not fetch media")), \
                contextlib.redirect_stdout(io.StringIO()):
            materials.frames(self.args)

    def test_old_sampling_strategy_and_frozen_budget_do_not_silently_change(self):
        run = load(self.directory / "run.json")
        run["budget"]["overview_frames"] = 12
        run["sampling"] = {"strategy": "slides"}
        save(self.directory / "run.json", run)
        before = (self.directory / "run.json").read_bytes()
        with patch.object(materials, "media_input", side_effect=AssertionError("No fetch")):
            with self.assertRaisesRegex(ValueError, "strategy changed"):
                materials.frames(self.args)
        self.assertEqual((self.directory / "run.json").read_bytes(), before)
        self.args.strategy = "slides"
        self.extract(self.image)
        run = load(self.directory / "run.json")
        self.assertEqual(run["budget"]["overview_frames"], 12)
        self.assertEqual(len(run["sampling"]["selected"]), 12)

    def test_exact_duplicates_consume_attempt_budget(self):
        self.extract(lambda command: self.image(command, duplicate=True))
        run = load(self.directory / "run.json")
        self.assertEqual(len(run["frames"]), 1)
        self.assertEqual(len(run["sampling_history"]["overview"]), 24)
        self.assertEqual(run["overview_exact_duplicates_skipped"], 23)
        self.args.times = "1"
        with patch.object(materials, "media_input", side_effect=AssertionError("No fetch when attempt budget is exhausted")):
            with self.assertRaisesRegex(ValueError, "budget exceeded"):
                materials.frames(self.args)

    def test_explicit_timestamps_can_add_within_the_saved_cap(self):
        run = load(self.directory / "run.json")
        run["processed_range"] = [0, 120]
        save(self.directory / "run.json", run)
        save(self.directory / "sampling-plan.json", {"scan_range": [0, 120], "candidates": []})
        self.extract(self.image)
        before = load(self.directory / "run.json")
        self.args.times = "1"
        self.args.strategy = "uniform"
        self.extract(self.image)
        after = load(self.directory / "run.json")
        self.assertEqual(len(after["sampling_history"]["overview"]), 7)
        self.assertEqual(after["sampling"], before["sampling"])
        self.assertEqual(after["budget"], before["budget"])

    def test_scan_is_bounded_to_180_frames_and_five_second_spacing(self):
        from PIL import Image
        commands = []
        def scan_frames(command, timeout):
            commands.append(command)
            output = Path(command[-1]).parent
            for index, color in enumerate(("white", "red", "red")):
                Image.new("RGB", (96, 54), color).save(output / f"c{index * 5000:04d}.jpg")
        with patch.object(materials, "execute", side_effect=scan_frames):
            plan = sampling.scan(self.directory, "fixture", [], 0, 600, max_candidates=1000, step=1)
        self.assertEqual(plan["step_seconds"], 5)
        self.assertEqual(commands[0][commands[0].index("-frames:v") + 1], "180")
        self.assertEqual(plan["reader_frames"], 0)
