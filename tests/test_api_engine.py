"""Standalone acquisition, actual HTTP vision input, evidence validation and resume."""
import argparse
import base64
import contextlib
import copy
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from video_notes import ApiBudget, ProviderConfig, analyze, analyze_prepared, api, cli, engine, materials, notes
from video_notes.run import load, save

SECRET = "engine-loopback-key"


def note_response(material):
    segments = material["transcript"]
    frame = material["frames"][0]["id"]
    refs = ["frame:" + frame]
    if segments:
        refs.append("segment:" + segments[0]["id"])
    left, right = material["processed_range"]
    return {"schema_version": 2, "title": "Synthetic learning note", "subtitle": "Fixture",
            "takeaway": "The sampled image and speech establish the fixture concept.", "takeaway_evidence_refs": refs,
            "sections": [{"id": "concept", "title": "Concept", "evidence_refs": refs, "blocks": [
                {"type": "paragraph", "text": "Visible synthetic frame with supplied speech.", "attribution": "speaker" if segments else "uncertain", "evidence_refs": refs},
                {"type": "paragraph", "text": "A clearly labeled optional learning tip.", "attribution": "agent", "evidence_refs": []}]}],
            "timeline": [{"start": left, "end": right, "title": "Fixture interval", "text": "Concept",
                          "section_id": "concept", "evidence_refs": refs}],
            "figures": [{"frame_id": frame, "title": "Image sample", "caption": "A sampled synthetic image", "evidence_refs": ["frame:" + frame]}],
            "caveats": [], "sources": []}


class ModelFixture:
    def __init__(self, *, detail=None, mutate=None, usage=True, unknown_stage=None):
        self.requests = []
        self.detail = detail or []
        self.mutate = mutate
        self.usage = usage
        self.unknown_stage = unknown_stage

    def __call__(self, config, request):
        self.requests.append(copy.deepcopy(request))
        material = json.loads(request["messages"][1]["content"][0]["text"])
        stage = material["stage"]
        if stage == self.unknown_stage:
            raise OSError(SECRET + " unknown transport")
        if stage == "synthesis":
            content = note_response(material)
        else:
            source = material["material"]
            frame = source["frames"][0]["id"]
            refs = ["frame:" + frame]
            if source["segments"]:
                refs.append("segment:" + source["segments"][0]["id"])
            content = {"observations": [{"text": "Visible synthetic frame and its timestamp.", "attribution": "uncertain", "evidence_refs": refs}],
                       "detail_timestamps": self.detail if stage == "overview" else []}
        if self.mutate:
            self.mutate(stage, content)
        value = {"id": "fixture-" + stage, "model": config.model,
                 "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(content)}}]}
        if self.usage:
            value["usage"] = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
                              "prompt_tokens_details": {"cached_tokens": 0}}
        return api.TransportResponse(200, json.dumps(value).encode())


class StandaloneEngine(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run = self.root / "run"
        self.config = ProviderConfig("http://127.0.0.1:1/v1", "fixture-vision", api_key=SECRET)
        self.model = ModelFixture()
        self.ledger = str(self.root / "ledger.jsonl")
        self.prepare(self.run)

    def prepare(self, directory, *, duration=20, requested_end=None, speech=True):
        source = {"source": {"platform": "local"}, "metadata": {"title": "Synthetic course", "duration_seconds": duration},
                  "content": {"source_type": "provided_transcript", "segments": [
                      {"start": 0, "end": min(duration, 20), "text": "The supplied fixture concept."}] if speech else []}}
        save(self.root / "source.json", source)
        with contextlib.redirect_stdout(io.StringIO()):
            materials.prepare(argparse.Namespace(video="fixture-video", out=directory, source_json=self.root / "source.json",
                transcript=None, start=0, end=requested_end, page=None, use_env_cookie=False, preset="economy", session_log=None))
        run = load(directory / "run.json")
        (directory / "frames").mkdir()
        from PIL import Image
        for index, color in enumerate(("#bc3232", "#24628a"), 1):
            path = directory / "frames" / f"f{index:04d}.jpg"
            Image.new("RGB", (120, 80), color).save(path, quality=80)
            run["frames"].append({"id": f"f{index:04d}", "timestamp": min(run["processed_range"][1] - .5, index * 5), "kind": "overview",
                                 "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "nearby_segments": load(directory / "segments.json")})
        save(directory / "run.json", run)

    def analyze(self, **kwargs):
        with patch("video_notes.api.send", self.model):
            return analyze_prepared(self.run, provider=self.config, allow_asr=False, ledger=self.ledger, **kwargs)

    def test_prepared_multimodal_to_frozen_note_and_zero_call_repeat(self):
        first = self.analyze()
        self.assertEqual(first["status"], "complete", first["reason"])
        self.assertEqual(len(self.model.requests), 2)
        body = self.model.requests[0]
        data_url = body["messages"][1]["content"][1]["image_url"]["url"]
        self.assertTrue(base64.b64decode(data_url.split(",", 1)[1]).startswith(b"\xff\xd8\xff"))
        material = json.loads(body["messages"][1]["content"][0]["text"])
        self.assertEqual(material["material"]["segments"][0]["id"], "s0001")
        self.assertEqual(material["material"]["frames"][0]["timestamp"], 5)
        self.assertEqual(first["usage"]["tokens"]["api_actual"]["total_tokens"], 240)
        self.assertEqual([row["stage"] for row in first["usage"]["api_calls"]], ["overview", "synthesis"])
        self.assertTrue(all(row["reading_method"] == "api_material_submission_and_response" for row in first["note"]["snapshot"]["reading"]))
        self.assertEqual(first["note"]["provenance"]["kind"], "api_engine")
        reading_record = first["note"]["snapshot"]["reading"][0]
        self.assertTrue(reading_record["api_call_id"])
        self.assertEqual(len(reading_record["request_hash"]), 64)
        self.assertNotIn(SECRET, json.dumps(first))
        self.assertNotIn(str(self.run), json.dumps(first["note"]))
        rendered = Path(first["outputs"]["html"]).read_text()
        self.assertIn("API 实际总 token", rendered)
        self.assertIn("240", rendered)
        self.assertIn("笔记生成调用均计入", rendered)
        self.assertIn("API 已提交画面帧", rendered)
        again = self.analyze(outputs=("md",))
        self.assertEqual(again["status"], "complete", again["reason"])
        self.assertTrue(again["cached"])
        self.assertEqual(len(self.model.requests), 2)
        self.assertEqual(again["note"]["content_hash"], first["note"]["content_hash"])
        self.assertEqual(set(again["outputs"]), {"md"})

    def test_completed_cache_needs_same_settings_but_no_credential(self):
        first = self.analyze()
        self.assertEqual(first["status"], "complete")
        no_key = ProviderConfig(self.config.base_url, self.config.model, api_key_env="FIXTURE_ABSENT_KEY")
        again = analyze_prepared(self.run, provider=no_key, allow_asr=False, ledger=self.ledger)
        self.assertEqual(again["status"], "complete", again["reason"])
        for options in ({"focus": "different"}, {"language": "en"}, {"budget": ApiBudget(max_calls=4)},
                        {"provider": ProviderConfig(self.config.base_url, "different", api_key=SECRET)}):
            kwargs = {"provider": self.config, "allow_asr": False, "ledger": self.ledger, **options}
            result = analyze_prepared(self.run, **kwargs)
            self.assertEqual(result["status"], "failed")
        self.assertEqual(len(self.model.requests), 2)

    def test_budget_preflight_rejects_before_paid_overview(self):
        result = self.analyze(budget=ApiBudget(max_calls=1))
        self.assertEqual(result["status"], "failed")
        self.assertIn("overview and synthesis", result["reason"])
        self.assertEqual(self.model.requests, [])

    def test_optional_detail_skips_when_synthesis_reservation_would_be_lost(self):
        self.model.detail = [2]
        for label, budget in (("calls", ApiBudget(max_calls=2)), ("images", ApiBudget(max_image_presentations=1))):
            directory = self.root / label; self.prepare(directory)
            model = ModelFixture(detail=[2])
            with patch("video_notes.api.send", model):
                result = analyze_prepared(directory, provider=self.config, budget=budget, allow_asr=False, ledger=self.ledger)
            self.assertEqual(result["status"], "complete", result["reason"])
            self.assertEqual([json.loads(row["messages"][1]["content"][0]["text"])["stage"] for row in model.requests], ["overview", "synthesis"])
            self.assertTrue(any("预算不足" in caveat for caveat in result["note"]["caveats"]))

    def test_invalid_detail_selection_fails_before_additional_dispatch(self):
        self.model.detail = [21]
        result = self.analyze()
        self.assertEqual(result["status"], "failed")
        self.assertIn("outside", result["reason"])
        self.assertEqual(len(self.model.requests), 1)
        self.assertNotEqual(load(self.run / "run.json")["status"], "finished")

    def test_active_settings_change_rejected_without_repeat_dispatch(self):
        self.model.unknown_stage = "synthesis"
        self.analyze()
        before = len(self.model.requests)
        result = self.analyze(focus="A different focus")
        self.assertEqual(result["status"], "outcome_unknown")
        self.assertIn("settings", result["reason"])
        self.assertEqual(len(self.model.requests), before)

    def test_invalid_visual_response_accounted_and_never_repaired(self):
        self.model.mutate = lambda stage, data: data["observations"][0].update(evidence_refs=["segment:s0001"]) if stage == "overview" else None
        first = self.analyze()
        self.assertEqual(first["status"], "failed")
        self.assertIn("frame-backed", first["reason"])
        self.assertEqual(first["usage"]["tokens"]["api_actual"]["total_tokens"], 120)
        self.assertNotEqual(load(self.run / "run.json")["status"], "finished")
        self.assertFalse((self.run / "notes/note-v001.json").exists())
        again = self.analyze()
        self.assertEqual(again["status"], "failed")
        self.assertEqual(len(self.model.requests), 1)

    def test_invalid_note_refs_or_time_fail_before_freeze(self):
        mutations = [
            lambda data: data.update(takeaway_evidence_refs=["frame:missing"]),
            lambda data: data["timeline"][0].update(end=1000),
            lambda data: data["sections"][0]["blocks"][0].update(evidence_refs=[]),
            lambda data: data.update(takeaway_evidence_refs=[]),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                directory = self.root / f"invalid-{index}"; self.prepare(directory)
                model = ModelFixture(mutate=lambda stage, data: mutate(data) if stage == "synthesis" else None)
                with patch("video_notes.api.send", model):
                    result = analyze_prepared(directory, provider=self.config, allow_asr=False, ledger=self.ledger)
                self.assertEqual(result["status"], "failed", result)
                self.assertEqual(len(model.requests), 2)
                self.assertNotEqual(load(directory / "run.json")["status"], "finished")
                self.assertFalse((directory / "usage.json").exists())
                self.assertFalse((directory / "notes/note-v001.json").exists())
                self.assertEqual(result["usage"]["tokens"]["api_actual"]["total_tokens"], 240)

    def test_internal_material_sources_are_preserved_without_external_links_or_rebuying(self):
        def mutate(stage, data):
            if stage == "synthesis":
                data["sources"] = [
                    {"type": "transcript", "description": "Supplied speech", "evidence_refs": ["segment:s0001"]},
                    {"type": "validated_visual_observations", "description": "Submitted visual", "evidence_refs": ["frame:f0001"]},
                    {"label": "Visible reference", "url": "https://example.org/reference"}]
        self.model.mutate = mutate
        result = self.analyze()
        self.assertEqual(result["status"], "complete", result["reason"])
        self.assertEqual(result["note"]["sources"], [{"label": "Visible reference", "url": "https://example.org/reference"}])
        self.assertIn("材料说明：Supplied speech [segment:s0001]", result["note"]["caveats"])
        self.assertIn("材料说明：Submitted visual [frame:f0001]", result["note"]["caveats"])
        again = self.analyze()
        self.assertEqual(again["note"]["content_hash"], result["note"]["content_hash"])
        self.assertEqual(len(self.model.requests), 2)

    def test_internal_material_sources_reject_missing_wrong_kind_or_unavailable_refs(self):
        for index, refs in enumerate(([], ["frame:f0001"], ["segment:missing"])):
            directory = self.root / f"source-invalid-{index}"; self.prepare(directory)
            def mutate(stage, data):
                if stage == "synthesis":
                    data["sources"] = [{"type": "transcript", "description": "Supplied speech", "evidence_refs": refs}]
            model = ModelFixture(mutate=mutate)
            with patch("video_notes.api.send", model):
                first = analyze_prepared(directory, provider=self.config, allow_asr=False, ledger=self.ledger)
                again = analyze_prepared(directory, provider=self.config, allow_asr=False, ledger=self.ledger)
            self.assertEqual(first["status"], "failed")
            self.assertEqual(again["status"], "failed")
            self.assertEqual(len(model.requests), 2)
            self.assertEqual(first["usage"]["tokens"]["api_actual"]["total_tokens"], 240)
            self.assertFalse((directory / "usage.json").exists())

    def test_reviewed_api_note_preserves_generation_metering(self):
        first = self.analyze()
        revised = notes.save_note(self.run, {**first["note"], "provenance": {
            "kind": "api_engine_semantic_review", "reference_policy": "Reviewed visible explanation"}})
        self.assertEqual(revised["revision"], 2)
        self.assertEqual(revised["snapshot"]["usage"], first["note"]["snapshot"]["usage"])
        explanations = " ".join(notes.usage_explanations(revised))
        self.assertIn("笔记生成调用均计入", explanations)
        self.assertNotIn("笔记撰写、后续对话与发布不包含", explanations)

    def test_material_changes_reject_active_resume_without_send(self):
        self.model.unknown_stage = "synthesis"
        first = self.analyze()
        self.assertEqual(first["status"], "outcome_unknown")
        before = len(self.model.requests)
        rows = load(self.run / "segments.json"); rows[0]["text"] += " changed"; save(self.run / "segments.json", rows)
        result = self.analyze()
        self.assertEqual(result["status"], "outcome_unknown")
        self.assertIn("changed", result["reason"])
        self.assertEqual(len(self.model.requests), before)
        self.assertIsNone(result["usage"]["tokens"]["api_actual"])
        self.assertEqual(result["usage"]["tokens"]["api_known_subtotal"]["total_tokens"], 120)
        self.assertEqual(result["usage"]["tokens"]["api_unknown_token_calls"], 1)

    def test_unknown_dispatch_survives_reentry_with_honest_subtotal(self):
        self.model.unknown_stage = "synthesis"
        first = self.analyze()
        self.assertEqual(first["status"], "outcome_unknown")
        self.assertNotIn(SECRET, first["reason"])
        self.assertEqual(len(first["usage"]["api_calls"]), 2)
        self.assertIsNone(first["usage"]["tokens"]["api_actual"])
        self.assertEqual(first["usage"]["tokens"]["api_known_subtotal"]["total_tokens"], 120)
        self.assertEqual(first["usage"]["tokens"]["api_unknown_token_calls"], 1)
        again = self.analyze()
        self.assertEqual(again["status"], "outcome_unknown")
        self.assertEqual(len(self.model.requests), 2)

    def test_saved_synthesis_restores_finish_without_rebuying(self):
        with patch("video_notes.usage.finish", side_effect=OSError("fixture finish crash")):
            first = self.analyze()
        self.assertEqual(first["status"], "failed")
        self.assertEqual(len(self.model.requests), 2)
        second = self.analyze()
        self.assertEqual(second["status"], "complete", second["reason"])
        self.assertEqual(len(self.model.requests), 2)

    def test_finished_draft_restores_note_after_save_failure(self):
        with patch("video_notes.notes.save_note", side_effect=OSError("fixture note save crash")):
            first = self.analyze()
        self.assertEqual(first["status"], "failed")
        self.assertEqual(load(self.run / "run.json")["status"], "finished")
        second = self.analyze()
        self.assertEqual(second["status"], "complete", second["reason"])
        self.assertEqual(len(self.model.requests), 2)
        self.assertEqual(second["note"]["revision"], 1)

    def test_visual_only_and_partial_preserve_honest_scope(self):
        for label, duration, speech, expected in (("visual", 20, False, "visual_only"), ("partial", 3600, True, "partial")):
            directory = self.root / label; self.prepare(directory, duration=duration, speech=speech)
            model = ModelFixture()
            with patch("video_notes.api.send", model):
                result = analyze_prepared(directory, provider=self.config, allow_asr=False, ledger=self.ledger)
            self.assertEqual(result["status"], expected, result["reason"])
            if expected == "partial":
                self.assertEqual(result["note"]["snapshot"]["run"]["remaining_range"], [1800, 3600])
            else:
                self.assertTrue(any("口头内容" in caveat for caveat in result["note"]["caveats"]))

    def test_text_only_model_rejected_and_native_finished_not_repurposed(self):
        result = analyze_prepared(self.run, provider=ProviderConfig(self.config.base_url, "text", api_key=SECRET, supports_images=False), allow_asr=False)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.model.requests, [])
        run = load(self.run / "run.json"); run["status"] = "finished"; save(self.run / "run.json", run)
        result = analyze_prepared(self.run, provider=self.config, allow_asr=False)
        self.assertEqual(result["status"], "failed")
        self.assertIn("native workflow", result["reason"])

    def test_no_counter_response_displays_unknown_and_includes_synthesis(self):
        self.model.usage = False
        result = self.analyze()
        self.assertEqual(result["status"], "complete", result["reason"])
        self.assertIsNone(result["usage"]["tokens"]["api_actual"])
        self.assertEqual(result["usage"]["tokens"]["api_unknown_token_calls"], 2)
        rendered = Path(result["outputs"]["md"]).read_text()
        self.assertIn("API 实际总 token", rendered)
        self.assertIn("API 未知计数调用", rendered)
        self.assertIn("笔记生成调用均计入", rendered)
        self.assertNotIn("API 实际总 token：0", rendered)

    def test_prepared_cli_rejects_ignored_scope_options_before_sending(self):
        with patch.dict(os.environ, {"FIXTURE_MODEL_KEY": SECRET}), patch("video_notes.api.send", self.model), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(["analyze-prepared", "--run", str(self.run), "--base-url", self.config.base_url,
                             "--model", self.config.model, "--api-key-env", "FIXTURE_MODEL_KEY", "--end", "10"])
        self.assertEqual(code, 1)
        self.assertEqual(self.model.requests, [])

    def test_cli_timeout_reaches_transport_and_invalid_values_do_not_dispatch(self):
        options = ["analyze-prepared", "--run", str(self.run), "--base-url", self.config.base_url,
                   "--model", self.config.model, "--api-key-env", "FIXTURE_MODEL_KEY", "--no-asr",
                   "--ledger", self.ledger]
        timeouts = []
        def transport(config, request):
            timeouts.append(config.timeout_seconds)
            return self.model(config, request)
        with patch.dict(os.environ, {"FIXTURE_MODEL_KEY": SECRET}), patch("video_notes.api.send", transport):
            for invalid in ("0", "601", "nan"):
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(cli.main(options + ["--timeout", invalid]), 1)
                self.assertEqual(timeouts, [])
                self.assertEqual(list((self.run / "api").glob("*/attempt.json")), [])
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(cli.main(options + ["--timeout", "300"]), 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "complete")
        self.assertEqual(timeouts, [300, 300])

    def test_focus_and_note_language_are_explicit_but_asr_separate(self):
        result = self.analyze(focus="Explain formulas", language="en")
        self.assertEqual(result["status"], "complete", result["reason"])
        for request in self.model.requests:
            material = json.loads(request["messages"][1]["content"][0]["text"])
            self.assertEqual(material["output_language"], "en")
            self.assertEqual(material["learning_focus"], "Explain formulas")
        self.assertEqual(load(self.run / "api/execution.json")["asr_language"], "auto")

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg/FFprobe required for synthetic acquisition")
    def test_local_source_through_loopback_http_details_cli_and_changed_finished_video(self):
        video = self.root / "course.mp4"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=5:duration=6",
                        "-c:v", "mpeg4", "-pix_fmt", "yuv420p", str(video)], check=True, capture_output=True)
        transcript = self.root / "transcript.json"
        save(transcript, [{"start": 0, "end": 6, "text": "A synthetic lecture fixture."}])
        model = ModelFixture(detail=[2])
        seen_auth = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                seen_auth.append((self.path, self.headers.get("Authorization")))
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                result = model(config, request)
                self.send_response(result.status_code); self.send_header("Content-Length", str(len(result.body))); self.end_headers(); self.wfile.write(result.body)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        config = ProviderConfig(f"http://127.0.0.1:{server.server_port}/v1", "fixture-vision", api_key=SECRET)
        directory = self.root / "actual-source"
        try:
            result = analyze(str(video), run_dir=directory, provider=config, transcript=transcript, allow_asr=False, strategy="uniform", ledger=self.ledger)
            self.assertEqual(result["status"], "complete", result["reason"])
            self.assertEqual(len(model.requests), 3)
            self.assertTrue(all(path == "/v1/chat/completions" and header == "Bearer " + SECRET for path, header in seen_auth))
            self.assertEqual([json.loads(row["messages"][1]["content"][0]["text"])["stage"] for row in model.requests], ["overview", "detail", "synthesis"])
            detail_body = model.requests[1]["messages"][1]["content"]
            self.assertEqual(len([block for block in detail_body if block["type"] == "image_url"]), 1)
            self.assertEqual(result["usage"]["tokens"]["api_actual"]["total_tokens"], 360)
            with patch.dict(os.environ, {"LOOPBACK_MODEL_KEY": SECRET}), contextlib.redirect_stdout(io.StringIO()) as output:
                code = cli.main(["analyze", "--video", str(video), "--out", str(directory), "--base-url", config.base_url,
                    "--model", config.model, "--api-key-env", "LOOPBACK_MODEL_KEY", "--transcript", str(transcript), "--no-asr",
                    "--strategy", "uniform", "--ledger", self.ledger, "--format", "md"])
            self.assertEqual(code, 0, output.getvalue())
            self.assertEqual(json.loads(output.getvalue())["status"], "complete")
            self.assertEqual(len(model.requests), 3)
            changed = analyze("https://www.youtube.com/watch?v=jNQXAC9IVRw", run_dir=directory, provider=config,
                transcript=transcript, allow_asr=False, strategy="uniform", ledger=self.ledger)
            self.assertEqual(changed["status"], "failed")
            self.assertIn("launch settings differ", changed["reason"])
            self.assertEqual(len(model.requests), 3)
            self.assertNotIn(SECRET, json.dumps(result))
        finally:
            server.shutdown(); server.server_close(); thread.join(2)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg/FFprobe required")
    def test_media_override_identity_is_frozen_without_storing_private_path(self):
        video = self.root / "override.mp4"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=red:size=80x60:rate=2:duration=20",
                        "-c:v", "mpeg4", "-pix_fmt", "yuv420p", str(video)], check=True, capture_output=True)
        with patch("video_notes.api.send", self.model):
            result = analyze_prepared(self.run, provider=self.config, media=video, allow_asr=False, ledger=self.ledger)
        self.assertEqual(result["status"], "complete", result["reason"])
        settings = load(self.run / "api/execution.json")
        self.assertNotIn("_media_path", settings)
        self.assertNotIn(str(video), json.dumps(settings))
