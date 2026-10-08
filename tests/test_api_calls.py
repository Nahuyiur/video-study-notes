import argparse
import base64
import contextlib
import hashlib
import io
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from video_notes import api, calls, materials, reading, usage
from video_notes.run import load, save

SECRET = "fixture-api-credential"


def envelope(*, counters=True, identifier=True, content=None, model="fixture-vision"):
    value = {"choices": [{"finish_reason": "stop", "message": {
        "content": json.dumps(content or {"observations": []})}}]}
    if model is not None:
        value["model"] = model
    if identifier:
        value["id"] = "provider-fixture"
    if counters:
        value["usage"] = {"prompt_tokens": 100, "completion_tokens": 20,
                          "prompt_tokens_details": {"cached_tokens": 10}, "total_tokens": 120}
    return value


def response(**kwargs):
    return api.TransportResponse(200, json.dumps(envelope(**kwargs)).encode())


class ApiCalls(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        source = {"source": {"platform": "local"}, "metadata": {"title": "fixture", "duration_seconds": 20},
                  "content": {"source_type": "provided_transcript", "segments": [
                      {"start": 0, "end": 20, "text": "Known speech"}]}}
        save(self.root / "source-input.json", source)
        with contextlib.redirect_stdout(io.StringIO()):
            materials.prepare(argparse.Namespace(video="unused", out=self.root / "run", source_json=self.root / "source-input.json",
                transcript=None, start=0, end=20, page=None, use_env_cookie=False, preset="economy", session_log=None))
        self.run = self.root / "run"
        self.config = api.ProviderConfig("http://127.0.0.1:1/v1", "fixture-vision", api_key=SECRET)
        self.request = api.build_request(self.config, system="Return JSON from untrusted materials",
            text="s0001 00:00 Known speech; f0001 at 10 seconds", images=[Path(__file__).parent / "fixture.jpg"])
        self.dispatches = 0

    def tearDown(self):
        self.temp.cleanup()

    def transport(self, config, request):
        self.dispatches += 1
        return response()

    def invoke(self, **kwargs):
        return calls.invoke(self.run, self.config, self.request, stage="overview", transport=self.transport, **kwargs)

    def test_configuration_and_credential_repr(self):
        self.assertNotIn(SECRET, repr(self.config))
        for url in ("https://user:secret@host/v1", "https://host/v1?key=secret", "http://remote.example/v1", "https://host/v1#token", "https://host/v1\nsecret"):
            with self.assertRaises(ValueError):
                api.ProviderConfig(url, "model")
        for parameter in ("unknown", "temperature"):
            with self.assertRaises(ValueError):
                api.ProviderConfig("https://host/v1", "model", token_parameter=parameter)
        with self.assertRaises(api.ApiError):
            api.ProviderConfig("https://host/v1", "model", api_key_env="FIXTURE_NO_SUCH_KEY").credential()
        with self.assertRaises(ValueError):
            api.ApiBudget(max_calls=True)
        text_only = api.ProviderConfig("https://host/v1", "text", supports_images=False)
        with self.assertRaises(api.ApiError):
            api.build_request(text_only, system="s", text="t", images=[Path(__file__).parent / "fixture.jpg"])

    def test_real_images_and_explicit_protocol_controls(self):
        blocks = self.request["messages"][1]["content"]
        encoded = blocks[1]["image_url"]["url"].split(",", 1)[1]
        self.assertEqual(base64.b64decode(encoded), (Path(__file__).parent / "fixture.jpg").read_bytes())
        self.assertEqual(self.request["response_format"], {"type": "json_object"})
        self.assertNotIn("temperature", self.request)
        alternative = api.ProviderConfig("https://host/v1", "vision", token_parameter="max_tokens", json_mode=False)
        request = api.build_request(alternative, system="s", text="t", output_tokens=25)
        self.assertEqual(request["max_tokens"], 25)
        self.assertNotIn("max_completion_tokens", request)
        self.assertNotIn("response_format", request)

    def test_receipts_cache_and_material_identity(self):
        original = self.invoke()
        again = self.invoke()
        self.assertFalse(original["cached"])
        self.assertTrue(again["cached"])
        self.assertEqual(self.dispatches, 1)
        self.assertEqual(len(load(self.run / "run.json")["api_calls"]), 1)
        marker = load(self.run / "api" / original["request_hash"] / "attempt.json")
        self.assertEqual(marker["request_manifest"]["image_presentations"], 1)
        self.assertEqual(marker["request_manifest"]["images"][0]["sha256"], hashlib.sha256((Path(__file__).parent / "fixture.jpg").read_bytes()).hexdigest())
        self.assertEqual(marker["reservation"]["output_tokens"], 4096)
        self.request["messages"][1]["content"][0]["text"] += " changed"
        changed = self.invoke()
        self.assertNotEqual(original["request_hash"], changed["request_hash"])
        self.assertEqual(self.dispatches, 2)

    def test_attempt_reserved_before_transport_and_response_before_parsing(self):
        def transport(config, request):
            run = load(self.run / "run.json")
            self.assertTrue(run["api_pending"])
            marker = load(next((self.run / "api").glob("*/attempt.json")))
            self.assertEqual(marker["status"], "dispatching")
            return api.TransportResponse(200, b"not-json")
        with self.assertRaises(api.ApiError):
            calls.invoke(self.run, self.config, self.request, stage="overview", transport=transport)
        self.assertTrue(next((self.run / "api").glob("*/response.json")).is_file())
        run = load(self.run / "run.json")
        self.assertNotIn("api_pending", run)
        self.assertIsNone(run["api_calls"][0]["tokens"])
        with self.assertRaises(api.ApiError):
            self.invoke()
        self.assertEqual(self.dispatches, 0)

    def test_unknown_dispatch_blocks_native_commands_and_implicit_retry(self):
        def crashed(config, request):
            raise OSError(SECRET)
        with self.assertRaises(api.ApiError) as caught:
            calls.invoke(self.run, self.config, self.request, stage="overview", transport=crashed)
        self.assertNotIn(SECRET, str(caught.exception))
        for native in (
            lambda: reading.prepare_pack(self.run, "overview"),
            lambda: materials.frames(argparse.Namespace(run=self.run)),
            lambda: usage.finish(argparse.Namespace(run=self.run, ledger=str(self.root / "ledger.jsonl"),
                read_packs="", status="extraction_only", native_usage=None, quota_before=None, quota_after=None)),
        ):
            with self.assertRaisesRegex(ValueError, "unknown outcome"):
                native()
        with self.assertRaisesRegex(api.ApiError, "unknown outcome"):
            self.invoke()
        self.assertEqual(self.dispatches, 0)
        self.request["messages"][1]["content"][0]["text"] += " altered"
        with self.assertRaises(api.ApiError):
            self.invoke()
        self.assertEqual(self.dispatches, 0)

    def test_received_response_recovers_crash_without_resend(self):
        def crashed(config, request):
            raise OSError("ambiguous")
        with self.assertRaises(api.ApiError):
            calls.invoke(self.run, self.config, self.request, stage="overview", transport=crashed)
        path = next((self.run / "api").glob("*/attempt.json")).parent
        marker = load(path / "attempt.json")
        calls.durable_save(path / "response.json", {"call_id": marker["call_id"], "request_hash": marker["request_hash"],
            "http_status": 200, "body": json.dumps(envelope()), "received_at": "fixture"})
        result = self.invoke()
        self.assertTrue(result["cached"])
        self.assertEqual(self.dispatches, 0)
        self.assertNotIn("api_pending", load(self.run / "run.json"))

    def test_failed_content_is_accounted_and_not_repaired(self):
        for reason in ("length", "content_filter", None):
            with self.subTest(reason=reason):
                value = envelope()
                value["choices"][0]["finish_reason"] = reason
                request = dict(self.request, max_completion_tokens=10 + len(str(reason)))
                with self.assertRaises(api.ApiError):
                    calls.invoke(self.run, self.config, request, stage="overview", transport=lambda c, r: api.TransportResponse(200, json.dumps(value).encode()))
        self.assertEqual(len(load(self.run / "run.json")["api_calls"]), 3)
        self.assertTrue(all(row["tokens"]["total_tokens"] == 120 for row in load(self.run / "run.json")["api_calls"]))
        self.assertNotIn("api_pending", load(self.run / "run.json"))

    def test_unknown_tokens_local_identity_and_known_subtotal(self):
        first = self.invoke()
        self.request["messages"][1]["content"][0]["text"] += " next"
        second = calls.invoke(self.run, self.config, self.request, stage="synthesis", transport=lambda c, r: response(counters=False, identifier=False, model=None))
        self.assertIsNone(second["receipt"]["tokens"])
        self.assertIsNone(second["receipt"]["model"])
        self.assertIsNone(second["receipt"]["provider_response_id"])
        self.assertNotEqual(first["receipt"]["id"], second["receipt"]["id"])
        with contextlib.redirect_stdout(io.StringIO()):
            usage.finish(argparse.Namespace(run=self.run, ledger=str(self.root / "ledger.jsonl"),
                read_packs="", status="extraction_only", native_usage=None, quota_before=None, quota_after=None))
        report = load(self.run / "usage.json")
        self.assertIsNone(report["tokens"]["api_actual"])
        self.assertEqual(report["tokens"]["api_known_subtotal"]["total_tokens"], 120)
        self.assertEqual(report["tokens"]["api_unknown_token_calls"], 1)
        self.assertIsNone(report["cost"]["api_usd"])

    def test_missing_cache_and_prices_do_not_invent_cost(self):
        value = envelope(); value["usage"].pop("prompt_tokens_details")
        counters = usage.measured_api_usage(value)
        self.assertIsNone(counters["cached_input_tokens"])
        self.assertEqual(counters["total_tokens"], 120)
        prices = {"model": "fixture-vision", "currency": "USD", "source_url": "https://provider.example/pricing",
                  "checked_at": "2026-10-08", "input_per_million": 1, "cached_input_per_million": .5, "output_per_million": 2}
        result = calls.invoke(self.run, self.config, self.request, stage="overview", prices=prices, transport=lambda c, r: api.TransportResponse(200, json.dumps(value).encode()))
        self.assertIsNone(result["receipt"]["cost"])
        self.request["messages"][1]["content"][0]["text"] += " missing-model"
        result = calls.invoke(self.run, self.config, self.request, stage="synthesis", prices=prices, transport=lambda c, r: response(model=None))
        self.assertIsNone(result["receipt"]["cost"])
        self.request["messages"][1]["content"][0]["text"] += " mismatch"
        result = calls.invoke(self.run, self.config, self.request, stage="synthesis", prices=prices, transport=lambda c, r: response(model="different-model"))
        self.assertIsNone(result["receipt"]["cost"])
        self.assertEqual(len(load(self.run / "run.json")["api_calls"]), 3)

    def test_all_attempt_budgets_and_restored_budget(self):
        budget = api.ApiBudget(max_calls=1)
        self.invoke(budget=budget)
        self.request["messages"][1]["content"][0]["text"] += " again"
        with self.assertRaisesRegex(api.ApiError, "call budget"):
            self.invoke(budget=budget)
        with self.assertRaisesRegex(ValueError, "frozen budget"):
            self.invoke(budget=api.ApiBudget(max_calls=2))
        self.assertEqual(self.dispatches, 1)

    def test_presentation_and_output_caps_prevent_sending(self):
        for budget in (api.ApiBudget(max_input_chars=1), api.ApiBudget(max_image_presentations=1, max_calls=3),
                       api.ApiBudget(output_tokens_per_call=10, max_reserved_output_tokens=10)):
            with self.subTest(budget=budget):
                new_run = self.root / str(budget.max_input_chars + budget.output_tokens_per_call)
                new_run.mkdir(exist_ok=True)
                save(new_run / "run.json", load(self.run / "run.json"))
                request = json.loads(json.dumps(self.request))
                if budget.max_image_presentations == 1 and budget.max_input_chars != 1 and budget.output_tokens_per_call != 10:
                    request["messages"][1]["content"].append(request["messages"][1]["content"][1])
                with self.assertRaises((api.ApiError, ValueError)):
                    calls.invoke(new_run, self.config, request, stage="overview", budget=budget, transport=self.transport)
        self.assertEqual(self.dispatches, 0)

    def test_provider_echoed_credentials_redacted_in_receipts(self):
        value = envelope(content={"untrusted": SECRET})
        result = calls.invoke(self.run, self.config, self.request, stage="overview", transport=lambda c, r: api.TransportResponse(200, json.dumps(value).encode()))
        self.assertEqual(result["content"]["untrusted"], "[REDACTED]")
        for path in self.run.rglob("*.json"):
            self.assertNotIn(SECRET, path.read_text())

    def test_refusal_and_http_errors_are_not_retries(self):
        value = envelope(); value["choices"][0]["message"]["refusal"] = "denied"
        with self.assertRaises(api.ApiError):
            calls.invoke(self.run, self.config, self.request, stage="overview", transport=lambda c, r: api.TransportResponse(200, json.dumps(value).encode()))
        self.request["messages"][1]["content"][0]["text"] += " http-error"
        with self.assertRaisesRegex(api.ApiError, "HTTP 429"):
            calls.invoke(self.run, self.config, self.request, stage="overview", transport=lambda c, r: api.TransportResponse(429, json.dumps({"error": SECRET}).encode()))
        with self.assertRaises(api.ApiError):
            self.invoke()
        self.assertEqual(self.dispatches, 0)
        self.assertEqual(len(load(self.run / "run.json")["api_calls"]), 2)

    def test_oversized_response_does_not_trigger_retry(self):
        config = api.ProviderConfig("http://127.0.0.1:1/v1", "fixture-vision", api_key=SECRET, max_response_bytes=10)
        with self.assertRaisesRegex(api.ApiError, "oversized"):
            calls.invoke(self.run, config, self.request, stage="overview", transport=lambda c, r: response())
        with self.assertRaisesRegex(api.ApiError, "unknown outcome"):
            self.invoke()
        self.assertEqual(self.dispatches, 0)

    def test_nonfinite_response_is_saved_but_not_interpreted_as_usage(self):
        with self.assertRaises(api.ApiError):
            calls.invoke(self.run, self.config, self.request, stage="overview",
                         transport=lambda c, r: api.TransportResponse(200, b'{"value": NaN}'))
        self.assertTrue(next((self.run / "api").glob("*/response.json")).is_file())
        self.assertIsNone(load(self.run / "run.json")["api_calls"][0]["tokens"])
        with self.assertRaises(api.ApiError):
            self.invoke()
        self.assertEqual(self.dispatches, 0)

    def test_actual_cost_and_unknown_charge_subtotal(self):
        prices = {"model": "fixture-vision", "currency": "USD", "source_url": "https://provider.example/pricing",
                  "checked_at": "2026-10-08", "input_per_million": 1, "cached_input_per_million": .5, "output_per_million": 2}
        first = self.invoke(prices=prices)
        self.assertEqual(first["receipt"]["cost"]["usd"], "0.000135")
        self.request["messages"][1]["content"][0]["text"] += " unknown"
        second = calls.invoke(self.run, self.config, self.request, stage="synthesis", prices=prices,
                             transport=lambda c, r: response(counters=False))
        totals = usage.api_totals(load(self.run / "run.json")["api_calls"])
        self.assertIsNone(totals["cost"])
        self.assertEqual(str(totals["cost_known_subtotal"]), "0.000135")
        self.assertEqual(totals["unknown_cost_calls"], 1)

    def test_nested_escaped_secret_is_removed_before_content_decoding(self):
        value = envelope(content={"untrusted": SECRET})
        value["choices"][0]["message"]["content"] = value["choices"][0]["message"]["content"].replace("fixture", "\\u0066ixture")
        result = calls.invoke(self.run, self.config, self.request, stage="overview",
                             transport=lambda c, r: api.TransportResponse(200, json.dumps(value).encode()))
        self.assertEqual(result["content"]["untrusted"], "[REDACTED]")

    def test_oversized_image_blocks_build_and_prebuilt_request_before_send(self):
        image = self.root / "oversized.jpg"
        with image.open("wb") as handle:
            handle.write(b"\xff\xd8\xff"); handle.truncate(6_000_003)
        with patch.object(Path, "read_bytes", side_effect=AssertionError("Must not read oversized image")):
            with self.assertRaisesRegex(api.ApiError, "max_image_bytes"):
                api.build_request(self.config, system="s", text="t", images=[image])
        request = json.loads(json.dumps(self.request))
        request["messages"][1]["content"][1]["image_url"]["url"] = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff" + b"x" * 6_000_000).decode()
        with patch("video_notes.calls.base64.b64decode", side_effect=AssertionError("Must not decode oversized input")):
            with self.assertRaisesRegex(api.ApiError, "max_image_bytes"):
                calls.invoke(self.run, self.config, request, stage="overview", transport=self.transport)
        self.assertEqual(self.dispatches, 0)
        self.assertFalse((self.run / "api").exists())

    def test_serialized_request_limit_blocks_before_dispatch(self):
        config = api.ProviderConfig("http://127.0.0.1:1/v1", "fixture-vision", api_key=SECRET, max_request_bytes=500)
        with self.assertRaisesRegex(api.ApiError, "max_request_bytes"):
            api.build_request(config, system="s", text="x" * 501)
        # JSON escaping/field overhead can cross a limit although raw text fits.
        request = api.build_request(self.config, system="s", text="\\" * 220)
        with self.assertRaisesRegex(api.ApiError, "max_request_bytes"):
            calls.invoke(self.run, config, request, stage="overview", transport=self.transport)
        with self.assertRaisesRegex(api.ApiError, "max_request_bytes"):
            api.send(config, request)
        self.assertEqual(self.dispatches, 0)
        self.assertFalse((self.run / "api").exists())

    def test_cached_response_tampering_and_missing_digest_block_reuse(self):
        first = self.invoke()
        folder = self.run / "api" / first["request_hash"]
        artifact = load(folder / "response.json")
        original = dict(artifact)
        artifact["body"] = json.dumps(envelope(content={"tampered": True}))
        save(folder / "response.json", artifact)
        with self.assertRaisesRegex(api.ApiError, "integrity"):
            self.invoke()
        self.assertEqual(self.dispatches, 1)
        save(folder / "response.json", original)
        marker = load(folder / "attempt.json"); marker.pop("response_sha256")
        save(folder / "attempt.json", marker)
        with self.assertRaisesRegex(api.ApiError, "integrity"):
            self.invoke()
        self.assertEqual(self.dispatches, 1)

    def test_loopback_http_transport_and_cross_origin_redirect_rejected(self):
        seen = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                seen.append((self.path, self.headers.get("Authorization"), json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                if self.path.startswith("/redirect/"):
                    self.send_response(307)
                    self.send_header("Location", "http://127.0.0.1:1/forbidden")
                    self.end_headers()
                else:
                    body = json.dumps(envelope()).encode()
                    self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            config = api.ProviderConfig(f"http://127.0.0.1:{server.server_port}/v1", "fixture-vision", api_key=SECRET)
            result = calls.invoke(self.run, config, self.request, stage="overview")
            self.assertFalse(result["cached"])
            self.assertEqual(seen[0][0], "/v1/chat/completions")
            self.assertEqual(seen[0][1], "Bearer " + SECRET)
            self.assertEqual(seen[0][2]["messages"][1]["content"][0]["text"], self.request["messages"][1]["content"][0]["text"])
            data_url = seen[0][2]["messages"][1]["content"][1]["image_url"]["url"]
            self.assertEqual(base64.b64decode(data_url.split(",", 1)[1]), (Path(__file__).parent / "fixture.jpg").read_bytes())
            redirect = api.ProviderConfig(f"http://127.0.0.1:{server.server_port}/redirect", "fixture-vision", api_key=SECRET)
            with self.assertRaisesRegex(api.ApiError, "HTTP 307"):
                calls.invoke(self.run, redirect, self.request, stage="overview")
            self.assertEqual(len(seen), 2)
        finally:
            server.shutdown(); server.server_close(); thread.join(2)
