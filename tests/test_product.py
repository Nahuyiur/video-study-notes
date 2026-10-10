"""Synthetic local HTTP, credential isolation, crash safety and artifact acceptance."""
import copy
import hashlib
import http.client
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid
import zipfile

from video_notes import api, calls, engine, notes, product, worker
from video_notes.locking import file_lock
from video_notes.run import load
import test_notes as fixtures


def payload():
    return {"submission_id": str(uuid.uuid4()), "url": "https://www.youtube.com/watch?v=test1234567&share=tracking",
            "provider": {"base_url": "https://synthetic.example/v1", "model": "synthetic-vision", "timeout_seconds": 90},
            "api_key": "synthetic-test-credential", "start": 100, "end": 180, "allow_asr": False}


class ProductTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.launched = []
        self.product = product.Product(self.root / "data", executor=lambda *args: self.launched.append(args))
        self.addCleanup(self.product.close)

    def fixture(self):
        fixture = fixtures.StudyNotes("runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        notes.save_note(fixture.root, fixture.data)
        return fixture

    def assertNoCredentialOnDisk(self, key):
        for path in self.product.root.rglob("*"):
            if path.is_file():
                self.assertNotIn(key.encode(), path.read_bytes(), str(path))

    def test_submit_idempotent_even_busy_and_secret_separate(self):
        body = payload()
        view, created = self.product.submit(body)
        self.assertTrue(created)
        self.assertEqual(len(self.launched), 1)
        self.assertEqual(self.launched[0][2], body["api_key"])
        with patch.object(self.product, "worker_busy", return_value=True):
            repeated, created = self.product.submit(body)
            self.assertFalse(created)
            self.assertEqual(view["id"], repeated["id"])
            with self.assertRaisesRegex(product.ProductError, "正在运行"):
                self.product.submit(payload())
        changed = copy.deepcopy(body)
        changed["focus"] = "changed"
        with self.assertRaisesRegex(product.ProductError, "其他配置"):
            self.product.submit(changed)
        self.assertNoCredentialOnDisk(body["api_key"])
        self.assertNotIn("provider", json.dumps(view))
        self.assertNotIn(str(self.root), json.dumps(view))
        self.assertNotIn("share", self.product.record(view["id"])["spec"]["url"])

    def test_boundaries_rejected_before_any_launch(self):
        changes = [{"url": "file:///private/video.mp4"}, {"url": "https://127.0.0.1/private"},
                   {"start": float("nan")}, {"end": 50}, {"api_key": ""},
                   {"provider": {"base_url": "https://u:password@synthetic.example/v1", "model": "test"}},
                   {"budget": {"max_calls": 99}}, {"budget": {"output_tokens_per_call": 500, "max_reserved_output_tokens": 999}},
                   {"focus": "synthetic-test-credential"}, {"media": "/private/file"}, {"strategy": "unknown"}]
        for change in changes:
            body = payload()
            body.update(change)
            with self.subTest(change=change), self.assertRaises(product.ProductError):
                self.product.submit(body)
        self.assertEqual(self.launched, [])
        self.assertEqual(self.product.history(), [])
        message = product.safe_failure("failed", "RedNote login_required synthetic-access-token")
        self.assertIn("已停止", message)
        self.assertNotIn("synthetic-access-token", message)

    def test_hybrid_is_default_and_existing_sampling_strategies_remain_available(self):
        body = payload()
        _, spec, _ = product.submission(body)
        self.assertEqual(spec["strategy"], "hybrid")
        self.assertEqual(spec["budget"]["max_images"], 16)
        self.assertEqual(spec["budget"]["max_calls"], 3)
        for strategy in ("hybrid", "slides", "uniform"):
            with self.subTest(strategy=strategy):
                _, spec, _ = product.submission({**body, "strategy": strategy})
                self.assertEqual(spec["strategy"], strategy)

    def test_rednote_access_link_is_ephemeral_and_resume_matches_note(self):
        note = "0123456789abcdef01234567"
        public = "https://www.xiaohongshu.com/explore/" + note
        body = payload()
        body["url"] = public + "?xsec_token=synthetic-access-token&share_id=tracking"
        view, _ = self.product.submit(body)
        self.assertEqual(self.launched[0][3], body["url"])
        self.assertEqual(self.product.record(view["id"])["spec"]["url"], public)
        self.assertEqual(view["source_url"], public)
        self.assertNoCredentialOnDisk("synthetic-access-token")
        fresh = public + "?xsec_token=fresh-synthetic-token"
        self.product.resume(view["id"], {"api_key": "new-synthetic-key", "url": fresh})
        self.assertEqual(self.launched[-1][3], fresh)
        self.assertNoCredentialOnDisk("fresh-synthetic-token")
        self.assertNoCredentialOnDisk("new-synthetic-key")
        before = len(self.launched)
        with self.assertRaisesRegex(product.ProductError, "同一篇"):
            self.product.resume(view["id"], {"api_key": "new-synthetic-key", "url": public.replace(note, "f" * 24)})
        self.assertEqual(len(self.launched), before)
        body["submission_id"] = str(uuid.uuid4())
        body["url"] = public + "?xsec_token=" + body["api_key"]
        with self.assertRaisesRegex(product.ProductError, "密钥栏"):
            self.product.submit(body)

    def test_rednote_short_links_and_browser_paths(self):
        self.assertEqual(product.source_url("https://xhslink.cn/o/synthetic?share=tracking"), "https://xhslink.cn/o/synthetic")
        self.assertEqual(product.source_url("https://www.xiaohongshu.com/discovery/item/0123456789abcdef01234567?xsec_token=synthetic"),
                         "https://www.xiaohongshu.com/explore/0123456789abcdef01234567")
        for url in ("https://www.xiaohongshu.com/user/profile/private", "https://xhslink.cn@outside.example/o/link",
                    "https://www.xiaohongshu.com/explore/too-short", "https://xhslink.cn:444/o/link"):
            with self.subTest(url=url), self.assertRaises(product.ProductError):
                product.source_url(url)
        body = payload()
        body["rednote_skill"] = "/operator-only/skill"
        with self.assertRaises(product.ProductError):
            self.product.submit(body)

    def test_independent_locks_and_restart_never_dispatch(self):
        with self.assertRaises(product.ProductError):
            product.Product(self.product.root)
        view, _ = self.product.submit(payload())
        with file_lock(self.product.root / ".execution.lock", blocking=False):
            self.assertTrue(self.product.worker_busy())
            with self.assertRaises(product.ProductError):
                self.product.submit(payload())
        self.product.close()
        restarted = product.Product(self.product.root, executor=lambda *args: self.fail("Restart must not dispatch"))
        self.addCleanup(restarted.close)
        view = restarted.view(view["id"])
        self.assertEqual(view["status"], "interrupted")
        self.assertTrue(view["can_resume"])
        self.assertEqual(len(restarted.history()), 1)

    def test_unknown_send_blocks_resume_but_durable_received_can_recover(self):
        view, _ = self.product.submit(payload())
        folder = self.product.folder(view["id"])
        directory = folder / "run"
        calls.durable_save(directory / "run.json", {"api_pending": "call-test"})
        self.assertEqual(self.product.view(view["id"])["status"], "outcome_unknown")
        self.assertFalse(self.product.view(view["id"])["can_resume"])
        with self.assertRaises(product.ProductError):
            self.product.resume(view["id"], {"api_key": "new-synthetic-key"})
        marker = {"call_id": "call-test", "request_hash": "test-hash", "status": "dispatching"}
        calls.durable_save(directory / "api/test-hash/attempt.json", marker)
        calls.durable_save(directory / "api/test-hash/response.json", {"call_id": "call-test", "request_hash": "test-hash", "http_status": 200, "body": "{}"})
        self.assertFalse(product._unknown(directory))
        self.assertTrue(self.product.view(view["id"])["can_resume"])
        self.product.resume(view["id"], {"api_key": "new-synthetic-key"})
        self.assertEqual(len(self.launched), 2)
        self.assertNoCredentialOnDisk("new-synthetic-key")

    def test_old_interrupted_job_does_not_look_running_for_another_job(self):
        first, _ = self.product.submit(payload())
        second, _ = self.product.submit(payload())
        row = self.product.record(second["id"])
        calls.durable_save(self.product.root / ".execution-active.json", {"job": second["id"], "attempt": row["attempt"]})
        with patch.object(self.product, "worker_busy", return_value=True):
            self.assertEqual(self.product.view(first["id"])["status"], "interrupted")
            self.assertEqual(self.product.view(second["id"])["status"], "running")

    def test_child_execution_lock_survives_supervisor_restart(self):
        view, _ = self.product.submit(payload())
        row = self.product.record(view["id"])
        calls.durable_save(self.product.root / ".execution-active.json", {"job": view["id"], "attempt": row["attempt"]})
        code = "from video_notes.locking import file_lock; import sys,time; from pathlib import Path\nwith file_lock(Path(sys.argv[1])/'.execution.lock',blocking=False):\n print('ready',flush=True)\n time.sleep(10)\n"
        child = subprocess.Popen([sys.executable, "-c", code, str(self.product.root)],
                                  cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        def cleanup():
            if child.poll() is None:
                child.terminate()
            child.communicate(timeout=5)
        self.addCleanup(cleanup)
        self.assertEqual(child.stdout.readline().strip(), b"ready")
        self.product.close()
        restarted = product.Product(self.product.root, executor=lambda *a: self.fail("Active child must block new dispatch"))
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.view(view["id"])["status"], "running")
        with self.assertRaises(product.ProductError):
            restarted.submit(payload())
        cleanup()
        self.assertEqual(restarted.view(view["id"])["status"], "interrupted")

    def test_import_only_frozen_note_assets_and_fixed_downloads(self):
        fixture = self.fixture()
        calls.durable_save(fixture.root / "api/private.json", {"private_receipt": True})
        jid = self.product.import_run(fixture.root)
        view = self.product.view(jid)
        self.assertEqual(view["status"], "completed")
        self.assertFalse(view["can_resume"])
        self.assertEqual(view["scope"]["processed_range"], [100, 180])
        self.assertIsNone(view["usage"]["cost_usd"])
        self.assertEqual(set(view["artifacts"]), {"html", "markdown", "markdown_zip", "markdown_text"})
        directory = self.product.folder(jid) / "run"
        for private in ("api", "source.json", "run.json", "segments.json", "usage.json"):
            self.assertFalse((directory / private).exists())
        self.assertEqual(notes.load_note(directory)["content_hash"], notes.load_note(fixture.root)["content_hash"])
        archive, _ = self.product.artifact(jid, "markdown-zip")
        with zipfile.ZipFile(archive) as zipped:
            names = zipped.namelist()
            self.assertTrue(any(name.endswith(".jpg") for name in names))
            md = zipped.read(next(name for name in names if name.endswith(".md"))).decode()
            self.assertTrue(any(name in md for name in names if name.endswith(".jpg")))
            self.assertTrue(all(not name.startswith("/") and ".." not in name for name in names))
        text, _ = self.product.artifact(jid, "markdown-text")
        self.assertNotIn("![", text.read_text())
        with self.assertRaises(product.ProductError):
            self.product.artifact(jid, "../job.json")

    def test_public_output_hashes_bind_downloads_and_zip_only_referenced_assets(self):
        fixture = self.fixture()
        jid = self.product.import_run(fixture.root)
        directory = self.product.folder(jid) / "run"
        note = notes.load_note(directory)
        assets = directory / "exports" / f"note-v{note['revision']:03d}-assets"
        (assets / "unexpected.jpg").write_bytes(b"private unrelated image")
        (assets / "private.txt").write_text("private unrelated data")
        product.make_artifacts(directory)
        manifest = load(directory / "public-manifest.json")
        self.assertEqual(manifest["note_content_hash"], note["content_hash"])
        self.assertEqual(set(manifest["files"]), {"note.html", "note.md", "note-markdown.zip", "note-text.md"})
        archive, _ = self.product.artifact(jid, "markdown-zip")
        with zipfile.ZipFile(archive) as zipped:
            names = zipped.namelist()
            self.assertEqual(len(names), 2)
            self.assertFalse(any("unexpected" in name or "private.txt" in name for name in names))
        for name in ("html", "markdown-zip"):
            path, _ = self.product.artifact(jid, name)
            original = path.read_bytes()
            path.write_bytes(original + b"tampered")
            with self.subTest(name=name), self.assertRaisesRegex(product.ProductError, "校验"):
                self.product.artifact(jid, name)
            path.write_bytes(original)
        manifest["note_content_hash"] = "0" * 64
        calls.durable_save(directory / "public-manifest.json", manifest)
        with self.assertRaises(product.ProductError):
            self.product.artifact(jid, "html")

    def test_usage_unknown_totals_preserve_known_subtotals_and_js_readiness(self):
        view = product._usage({"api_calls": [{"outcome": "known"}, {"outcome": "unknown"}],
            "tokens": {"api_actual": None, "api_known_subtotal": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
                       "api_unknown_token_calls": 1},
            "cost": {"api_usd": None, "api_known_subtotal_usd": "0.001", "api_unknown_cost_calls": 1}})
        self.assertIsNone(view["total_tokens"])
        self.assertIsNone(view["cost_usd"])
        self.assertEqual(view["known_total_tokens"], 120)
        self.assertEqual(view["known_input_tokens"], 100)
        self.assertEqual(view["known_output_tokens"], 20)
        self.assertEqual(view["known_cost_subtotal_usd"], "0.001")
        self.assertEqual(view["unknown_token_calls"], 1)
        self.assertEqual(view["unknown_cost_calls"], 1)
        with patch("video_notes.sources.youtube._runtime", return_value=None):
            ready = product.readiness()
        runtime = next(row for row in ready["items"] if row["name"] == "YouTube JavaScript")
        self.assertFalse(runtime["available"])
        self.assertFalse(runtime["required"])
        self.assertIn("Node 22", runtime["message"])
        self.assertIn("Node 22", product.safe_failure("failed", "YouTube needs Deno >= 2.3 or Node >= 22"))

    def test_symlink_and_tampered_note_refuse_download(self):
        fixture = self.fixture()
        jid = self.product.import_run(fixture.root)
        folder = self.product.folder(jid)
        note_path = next((folder / "run/notes").glob("note-v*.json"))
        value = load(note_path)
        value["title"] = "tampered"
        calls.durable_save(note_path, value)
        with self.assertRaises(product.ProductError):
            self.product.artifact(jid, "html")
        self.assertEqual(self.product.view(jid)["status"], "failed")

    def test_artifacts_reject_symlinked_parent(self):
        fixture = self.fixture()
        jid = self.product.import_run(fixture.root)
        export = self.product.folder(jid) / "run/public"
        moved = self.root / "moved-export"
        export.rename(moved)
        try:
            export.symlink_to(moved, target_is_directory=True)
        except (OSError, NotImplementedError):
            moved.rename(export)
            self.skipTest("Platform does not allow symlink creation for this user")
        with self.assertRaises(product.ProductError):
            self.product.artifact(jid, "html")

    def test_worker_fake_engine_success_and_raw_failure_redaction(self):
        view, _ = self.product.submit(payload())
        row = self.product.record(view["id"])
        fixture = self.fixture()
        def fake_analyze(*args, **kwargs):
            self.assertEqual(kwargs["provider"].credential(), "synthetic-test-credential")
            self.assertEqual(kwargs["budget"].max_calls, 3)
            self.assertEqual(kwargs["ledger"], self.product.root / "usage.jsonl")
            shutil.copytree(fixture.root, kwargs["run_dir"])
            return {"status": "partial", "run_dir": "/private/path"}
        self.assertEqual(worker.execute(self.product.root, view["id"], row["attempt"], "synthetic-test-credential", analyze=fake_analyze), 0)
        self.assertEqual(self.product.view(view["id"])["status"], "completed")
        self.assertNoCredentialOnDisk("synthetic-test-credential")
        second, _ = self.product.submit(payload())
        row = self.product.record(second["id"])
        worker.execute(self.product.root, second["id"], row["attempt"], "synthetic-test-credential",
                       analyze=lambda *a, **k: {"status": "failed", "reason": "synthetic-test-credential /private/path arbitrary response"})
        view = self.product.view(second["id"])
        self.assertNotIn("/private", json.dumps(view))
        self.assertNotIn("synthetic-test-credential", json.dumps(view))
        self.assertNoCredentialOnDisk("synthetic-test-credential")

    def test_actual_child_uses_stdin_and_cached_note_without_model_or_platform_call(self):
        body = payload()
        view, _ = self.product.submit(body)
        row = self.product.record(view["id"])
        fixture = self.fixture()
        directory = self.product.folder(view["id"]) / "run"
        shutil.copytree(fixture.root, directory)
        config = api.ProviderConfig(**row["spec"]["provider"], api_key=body["api_key"])
        budget = api.ApiBudget()
        settings = engine._settings(config, budget, focus="", language="zh", allow_asr=False,
                    strategy=row["spec"]["strategy"], asr_model="small", asr_language="auto", media=None, prices=None)
        calls.durable_save(directory / "api/execution.json", settings)
        calls.durable_save(directory / "api/launch.json", {"video_sha256": hashlib.sha256(row["spec"]["url"].encode()).hexdigest(),
            "start": 100, "end": 180, "preset": "economy", "page": None, "source_json": None, "transcript": None})
        self.product._spawn(view["id"], row["attempt"], body["api_key"])
        child = self.product.children[row["attempt"]]
        child.wait(timeout=10)
        self.assertNotIn(body["api_key"], " ".join(child.args))
        self.assertEqual(child.returncode, 0)
        self.assertEqual(self.product.view(view["id"])["status"], "completed")
        self.assertNoCredentialOnDisk(body["api_key"])
        self.assertFalse(any((directory / "api").glob("*/attempt.json")))


class ProductHTTPTests(unittest.TestCase):
    fixture = ProductTests.fixture
    def setUp(self):
        ProductTests.setUp(self)
        self.server = product.LocalServer(self.product, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.port = self.server.server_address[1]

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, path, *, method="GET", body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(conn.close)
        hdr = {"X-Video-Notes-Session": self.product.session}
        if method == "POST":
            hdr.update({"Content-Type": "application/json", "Origin": self.server.url})
        hdr.update(headers or {})
        encoded = json.dumps(body).encode() if body is not None else None
        conn.request(method, path, body=encoded, headers=hdr)
        response = conn.getresponse()
        return response.status, dict(response.headers), response.read()

    def test_http_session_submission_history_and_authenticated_download(self):
        status, headers, _ = self.request("/")
        self.assertEqual(status, 200)
        policy = headers["Content-Security-Policy"]
        self.assertIn("style-src 'self' 'unsafe-inline'", policy)
        self.assertIn("img-src data:", policy)
        self.assertIn("script-src 'self';", policy)
        self.assertNotIn("script-src 'self' 'unsafe-inline'", policy)
        status, headers, raw = self.request("/api/session")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["csrf_token"], self.product.session)
        self.assertEqual(headers["Cache-Control"], "no-store")
        body = payload()
        status, _, raw = self.request("/api/jobs", method="POST", body=body)
        self.assertEqual(status, 201)
        self.assertNotIn(body["api_key"].encode(), raw)
        status, _, repeated = self.request("/api/jobs", method="POST", body=body)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["id"], json.loads(repeated)["id"])
        status, _, raw = self.request("/api/jobs")
        self.assertEqual(len(json.loads(raw)["jobs"]), 1)
        jid = self.product.import_run(self.fixture().root)
        status, headers, html = self.request(f"/api/jobs/{jid}/artifacts/html")
        self.assertEqual(status, 200)
        self.assertIn("sandbox", headers["Content-Security-Policy"])
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertIn(b"data:image/jpeg;base64,", html)

    def test_http_host_origin_session_and_bounds(self):
        cases = [("/api/session", "GET", {"Host": "evil.example"}, None, 403),
                 ("/api/session", "GET", {"Origin": "https://evil.example"}, None, 403),
                 ("/api/jobs", "GET", {"X-Video-Notes-Session": "wrong"}, None, 403),
                 ("/api/jobs", "POST", {"Origin": "http://localhost:1"}, payload(), 403),
                 ("/api/jobs", "POST", {"Content-Type": "text/plain"}, payload(), 415),
                 ("/api/jobs", "POST", {}, {"large": "x" * 25000}, 413),
                 ("/api/jobs", "POST", {"Transfer-Encoding": "chunked"}, payload(), 400),
                 ("/api/jobs?token=secret", "GET", {}, None, 404),
                 ("/api/jobs/../../private", "GET", {}, None, 404)]
        for path, method, headers, body, expected in cases:
            with self.subTest(path=path, headers=headers):
                status, _, raw = self.request(path, method=method, body=body, headers=headers)
                self.assertEqual(status, expected)
                self.assertNotIn(b"synthetic-test-credential", raw)
        self.assertEqual(self.launched, [])


if __name__ == "__main__":
    unittest.main()
