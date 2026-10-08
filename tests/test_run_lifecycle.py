import argparse
import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from video_notes import materials
from video_notes.run import PRESETS, activity, load, run_lock, save


class Lifecycle(unittest.TestCase):
    def test_same_run_concurrent_command_rejected(self):
        with tempfile.TemporaryDirectory() as folder, run_lock(folder):
            with self.assertRaises(RuntimeError):
                with run_lock(folder): pass

    def test_activity_is_unknown_model_cost_and_records_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                with activity(folder, "export"): raise ValueError("fixture")
            import json
            row = json.loads((Path(folder) / "activities.jsonl").read_text())
            self.assertEqual(row["status"], "failed")
            self.assertIsNone(row["native_tokens"])
            self.assertIsNone(row["api_usd"])

    def test_visual_only_long_video_keeps_remaining_range(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            save(root / "source.json", {"source": {"platform": "local"},
                "metadata": {"title": "long", "duration_seconds": 5400}, "content": {"segments": []}})
            args = argparse.Namespace(video="unused", out=str(root / "run"), source_json=str(root / "source.json"),
                transcript=None, start=600, end=None, page=None, use_env_cookie=False,
                preset="economy", session_log=None)
            with contextlib.redirect_stdout(io.StringIO()): materials.prepare(args)
            run = load(root / "run/run.json")
            self.assertEqual(run["processed_range"], [600, 2400])
            self.assertEqual(run["remaining_range"], [2400, 5400])

    def test_continuation_uses_remaining_scope_without_rewriting_parent(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); previous = root / "previous"
            source = {"source": {"platform": "local"}, "metadata": {"title": "long", "duration_seconds": 5400}, "content": {"segments": []}}
            save(previous / "source.json", source)
            save(previous / "run.json", {"status":"finished", "run_id":"parent", "remaining_range":[1800,5400], "video":"unused", "preset":"economy"})
            before = (previous / "run.json").read_bytes()
            args = argparse.Namespace(run=str(previous),out=str(root / "next"),preset=None,session_log=None,ledger=None)
            with contextlib.redirect_stdout(io.StringIO()): materials.continue_run(args)
            self.assertEqual((previous / "run.json").read_bytes(),before)
            next_run=load(root / "next/run.json")
            self.assertEqual(next_run["processed_range"],[1800,3600])
            self.assertEqual(next_run["remaining_range"],[3600,5400])
            self.assertEqual(next_run["previous_run_id"],"parent")
