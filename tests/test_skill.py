"""Behavior tests for scope, budgets, metering and failure/completion boundaries."""
import argparse
import contextlib
import io
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import core
import read_pack
import usage
import video


class Contracts(unittest.TestCase):
    def test_selected_part_duration(self):
        source = {"selection": {"page": 2}, "metadata": {"duration_seconds": 900, "pages": [{"page": 1, "duration_seconds": 600}, {"page": 2, "duration_seconds": 300}]}}
        self.assertEqual(core.selected_duration(source), 300)

    def test_bilibili_and_whisper_timestamp_shapes(self):
        expected = [{"start": 2.0, "end": 4.0, "text": "画面"}]
        self.assertEqual(core.segments_from({"body": [{"from": 2, "to": 4, "content": "画面"}]}), expected)
        self.assertEqual(core.segments_from({"segments": expected}), expected)

    def test_invalid_timestamps_rejected(self):
        for start, end in ((-1, 2), (3, 2), (0, math.inf), (math.nan, 2)):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                core.segments_from([{"start": start, "end": end, "text": "x"}])

    def test_long_course_checkpoints_instead_of_truncating_claim(self):
        rows = [{"start": i * 10, "end": i * 10 + 10, "text": "字" * 100} for i in range(100)]
        chosen, end = core.bounded_segments(rows, 0, 1000, 250)
        self.assertEqual(len(chosen), 2); self.assertEqual(end, 20)

    def test_resume_excludes_previous_segment(self):
        rows = [{"start": 0, "end": 10, "text": "first"}, {"start": 10, "end": 20, "text": "second"}]
        chosen, end = core.bounded_segments(rows, 10, 20, 200)
        self.assertEqual([r["text"] for r in chosen], ["second"])

    def test_oversize_single_segment_stops(self):
        with self.assertRaises(ValueError):
            core.bounded_segments([{"start": 0, "end": 1, "text": "字" * 100}], 0, 10, 30)

    def test_overview_covers_late_course(self):
        samples = core.sample_times(600, 7800, 12)
        self.assertEqual(len(samples), 12)
        self.assertGreater(samples[-1], 7200); self.assertGreater(samples[0], 600)
        self.assertTrue(all(600 <= t < 7800 for t in samples))

    def test_asr_offset_and_end_clamp(self):
        rows = [{"start": 0, "end": 3, "text": "first"}, {"start": 14, "end": 16, "text": "last"}, {"start": 16, "end": 18, "text": "outside"}]
        shifted = core.offset_segments(rows, 30, 45)
        self.assertEqual([(r["start"], r["end"]) for r in shifted], [(30, 33), (44, 45)])

    def test_slice_timestamp_clamp(self):
        chosen, end = core.bounded_segments([{"start": 20, "end": 50, "text": "context"}], 30, 45, 200)
        self.assertEqual(chosen[0]["start"], 30); self.assertEqual(chosen[0]["end"], 45)

    def test_oversize_segment_after_silence_stops(self):
        with self.assertRaises(ValueError): core.bounded_segments([{"start": 10, "end": 30, "text": "字" * 100}], 0, 30, 30)

    def test_gaps_and_overlaps_not_false_full_coverage(self):
        rows = [{"start": 0, "end": 10}, {"start": 5, "end": 15}, {"start": 90, "end": 100}]
        self.assertEqual(core.transcript_coverage(rows, 0, 100), 25)

    def test_missing_api_usage_not_zero(self):
        with self.assertRaises(ValueError): usage.api_usage({"id": "r1"})

    def test_cached_input_is_not_double_charged(self):
        response = {"model": "test-model", "usage": {"input_tokens": 1000, "input_tokens_details": {"cached_tokens": 200}, "output_tokens": 100}}
        prices = {"model": "test-model", "currency": "USD", "source_url": "fixture", "checked_at": "2026-10-06", "input_per_million": 2, "cached_input_per_million": .5, "output_per_million": 8}
        result = usage.price_usage(response, usage.api_usage(response), prices)
        self.assertEqual(result["usd"], "0.0025")

    def test_model_price_mismatch_rejected(self):
        with self.assertRaises(ValueError): usage.price_usage({"model": "different"}, {}, {"model": "expected"})

    def test_chat_completions_receipt(self):
        result = usage.api_usage({"usage": {"prompt_tokens": 100, "completion_tokens": 20, "prompt_tokens_details": {"cached_tokens": 40}}})
        self.assertEqual(result, {"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 20, "total_tokens": 120})

    def test_invalid_cached_counts(self):
        with self.assertRaises(ValueError): usage.api_usage({"usage": {"input_tokens": 10, "output_tokens": 1, "input_tokens_details": {"cached_tokens": 20}}})

    def test_native_unavailable_or_unrefreshed_not_zero(self):
        self.assertIsNone(usage.native_delta(None, None))
        snapshot = {"path": "fixture", "timestamp": "t1", "counters": {"input_tokens": 100, "cached_input_tokens": 10, "output_tokens": 20, "total_tokens": 120}}
        self.assertIsNone(usage.native_delta(snapshot, snapshot))

    def test_native_counter_reset_rejected(self):
        before = {"path": "fixture", "timestamp": "t1", "counters": {"input_tokens": 100, "cached_input_tokens": 10, "output_tokens": 20, "total_tokens": 120}}
        after = {"path": "fixture", "timestamp": "t2", "counters": {"input_tokens": 50, "cached_input_tokens": 5, "output_tokens": 10, "total_tokens": 60}}
        self.assertIsNone(usage.native_delta(before, after))

    def test_native_delta_observed_scope(self):
        before = {"path": "fixture", "timestamp": "t1", "counters": {"input_tokens": 100, "cached_input_tokens": 10, "output_tokens": 20, "total_tokens": 120}}
        after = {"path": "fixture", "timestamp": "t2", "counters": {"input_tokens": 150, "cached_input_tokens": 20, "output_tokens": 40, "total_tokens": 190}}
        result = usage.native_delta(before, after)
        self.assertEqual(result["total_tokens"], 70); self.assertEqual(result["cached_input_tokens"], 10)
        self.assertIn("final reply", result["scope"])

    def test_ledger_duplicate_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "usage.jsonl"
            usage.ledger_append(path, {"run_id": "same", "cost": None})
            usage.ledger_append(path, {"run_id": "same", "cost": None})
            self.assertEqual(len(path.read_text().splitlines()), 1)

    def test_counter_file_reads_only_token_events(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "session.jsonl"
            path.write_text(json.dumps({"type": "event_msg", "timestamp": "t1", "payload": {"type": "token_count", "info": {"total_token_usage": {"total_tokens": 42}}}}) + "\n" + json.dumps({"type": "response_item", "payload": {"content": "irrelevant"}}) + "\n")
            self.assertEqual(usage.counter_snapshot(path)["counters"]["total_tokens"], 42)

    def test_failed_prepare_keeps_unknown_duration_and_one_row(self):
        with tempfile.TemporaryDirectory() as folder:
            args = argparse.Namespace(out=str(Path(folder) / "failed"), video="missing", page=None, preset="economy", start=0, end=None, session_log=None, ledger=str(Path(folder) / "ledger.jsonl"))
            with contextlib.redirect_stdout(io.StringIO()):
                video.record_failed_prepare(args, core.now(), "fixture failure")
                video.record_failed_prepare(args, core.now(), "fixture failure")
            report = core.load(Path(args.out) / "usage.json")
            self.assertEqual(report["result_status"], "failed")
            self.assertIsNone(report["video"]["duration_seconds"])
            self.assertEqual(len(Path(args.ledger).read_text().splitlines()), 1)


class Stateful(unittest.TestCase):
    def setUp(self):
        # These cases test state/budget checks, never actual media execution.
        media_tool = patch.object(video.shutil, "which", return_value="/fixture/ffmpeg")
        media_tool.start(); self.addCleanup(media_tool.stop)
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        source = {"source": {"platform": "local", "canonical_url": None}, "metadata": {"title": "fixture", "duration_seconds": 120}, "selection": {"page": 1}, "content": {"source_type": "provided", "segments": [{"start": 0, "end": 120, "text": "real transcript"}]}}
        core.save(self.directory / "source-in.json", source)
        args = argparse.Namespace(out=str(self.directory), video="local-fixture", source_json=str(self.directory / "source-in.json"), transcript=None, page=None, use_env_cookie=False, start=0, end=None, preset="economy", session_log=None)
        with contextlib.redirect_stdout(io.StringIO()): video.prepare(args)

    def finish(self, status="complete", read_packs=""):
        args = argparse.Namespace(run=str(self.directory), status=status, read_packs=read_packs, ledger=str(self.directory / "ledger.jsonl"), native_usage=None, quota_before=None, quota_after=None)
        with contextlib.redirect_stdout(io.StringIO()): usage.finish(args)

    def test_cannot_finish_complete_without_reading(self):
        with self.assertRaises(ValueError): self.finish()

    def test_transcript_only_not_visual_completion(self):
        read_pack.prepare_pack(self.directory, "overview")
        with self.assertRaises(ValueError): self.finish(read_packs="p001")

    def test_reading_budget_is_reserved_before_viewing(self):
        run = core.load(self.directory / "run.json")
        run["budget"]["read_batches"] = 1; core.save(self.directory / "run.json", run)
        read_pack.prepare_pack(self.directory, "overview")
        with self.assertRaises(ValueError): read_pack.prepare_pack(self.directory, "overview")

    def test_finished_run_cannot_be_read_again(self):
        self.finish("extraction_only")
        with self.assertRaises(ValueError): read_pack.prepare_pack(self.directory, "overview")

    def test_partial_budget_cannot_be_marked_complete(self):
        run = core.load(self.directory / "run.json")
        run["remaining_range"] = [60, 120]; core.save(self.directory / "run.json", run)
        with self.assertRaises(ValueError): self.finish()

    def test_finish_and_retry_records_one_row(self):
        self.finish("extraction_only"); self.finish("extraction_only")
        rows = (self.directory / "ledger.jsonl").read_text().splitlines()
        self.assertEqual(len(rows), 1)
        report = json.loads(rows[0])
        self.assertIsNone(report["tokens"]["native_actual"])
        self.assertIsNone(report["cost"]["codex_subscription_usd"])

    def test_extra_detail_frames_blocked_before_media_fetch(self):
        args = argparse.Namespace(run=str(self.directory), kind="detail", times="1,2,3,4,5,6,7", media=None, use_env_cookie=False)
        with patch.object(video, "media_input") as fetch:
            with self.assertRaises(ValueError): video.frames(args)
            fetch.assert_not_called()

    def test_cached_frame_output_does_not_repeat_transcript(self):
        run = core.load(self.directory / "run.json")
        run["frames"] = [{"id": "f0001", "kind": "detail", "timestamp": 1.0,
                          "path": "cached.jpg", "nearby_segments": [{"text": "private transcript"}]}]
        core.save(self.directory / "run.json", run)
        args = argparse.Namespace(run=str(self.directory), kind="detail", times="1", media=None, use_env_cookie=False)
        output = io.StringIO()
        with patch.object(video, "media_input") as fetch, contextlib.redirect_stdout(output):
            video.frames(args)
            fetch.assert_not_called()
        result = json.loads(output.getvalue())
        self.assertTrue(result["cached"])
        self.assertEqual(set(result["frames"][0]), {"id", "timestamp", "path"})

    def test_timestamp_outside_processed_range_rejected(self):
        args = argparse.Namespace(run=str(self.directory), kind="detail", times="130", media=None, use_env_cookie=False)
        with self.assertRaises(ValueError): video.frames(args)

    def test_unknown_pack_not_claimed_as_read(self):
        with self.assertRaises(ValueError): self.finish("partial", "unknown")


if __name__ == "__main__": unittest.main(verbosity=2)
