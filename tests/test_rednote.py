"""Synthetic single-note acquisition, access privacy, and source handoffs."""
import argparse
import contextlib
import copy
import io
import json
import tempfile
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from video_notes import engine, materials
from video_notes.contracts import source_record
from video_notes.run import load, now
from video_notes.sources import normalize_source, resolve_source, source_access
from video_notes.sources import rednote, rednote_bridge

NOTE_ID = "123456780000000012345678"
URL = "https://www.xiaohongshu.com/explore/" + NOTE_ID
ACCESS = URL + "?xsec_token=ephemeral-test-token&xsec_source=pc_feed"
SHORT = "https://xhslink.cn/o/testLink"
CDN = "https://sns-video-v4.xhscdn.com/synthetic/video.mp4"


def detail():
    return {"note": {"noteId": NOTE_ID, "type": "video", "title": "Synthetic lecture", "desc": "Description is not speech",
                     "user": {"nickname": "private-account"}, "video": {"capa": {"duration": 61}, "media": {"stream": {
                         "EF5": [{"masterUrl": CDN, "duration": 61300, "width": 1280, "height": 720, "videoCodec": "h264"}],
                         "EF7": [{"masterUrl": CDN + "?1080", "duration": 61300, "width": 1920, "height": 1080, "videoCodec": "hevc"}],
                         "EF4": [{"masterUrl": CDN + "?audio", "duration": 61300, "width": 640, "height": 360, "audioCodec": "aac"}]
                     }}}}}


def page(value=None):
    state = {"note": {"noteDetailMap": {NOTE_ID: value or detail()}}}
    return "<html><script>window.__INITIAL_STATE__=" + json.dumps(state) + ";</script></html>"


class Response:
    def __init__(self, body):
        self.body = body.encode() if isinstance(body, str) else body

    def read(self, size):
        return self.body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def redirect(url, location):
    return HTTPError(url, 302, "redirect", {"Location": location}, io.BytesIO())


class RedNoteSource(unittest.TestCase):
    def test_direct_urls_are_canonical_and_tokens_never_enter_source(self):
        for path in ("/explore/", "/discovery/item/"):
            self.assertEqual(rednote.canonical_video("https://www.xiaohongshu.com" + path + NOTE_ID + "?xsec_token=private"), (URL, NOTE_ID))
        with patch.object(rednote, "_fetch_page", return_value=(page(), URL, NOTE_ID)):
            result = resolve_source(ACCESS)
        self.assertEqual(result["metadata"]["duration_seconds"], 61.3)
        self.assertEqual(result["content"]["segments"], [])
        self.assertEqual(result["source"]["canonical_url"], URL)
        public = json.dumps(result)
        for private in ("ephemeral-test-token", "Description is not speech", "private-account", "masterUrl"):
            self.assertNotIn(private, public)

    def test_single_video_boundaries_reject_untrusted_urls(self):
        invalid = ["http://www.xiaohongshu.com/explore/" + NOTE_ID, "https://user:secret@www.xiaohongshu.com/explore/" + NOTE_ID,
                   "https://www.xiaohongshu.com:8443/explore/" + NOTE_ID, "https://www.xiaohongshu.com/user/profile/" + NOTE_ID,
                   "https://xhslink.cn.evil.example/o/test", "https://127.0.0.1/explore/" + NOTE_ID,
                   "https://www.xiaohongshu.com/explore/too-short"]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                rednote.persistence_video(value) if rednote.is_rednote(value) else rednote.canonical_video(value)

    def test_all_stream_keys_backup_urls_and_duration_units(self):
        value = detail()
        value["note"]["video"]["media"]["stream"]["EF5"][0]["masterUrl"] = "https://evil.example/video"
        value["note"]["video"]["media"]["stream"]["EF5"][0]["backupUrls"] = [CDN]
        material = rednote.parse_detail(value, NOTE_ID)
        self.assertEqual(material["duration_seconds"], 61.3)
        self.assertEqual(len(material["streams"]), 3)
        with rednote.access(ACCESS), patch.object(rednote, "_fetch_page", return_value=(page(value), URL, NOTE_ID)) as fetch:
            source = rednote.resolve(URL)
            self.assertEqual(rednote.media(source, "video")[0], CDN)
            self.assertTrue(rednote.media(source, "audio")[0].endswith("?audio"))
            self.assertEqual(fetch.call_count, 1)

    def test_media_url_allowlist_has_no_credentials_or_nonstandard_port(self):
        for value in ("http://sns-video-v4.xhscdn.com/a", "https://u:p@sns-video-v4.xhscdn.com/a",
                      "https://sns-video-v4.xhscdn.com:8443/a", "https://sns-video-v4.xhscdn.com.evil.test/a",
                      "https://localhost/a", "https://sns-video-v4.xhscdn.com/a\r\nCookie: private"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                rednote._media_url(value)

    def test_nonvideo_and_missing_media_do_not_use_cover_or_description(self):
        value = detail()
        value["note"].update(type="normal", imageList=[{"urlDefault": CDN}])
        with self.assertRaisesRegex(rednote.AccessError, "not_a_video"):
            rednote.parse_detail(value, NOTE_ID)
        value["note"]["type"] = "video"
        value["note"]["video"] = {}
        with self.assertRaisesRegex(rednote.AccessError, "media_unavailable"):
            rednote.parse_detail(value, NOTE_ID)

    def test_duration_probe_is_single_bounded_fallback(self):
        value = detail()
        video = value["note"]["video"]
        video["capa"] = {}
        for entry in rednote._streams(video["media"]["stream"]):
            entry.pop("duration")
        with rednote.access(ACCESS), patch.object(rednote, "_fetch_page", return_value=(page(value), URL, NOTE_ID)), \
                patch("video_notes.materials.execute", return_value="61.3\n") as probe:
            source = rednote.resolve(URL)
            rednote.media(source, "video")
            rednote.media(source, "audio")
            self.assertEqual(source["metadata"]["duration_seconds"], 61.3)
            self.assertEqual(probe.call_count, 1)
            self.assertEqual(probe.call_args.args[1], 25)
            self.assertNotIn("ephemeral-test-token", " ".join(probe.call_args.args[0]))

    def test_capa_seconds_and_origin_key_are_not_milliseconds(self):
        value = detail()
        value["note"]["video"] = {"capa": {"duration": 61}, "consumer": {"originVideoKey": "synthetic/key"}}
        material = rednote.parse_detail(value, NOTE_ID)
        self.assertEqual(material["duration_seconds"], 61)
        self.assertEqual(material["streams"][0]["url"], "https://sns-video-bd.xhscdn.com/synthetic/key")
        value["note"]["video"]["consumer"]["originVideoKey"] = "../secret"
        with self.assertRaises(rednote.AccessError):
            rednote.parse_detail(value, NOTE_ID)

    def test_nonfinite_missing_and_conflicting_durations_fail(self):
        value = detail()
        for entry in rednote._streams(value["note"]["video"]["media"]["stream"]):
            entry["duration"] = float("nan")
        value["note"]["video"]["capa"]["duration"] = float("inf")
        with patch.object(rednote, "_fetch_page", return_value=(page(value).replace("NaN", "null").replace("Infinity", "null"), URL, NOTE_ID)), \
                patch.object(rednote, "_probe_duration", side_effect=rednote.AccessError("duration_unavailable")):
            with self.assertRaisesRegex(rednote.AccessError, "duration_unavailable"):
                rednote.resolve(URL)
        value = detail()
        value["note"]["video"]["media"]["stream"]["EF7"][0]["duration"] = 999999
        with self.assertRaisesRegex(ValueError, "inconsistent"):
            rednote.parse_detail(value, NOTE_ID)

    def test_initial_state_is_strict_json_with_only_undefined_conversion(self):
        value = detail()
        value["note"]["title"] = 'literal undefined and escaped "quotes" \\path'
        source = page(value).replace('"desc": "Description is not speech"', '"desc": undefined')
        self.assertEqual(rednote.parse_page(source, NOTE_ID)["title"], value["note"]["title"])
        for body in ("window.__INITIAL_STATE__={'note':{}}", "window.__INITIAL_STATE__={\"note\":eval('x')}",
                     "window.__INITIAL_STATE__={\"note\":NaN}", "<script>window.__INITIAL_STATE__={"):
            with self.subTest(body=body), self.assertRaises(rednote.AccessError):
                rednote.parse_page(body, NOTE_ID)

    def test_reactive_note_map_is_supported(self):
        state = {"note": {"noteDetailMap": {"value": {NOTE_ID: detail()}}}}
        self.assertEqual(rednote.parse_page("window.__INITIAL_STATE__=" + json.dumps(state), NOTE_ID)["note_id"], NOTE_ID)

    def test_short_link_redirect_is_bounded_and_no_cookie_is_sent(self):
        opener = Mock()
        opener.open.side_effect = [redirect(SHORT, ACCESS), Response(page())]
        with patch.object(rednote, "build_opener", return_value=opener):
            self.assertEqual(rednote._fetch_page(SHORT)[1:], (URL, NOTE_ID))
        self.assertEqual(opener.open.call_count, 2)
        for call in opener.open.call_args_list:
            self.assertNotIn("Cookie", call.args[0].headers)
            self.assertNotIn("Authorization", call.args[0].headers)

    def test_login_redirect_stops_before_opening_login_and_removes_token_from_error(self):
        opener = Mock()
        opener.open.side_effect = [redirect(SHORT, ACCESS), redirect(ACCESS, "/login?private=ephemeral-test-token")]
        with patch.object(rednote, "build_opener", return_value=opener), self.assertRaises(rednote.AccessError) as failure:
            rednote._fetch_page(SHORT)
        self.assertEqual(failure.exception.code, "login_required")
        self.assertEqual(failure.exception.canonical_url, URL)
        self.assertNotIn("ephemeral-test-token", str(failure.exception))
        self.assertEqual(opener.open.call_count, 2)

    def test_captcha_page_and_offdomain_redirect_stop_without_retry(self):
        for response in (Response("<title>安全验证</title>"), redirect(ACCESS, "https://evil.example/private")):
            opener = Mock()
            opener.open.side_effect = [response] if isinstance(response, Exception) else None
            opener.open.return_value = response
            with patch.object(rednote, "build_opener", return_value=opener), self.assertRaises((rednote.AccessError, ValueError)):
                rednote._fetch_page(ACCESS)
            self.assertEqual(opener.open.call_count, 1)

    def test_redirect_loop_limit_and_oversized_page(self):
        opener = Mock()
        opener.open.side_effect = [redirect(SHORT, SHORT)]
        with patch.object(rednote, "build_opener", return_value=opener), self.assertRaisesRegex(rednote.AccessError, "redirect_loop"):
            rednote._fetch_page(SHORT)
        self.assertEqual(opener.open.call_count, 1)
        opener = Mock()
        opener.open.side_effect = [redirect(f"https://xhslink.com/o/{i}", f"https://xhslink.com/o/{i+1}") for i in range(6)]
        with patch.object(rednote, "build_opener", return_value=opener), self.assertRaisesRegex(rednote.AccessError, "redirect_limit"):
            rednote._fetch_page("https://xhslink.com/o/0")
        self.assertEqual(opener.open.call_count, 6)
        opener = Mock()
        opener.open.return_value = Response(b"x" * (rednote.MAX_PAGE_BYTES + 1))
        with patch.object(rednote, "build_opener", return_value=opener), self.assertRaisesRegex(ValueError, "bounded"):
            rednote._fetch_page(URL)

    def test_bridge_command_contains_no_token_and_uses_existing_runtime(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "scripts").mkdir()
            for name in ("feed.py", "client.py"):
                (root / "scripts" / name).touch()
            executable = root / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
            executable.parent.mkdir(parents=True)
            executable.touch()
            reply = types.SimpleNamespace(returncode=0, stdout=json.dumps({"status": "ok", "material": rednote.parse_detail(detail(), NOTE_ID)}), stderr="private raw diagnostic")
            with patch.object(rednote.subprocess, "run", return_value=reply) as child:
                result = rednote._bridge_extract(ACCESS, root)
            self.assertEqual(result["note_id"], NOTE_ID)
            self.assertNotIn("ephemeral-test-token", " ".join(child.call_args.args[0]))
            self.assertIn("ephemeral-test-token", child.call_args.kwargs["input"])
            self.assertNotIn("env", child.call_args.kwargs)
            self.assertNotIn("private-account", reply.stdout)

    def test_bridge_invalid_or_unsafe_material_never_crosses_boundary(self):
        material = rednote.parse_detail(detail(), NOTE_ID)
        material["streams"][0]["url"] = "https://localhost/private"
        with self.assertRaises(ValueError):
            rednote.validate_material(material)
        for value in (None, {}, {"note_id": NOTE_ID, "streams": []}):
            with self.assertRaises(rednote.AccessError):
                rednote.validate_material(value)

    def test_context_cache_clears_links_and_does_not_cross_runs(self):
        with patch.object(rednote, "_fetch_page", return_value=(page(), URL, NOTE_ID)) as fetch:
            with rednote.access(ACCESS) as context:
                source = rednote.resolve(URL)
                rednote.media(source, "video")
                rednote.media(source, "audio")
                self.assertEqual(fetch.call_count, 1)
            self.assertEqual(context.materials, {})
            self.assertIsNone(context.url)
            self.assertIsNone(rednote._CURRENT.get())
            with rednote.access(ACCESS):
                rednote.resolve(URL)
            self.assertEqual(fetch.call_count, 2)

    def test_fresh_access_link_must_match_prepared_identity(self):
        other = detail()
        other["note"]["noteId"] = "abcdefab0000000012345678"
        material = rednote.parse_detail(other, other["note"]["noteId"])
        with rednote.access(ACCESS, "/configured/skill"), patch.object(rednote, "_bridge_extract", return_value=material):
            with self.assertRaisesRegex(ValueError, "does not match"):
                rednote.resolve(URL)

    def test_source_normalization_cannot_retain_access_queries(self):
        source = source_record("rednote", NOTE_ID, NOTE_ID, "lecture", 61.3, ACCESS)
        self.assertEqual(source["source"]["canonical_url"], URL)
        source["source"]["canonical_url"] = ACCESS
        self.assertEqual(normalize_source(source)["source"]["canonical_url"], URL)
        with self.assertRaises(ValueError):
            source_record("rednote", NOTE_ID, NOTE_ID, "lecture", 61.3, "https://www.xiaohongshu.com/explore/abcdefab0000000012345678")

    def test_prepare_records_only_canonical_and_can_reuse_media_inside_context(self):
        with tempfile.TemporaryDirectory() as folder:
            args = argparse.Namespace(video=ACCESS, out=folder, source_json=None, transcript=None, page=None,
                                      use_env_cookie=False, start=0, end=15, preset="economy", session_log=None,
                                      rednote_skill="/configured/skill")
            material = rednote.parse_detail(detail(), NOTE_ID)
            with source_access(ACCESS, rednote_skill=args.rednote_skill), patch.object(rednote, "_bridge_extract", return_value=material) as bridge, contextlib.redirect_stdout(io.StringIO()):
                materials.prepare(args)
                run = load(Path(folder) / "run.json")
                materials.media_input(folder, run, "video")
                materials.media_input(folder, run, "audio")
            self.assertEqual(bridge.call_count, 1)
            self.assertEqual(run["video"], URL)
            self.assertTrue(run["authenticated"])
            self.assertEqual(run["transcript_status"], "unavailable")
            for path in Path(folder).rglob("*"):
                if path.is_file():
                    self.assertNotIn("ephemeral-test-token", path.read_text())
                    self.assertNotIn("/configured/skill", path.read_text())

    def test_prepare_failure_also_removes_token_and_does_not_read_skill(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(rednote, "_fetch_page", side_effect=rednote.AccessError("login_required")), patch.object(rednote, "stdin_access", return_value=ACCESS), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = materials.main(["prepare", "--video", URL, "--out", folder, "--rednote-access-stdin"])
            self.assertEqual(code, 1)
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()):
            args = argparse.Namespace(video=ACCESS, out=folder, page=None, preset="economy", start=0, end=None, session_log=None)
            materials.record_failed_prepare(args, now(), "safe failure")
            self.assertEqual(load(Path(folder) / "run.json")["video"], URL)
            self.assertNotIn("ephemeral-test-token", "".join(p.read_text() for p in Path(folder).rglob("*") if p.is_file()))

    def test_stdin_access_is_bounded_single_field_and_query_is_not_accepted_as_cli_argument(self):
        self.assertEqual(rednote.stdin_access(io.StringIO(json.dumps({"url": ACCESS}))), ACCESS)
        for value in ("not-json", json.dumps({"url": ACCESS, "cookie": "private"}), "x" * 32769):
            with self.assertRaises(ValueError):
                rednote.stdin_access(io.StringIO(value))
        with tempfile.TemporaryDirectory() as folder, patch.object(rednote, "_fetch_page") as fetch, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(materials.main(["prepare", "--video", ACCESS, "--out", folder]), 1)
            fetch.assert_not_called()
            self.assertNotIn("ephemeral-test-token", errors.getvalue())

    def test_bool_numeric_values_are_missing_and_url_whitespace_is_rejected(self):
        self.assertIsNone(rednote._positive(True))
        self.assertIsNone(rednote._positive(False))
        for char in ("\t", "\r", "\n", "\x00", "\x1f", " ", "\\"):
            for function, value in ((rednote._page_url, ACCESS), (rednote._media_url, CDN)):
                with self.subTest(char=repr(char)), self.assertRaises(ValueError):
                    function(value + char + "private")

    def test_share_resolution_stops_at_first_note_before_anonymous_login_redirect(self):
        target = "https://www.xiaohongshu.com/discovery/item/" + NOTE_ID + "?xsec_token=ephemeral-test-token&xsec_source=pc_share"
        opener = Mock()
        opener.open.side_effect = [redirect(SHORT, target), redirect(target, "https://www.xiaohongshu.com/login")]
        with patch.object(rednote, "build_opener", return_value=opener):
            self.assertEqual(rednote.resolve_share(SHORT), target)
        self.assertEqual(opener.open.call_count, 1)
        with patch.object(rednote, "build_opener") as factory:
            self.assertEqual(rednote.resolve_share(target), target)
        factory.assert_not_called()

    def test_share_resolution_rejects_untrusted_redirect_login_and_loops(self):
        for destination, expected in (("https://evil.test/secret", ValueError),
                                      ("https://www.xiaohongshu.com/login?private=ephemeral-test-token", rednote.AccessError),
                                      (SHORT, rednote.AccessError)):
            opener = Mock()
            opener.open.side_effect = [redirect(SHORT, destination)]
            with self.subTest(destination=destination), patch.object(rednote, "build_opener", return_value=opener), self.assertRaises(expected) as failure:
                rednote.resolve_share(SHORT)
            self.assertNotIn("ephemeral-test-token", str(failure.exception))
            self.assertEqual(opener.open.call_count, 1)

    def test_bridge_regular_login_entry_does_not_block_readable_note(self):
        element = types.SimpleNamespace(is_visible=lambda: True, inner_text=lambda **_: "登录后同步，手机号和验证码",
                                        get_attribute=lambda _: None, evaluate=lambda _: False)
        empty = types.SimpleNamespace(count=lambda: 0)
        container = types.SimpleNamespace(count=lambda: 1, first=element)
        client = types.SimpleNamespace(page=types.SimpleNamespace(url=ACCESS, locator=lambda selector: container if selector == ".login-container" else empty), _check_captcha=lambda: False)
        rednote_bridge._check_page(client)
        rednote_bridge._check_page(client, material_available=True)
        with self.assertRaises(rednote.AccessError) as failure:
            rednote_bridge._check_page(client, phase="bridge_detail", material_available=False)
        self.assertEqual(failure.exception.code, "login_required")
        self.assertEqual(failure.exception.phase, "bridge_detail")
        self.assertEqual(failure.exception.url_path, "/explore/" + NOTE_ID)
        self.assertNotIn("ephemeral-test-token", str(failure.exception))

    def test_bridge_safe_read_uses_visible_main_profile_once(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "scripts").mkdir()
            for name in ("feed.py", "client.py", "profiles.py"):
                (root / "scripts" / name).touch()
            clients = []
            class SafeClient:
                BROWSER_CHANNEL = "chrome"
                def __init__(self, **kwargs):
                    self.kwargs = kwargs
                    self.closed = False
                    self.page = types.SimpleNamespace(url=URL, locator=lambda _: types.SimpleNamespace(count=lambda: 0))
                    self.wait_for_initial_state = Mock()
                    clients.append(self)
                def start(self):
                    pass
                def navigate(self, value):
                    self.requested = value
                def _check_captcha(self):
                    return False
                def close(self):
                    self.closed = True
            read = Mock(return_value=detail())
            profiles = Mock(return_value=types.SimpleNamespace(cookie_path=root / "main/cookies.json", user_data_dir=root / "main/browser"))
            modules = {
                "scripts.client": types.SimpleNamespace(__file__=root / "scripts/client.py", XiaohongshuClient=SafeClient),
                "scripts.feed": types.SimpleNamespace(__file__=root / "scripts/feed.py", FeedDetailAction=lambda _: types.SimpleNamespace(_extract_feed_detail=read)),
                "scripts.profiles": types.SimpleNamespace(__file__=root / "scripts/profiles.py", profile_paths=profiles),
            }
            with patch.object(rednote_bridge.importlib, "import_module", side_effect=modules.__getitem__), \
                    patch.object(rednote_bridge, "resolve_share", return_value=ACCESS) as resolver:
                result = rednote_bridge.extract(SHORT, root)
            resolver.assert_called_once_with(SHORT)
            self.assertEqual(clients[0].requested, ACCESS)
            profiles.assert_called_once_with("main")
            self.assertFalse(clients[0].kwargs["headless"])
            self.assertEqual(clients[0].kwargs["cookie_path"], str(root / "main/cookies.json"))
            clients[0].wait_for_initial_state.assert_called_once_with(timeout=10000, retries=0)
            read.assert_called_once_with(NOTE_ID)
            self.assertTrue(clients[0].closed)
            self.assertEqual(result["note_id"], NOTE_ID)

    def test_bridge_login_overlay_and_captcha_stop_before_feed(self):
        for url, captcha, overlay, expected in ((URL + "/login", False, False, None),
                                                 ("https://www.xiaohongshu.com/login", False, False, "login_required"),
                                                 (URL, True, False, "verification_required"),
                                                 (URL, False, True, "login_required")):
            if expected is None:
                continue
            locator = types.SimpleNamespace(count=lambda: int(overlay), first=types.SimpleNamespace(
                is_visible=lambda: overlay, inner_text=lambda **_: "扫码登录", get_attribute=lambda key: "dialog" if key == "role" else None,
                evaluate=lambda _: True))
            client = types.SimpleNamespace(page=types.SimpleNamespace(url=url, locator=lambda _: locator), _check_captcha=lambda: captcha)
            with self.subTest(expected=expected), self.assertRaises(rednote.AccessError) as failure:
                rednote_bridge._check_page(client)
            self.assertEqual(failure.exception.code, expected)

    def test_bridge_discards_raw_output_and_only_prints_normalized_material(self):
        def raw_extract(*args):
            print("private account and ephemeral-test-token")
            print("private stderr ephemeral-test-token", file=sys.stderr)
            return rednote.parse_detail(detail(), NOTE_ID)
        input_data = io.StringIO(json.dumps({"url": ACCESS, "skill_dir": "/configured/skill"}))
        with patch.object(sys, "stdin", input_data), patch.object(rednote_bridge, "extract", side_effect=raw_extract), \
                contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(rednote_bridge.main(), 0)
        public = output.getvalue()
        self.assertEqual(json.loads(public)["status"], "ok")
        self.assertNotIn("ephemeral-test-token", public)
        self.assertNotIn("private-account", public)
        self.assertEqual(errors.getvalue(), "")

    def test_provided_source_json_cannot_put_access_query_on_disk(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = source_record("rednote", NOTE_ID, NOTE_ID, "lecture", 61.3, URL)
            source["source"]["canonical_url"] = ACCESS
            original = root / "provided.json"
            original.write_text(json.dumps(source))
            args = argparse.Namespace(video=URL, out=root / "run", source_json=original, transcript=None, page=None,
                                      use_env_cookie=False, start=0, end=15, preset="economy", session_log=None)
            with contextlib.redirect_stdout(io.StringIO()):
                materials.prepare(args)
            for path in (root / "run").rglob("*"):
                if path.is_file():
                    self.assertNotIn("ephemeral-test-token", path.read_text())
            self.assertEqual(load(root / "run/source.json")["source"]["canonical_url"], URL)

    def test_engine_scopes_source_access_and_stores_only_public_launch_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            provider = engine.api.ProviderConfig("http://127.0.0.1:1/v1", "fixture", api_key="fixture-key")
            def inspect_stage(directory, *args, **kwargs):
                run = load(directory / "run.json")
                source = load(directory / "source.json")
                rednote.media(source, "video")
                rednote.media(source, "audio")
                self.assertEqual(run["video"], URL)
                return {"status": "fixture"}
            with patch.object(rednote, "_fetch_page", return_value=(page(), URL, NOTE_ID)) as fetch, \
                    patch.object(engine, "_analyze_locked", side_effect=inspect_stage):
                result = engine.analyze(URL, run_dir=folder, provider=provider, rednote_access_url=ACCESS)
            self.assertEqual(result["status"], "fixture")
            self.assertEqual(fetch.call_count, 1)
            self.assertIsNone(rednote._CURRENT.get())
            for path in Path(folder).rglob("*"):
                if path.is_file():
                    self.assertNotIn("ephemeral-test-token", path.read_text())
            self.assertEqual(load(Path(folder) / "api/execution.json")["provider"]["model"], "fixture")

    def test_engine_failure_does_not_store_or_display_access_token(self):
        with tempfile.TemporaryDirectory() as folder:
            provider = engine.api.ProviderConfig("http://127.0.0.1:1/v1", "fixture", api_key="fixture-key")
            with patch.object(rednote, "_fetch_page", side_effect=rednote.AccessError("login_required")):
                result = engine.analyze(ACCESS, run_dir=folder, provider=provider, rednote_access_url=ACCESS)
            self.assertEqual(result["status"], "failed")
            self.assertNotIn("ephemeral-test-token", json.dumps(result))
            for path in Path(folder).rglob("*"):
                if path.is_file():
                    self.assertNotIn("ephemeral-test-token", path.read_text())

    def test_engine_cli_passes_ephemeral_options_without_putting_link_in_video(self):
        result = {"status": "failed", "reason": "fixture", "note": None}
        with patch.object(engine, "analyze", return_value=result) as analyze, patch.object(rednote, "stdin_access", return_value=ACCESS), contextlib.redirect_stdout(io.StringIO()):
            engine.main(["--video", URL, "--out", "fixture", "--base-url", "http://127.0.0.1:1/v1", "--model", "fixture",
                         "--rednote-skill", "/configured/skill", "--rednote-access-stdin"])
        self.assertEqual(analyze.call_args.args[0], URL)
        self.assertEqual(analyze.call_args.kwargs["rednote_access_url"], ACCESS)
        self.assertEqual(analyze.call_args.kwargs["rednote_skill"], "/configured/skill")


if __name__ == "__main__":
    unittest.main()
