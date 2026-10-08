"""Provider-shaped synthetic fixtures; no network or optional dependencies."""
import io
import json
import subprocess
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from video_notes.sources import youtube as yt

IDENTITY = "jNQXAC9IVRw"
URL = "https://www.youtube.com/watch?v=" + IDENTITY
CAPTION = "https://www.youtube.com/api/timedtext?v=fixture&signature=PRIVATE"
STREAM = "https://rr1---sn-fixture.googlevideo.com/videoplayback?signature=PRIVATE"


def metadata(**updates):
    value = {"id": IDENTITY, "title": "A lecture", "duration": 30, "language": "en",
             "subtitles": {}, "automatic_captions": {}, "formats": []}
    value.update(updates)
    return value


def track(ext="json3"):
    return {"ext": ext, "url": CAPTION}


def json3(text="Hello", start=0, duration=1000):
    return json.dumps({"events": [{"tStartMs": start, "dDurationMs": duration, "segs": [{"utf8": text}]}]})


class YouTubeSource(unittest.TestCase):
    def test_canonical_single_item_discards_playlist_and_tracking(self):
        for url in (URL + "&list=PLfixture&t=99", "https://youtu.be/" + IDENTITY + "?si=tracking",
                    "https://www.youtube.com/shorts/" + IDENTITY):
            self.assertEqual(yt.canonical_video(url), (URL, IDENTITY))
        for url in ("https://www.youtube.com/playlist?list=PL", "https://www.youtube.com/@channel",
                    "https://youtube.com.evil.invalid/watch?v=" + IDENTITY,
                    "https://user:secret@youtube.com/watch?v=" + IDENTITY, "file:///tmp/video"):
            with self.assertRaises(ValueError): yt.canonical_video(url)

    def test_only_normalized_public_metadata_persists(self):
        raw = metadata(subtitles={"en": [track()]}, formats=[{"url": STREAM}],
                       http_headers={"Cookie": "secret"}, thumbnail="https://signed.invalid/private")
        with patch.object(yt, "_extract", return_value=raw), patch.object(yt, "_fetch_caption", return_value=json3()):
            source = yt.resolve(URL + "&list=PL")
        self.assertEqual(source["source"]["platform"], "youtube")
        self.assertEqual(source["selection"]["part_id"], IDENTITY)
        self.assertEqual(source["content"]["source_type"], "youtube_subtitle")
        self.assertFalse(source["content"]["automatic"])
        serialized = json.dumps(source)
        for secret in ("PRIVATE", "Cookie", "thumbnail", "videoplayback", "timedtext"):
            self.assertNotIn(secret, serialized)

    def test_preferred_manual_then_automatic_language(self):
        info = metadata(subtitles={"zh-Hans": [track("vtt")], "en": [track("vtt")]},
                        automatic_captions={"en": [track()]})
        self.assertEqual(yt.caption_tracks(info)[0][:2], ("en", False))
        self.assertEqual(yt.caption_tracks(info, "zh")[0][:2], ("zh-Hans", False))
        info["subtitles"] = {"ja": [track()]}
        self.assertEqual(yt.caption_tracks(info, "en")[0][:2], ("en", True))
        self.assertEqual(yt.caption_tracks(info, "fr"), [])

    def test_automatic_flag_and_missing_captions_leave_asr_possible(self):
        raw = metadata(automatic_captions={"en": [track()]})
        with patch.object(yt, "_extract", return_value=raw), patch.object(yt, "_fetch_caption", return_value=json3()):
            source = yt.resolve(URL)
        self.assertEqual(source["content"]["source_type"], "youtube_auto")
        self.assertTrue(source["content"]["automatic"])
        for raw in (metadata(), metadata(subtitles={"en": [track()]})):
            with patch.object(yt, "_extract", return_value=raw), patch.object(yt, "_fetch_caption", side_effect=RuntimeError("Denied")):
                source = yt.resolve(URL)
            self.assertEqual(source["metadata"]["duration_seconds"], 30)
            self.assertEqual(source["content"]["source_type"], "none")
            self.assertEqual(source["content"]["segments"], [])
            self.assertIn("missing_reason", source["content"])

    def test_caption_fallback_attempts_are_bounded(self):
        raw = metadata(subtitles={f"en-{index}": [track()] for index in range(12)})
        with patch.object(yt, "_extract", return_value=raw), patch.object(yt, "_fetch_caption", side_effect=RuntimeError("Denied")) as fetch:
            yt.resolve(URL)
        self.assertEqual(fetch.call_count, 3)

    def test_failed_manual_track_falls_back_to_automatic(self):
        raw = metadata(subtitles={"en": [track("vtt"), track(), track("srt")]}, automatic_captions={"en": [track()]})
        with patch.object(yt, "_extract", return_value=raw), patch.object(yt, "_fetch_caption", side_effect=[RuntimeError("Denied"), json3()]) as fetch:
            source = yt.resolve(URL, language="en")
        self.assertEqual(fetch.call_count, 2)
        self.assertTrue(source["content"]["automatic"])

    def test_live_playlist_mismatched_identity_and_bad_duration_rejected(self):
        for raw in (metadata(_type="playlist", entries=[]), metadata(is_live=True), metadata(live_status="is_upcoming"),
                    metadata(live_status="post_live"), metadata(id="abcdefghijk"), metadata(duration=None),
                    metadata(duration=float("nan"))):
            with patch.object(yt, "_extract", return_value=raw), self.assertRaises(ValueError): yt.resolve(URL)
        with patch.object(yt, "_extract", return_value=metadata(live_status="was_live")):
            self.assertEqual(yt.resolve(URL)["metadata"]["duration_seconds"], 30)

    def test_json3_and_cues_have_original_seconds_and_clean_text(self):
        self.assertEqual(yt.parse_captions(json3("A &amp; B", 1500, 4000), "json3", 4),
                         [{"start": 1.5, "end": 4, "text": "A & B"}])
        for ext, payload in (("vtt", "WEBVTT\n\nNOTE hidden\nignored\n\ncue-id\n00:01.000 --> 00:03.000 align:start\n<c>First</c> line\nsecond\n"),
                             ("srt", "1\n00:00:01,000 --> 00:00:03,000\nFirst line\nsecond\n")):
            self.assertEqual(yt.parse_captions(payload, ext, 10), [{"start": 1.0, "end": 3.0, "text": "First line second"}])
        with self.assertRaises(ValueError): yt.parse_captions(json3("Bad", -1000), "json3", 10)
        with self.assertRaises(ValueError): yt.parse_captions("not json", "json3", 10)

    def test_rolling_cues_remove_overlap_but_preserve_later_repetition(self):
        payload = "WEBVTT\n\n00:00.000 --> 00:03.000\nThe robot\n\n00:02.000 --> 00:05.000\nThe robot learns\n\n00:04.000 --> 00:07.000\nrobot learns online\n\n00:10.000 --> 00:12.000\nThe robot\n"
        self.assertEqual([row["text"] for row in yt.parse_captions(payload, "vtt", 30, automatic=True)],
                         ["The robot", "learns", "online", "The robot"])
        self.assertEqual(len(yt.parse_captions(payload, "vtt", 30)), 4)
        repeated = "WEBVTT\n\n00:00.000 --> 00:03.000\nYes\n\n00:02.000 --> 00:05.000\nYes\n"
        self.assertEqual(yt.parse_captions(repeated, "vtt", 30, True), [{"start": 0.0, "end": 5.0, "text": "Yes"}])
        self.assertEqual(yt._rolling_text("机器人学习", "学习更多"), "更多")
        self.assertEqual(yt._rolling_text("cat", "caterpillar"), "caterpillar")

    def test_stream_selection_returns_ephemeral_url_and_no_auth_headers(self):
        formats = [{"url": STREAM, "protocol": "https", "height": 1080, "vcodec": "avc1", "acodec": "none"},
                   {"url": STREAM + "&small=1", "protocol": "https", "height": 720, "vcodec": "avc1", "acodec": "none"},
                   {"url": STREAM + "&audio=1", "protocol": "https", "vcodec": "none", "acodec": "mp4a", "abr": 64,
                    "http_headers": {"Cookie": "secret", "Authorization": "private", "User-Agent": "safe-agent"}}]
        source = {"source": {"canonical_url": URL}}
        with patch.object(yt, "_extract", return_value=metadata(formats=formats)):
            url, options = yt.media(source, "video", height=720)
            self.assertTrue(url.endswith("small=1"))
            url, options = yt.media(source, "audio")
            self.assertTrue(url.endswith("audio=1"))
            self.assertIn("safe-agent", options)
            self.assertNotIn("secret", str(options))
            self.assertNotIn("private", str(options))
        for host in ("http://rr1.googlevideo.com/video", "https://user:secret@rr1.googlevideo.com/video", "https://127.0.0.1/video"):
            with patch.object(yt, "_extract", return_value=metadata(formats=[{"url": host, "vcodec": "avc1"}])), self.assertRaises(RuntimeError):
                yt.media(source, "video")

    def test_ffmpeg_preserves_existing_https_proxy_and_no_proxy(self):
        with patch.object(yt, "getproxies", return_value={"https": "http://localhost:1234"}), patch.object(yt, "proxy_bypass", return_value=False):
            self.assertEqual(yt._proxy_options(STREAM), ["-http_proxy", "http://localhost:1234"])
        with patch.object(yt, "getproxies", return_value={"all": "http://localhost:1234"}), patch.object(yt, "proxy_bypass", return_value=False):
            self.assertEqual(yt._proxy_options(STREAM), ["-http_proxy", "http://localhost:1234"])
        with patch.object(yt, "getproxies", return_value={}), patch.object(yt, "proxy_bypass", return_value=False):
            self.assertEqual(yt._proxy_options(STREAM), [])
        with patch.object(yt, "getproxies", return_value={"https": "http://localhost:1234"}), patch.object(yt, "proxy_bypass", return_value=True):
            self.assertEqual(yt._proxy_options(STREAM), [])
        with patch.object(yt, "getproxies", return_value={"https": "socks5://user:PRIVATE@localhost:1234"}), patch.object(yt, "proxy_bypass", return_value=False), self.assertRaises(RuntimeError) as error:
            yt._proxy_options(STREAM)
        self.assertNotIn("PRIVATE", str(error.exception))

    def test_command_disables_profiles_and_bounds_retries(self):
        def which(name):
            return {"uv": "/bin/uv", "node": "/bin/node"}.get(name)
        with patch.object(yt.shutil, "which", side_effect=which), patch.object(yt, "_run", return_value=subprocess.CompletedProcess([], 0, "v22.0.0", "")):
            command = yt._command()
        for flag in ("--ignore-config", "--no-playlist", "--no-plugin-dirs", "--no-cache-dir", "--no-remote-components", "--isolated"):
            self.assertIn(flag, command)
        self.assertEqual(command[command.index("--extractor-retries") + 1], "1")
        self.assertNotIn("--cookies-from-browser", command)
        self.assertNotIn("--netrc", command)
        with patch.object(yt.shutil, "which", return_value=None), self.assertRaises(RuntimeError): yt._command()

    def test_extraction_errors_never_expose_provider_stderr_or_urls(self):
        result = subprocess.CompletedProcess([], 1, "", "Sign in to confirm you're not a bot " + STREAM + " Cookie=secret")
        with patch.object(yt, "_command", return_value=(["fake"], {})), patch.object(yt, "_run", return_value=result), self.assertRaises(RuntimeError) as error:
            yt._extract(URL)
        self.assertIn("anonymous access", str(error.exception))
        self.assertNotIn("PRIVATE", str(error.exception))
        self.assertNotIn("secret", str(error.exception))
        with patch.object(yt.subprocess, "run", side_effect=subprocess.TimeoutExpired(["fake", STREAM], 120)), self.assertRaises(RuntimeError) as error:
            yt._run(["fake", STREAM], 120)
        self.assertNotIn("PRIVATE", str(error.exception))

    def test_existing_dependency_version_is_recorded_separately_from_pins(self):
        def which(name): return {"yt-dlp": "/bin/yt-dlp", "node": "/bin/node"}.get(name)
        versions = [subprocess.CompletedProcess([], 0, "2026.10.07\n", ""),
                    subprocess.CompletedProcess([], 0, "v22.23.2\n", "")]
        with patch.object(yt.shutil, "which", side_effect=which), patch.object(yt, "_run", side_effect=versions):
            command, details = yt._command(with_details=True)
        self.assertEqual(command[0], "/bin/yt-dlp")
        self.assertEqual(details["yt_dlp_version"], "2026.10.07")
        self.assertIsNone(details["ejs_version"])
        self.assertEqual(details["js_runtime"], {"name": "node", "version": "22.23.2"})

    def test_caption_request_retries_and_size_limit(self):
        class Response(io.BytesIO):
            def geturl(self): return CAPTION
        opener = unittest.mock.Mock()
        opener.open.side_effect = [HTTPError(CAPTION, 503, "private failure", {}, None), Response(b"caption")]
        with patch.object(yt, "build_opener", return_value=opener): self.assertEqual(yt._fetch_caption(track()), "caption")
        self.assertEqual(opener.open.call_count, 2)
        opener.open.side_effect = [URLError("secret"), URLError("secret")]
        with patch.object(yt, "build_opener", return_value=opener), self.assertRaises(RuntimeError) as error: yt._fetch_caption(track())
        self.assertNotIn("secret", str(error.exception))
        opener.open.side_effect = [Response(b"12345")]
        with patch.object(yt, "build_opener", return_value=opener), patch.object(yt, "MAX_CAPTION_BYTES", 4), self.assertRaises(ValueError):
            yt._fetch_caption(track())
        with self.assertRaises(ValueError): yt._fetch_caption({"url": "https://127.0.0.1/private"})


if __name__ == "__main__":
    unittest.main()
