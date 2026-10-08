"""Anonymous, bounded YouTube acquisition; ephemeral provider data stays here."""
from __future__ import annotations

import html
import json
import math
import re
import shutil
import subprocess
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, getproxies, proxy_bypass

from ..contracts import source_record

YTDLP_VERSION = "2026.8.19"
EJS_VERSION = "0.8.0"
MAX_CAPTION_BYTES = 8 * 1024 * 1024
MAX_INFO_BYTES = 20 * 1024 * 1024
USER_AGENT = "Mozilla/5.0 (compatible; VideoStudyNotes/1.0)"
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")


def canonical_video(video):
    parts = urlsplit(str(video))
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("https", "http") or parts.username or parts.password or parts.port not in (None, 80, 443):
        raise ValueError("Expected a public YouTube video URL without credentials")
    if host == "youtu.be":
        identity = parts.path.strip("/")
    elif host in ("youtube.com", "www.youtube.com", "m.youtube.com"):
        if parts.path == "/watch":
            identity = parse_qs(parts.query).get("v", [""])[0]
        elif parts.path.startswith(("/shorts/", "/embed/", "/live/")):
            identity = parts.path.split("/")[2]
        else:
            raise ValueError("Select one YouTube video; playlists and channels are not expanded")
    else:
        raise ValueError("Unsupported YouTube host")
    if not VIDEO_ID.fullmatch(identity):
        raise ValueError("Expected one valid YouTube video identity")
    return f"https://www.youtube.com/watch?v={identity}", identity


def _run(command, timeout):
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise RuntimeError("YouTube dependency or extraction timed out; retry or provide local materials") from None
    except OSError:
        raise RuntimeError("YouTube dependency cannot be executed; check yt-dlp or uv") from None


def _runtime():
    for name, minimum in (("deno", (2, 3)), ("node", (22, 0))):
        path = shutil.which(name)
        if not path:
            continue
        try:
            result = _run([path, "--version"], 5)
        except RuntimeError:
            continue
        match = re.search(r"(?:^|\s|v)(\d+)\.(\d+)(?:\.(\d+))?", result.stdout)
        if result.returncode == 0 and match and tuple(map(int, match.groups()[:2])) >= minimum:
            return name, path, ".".join(part for part in match.groups() if part is not None)
    return None


def _command(with_details=False):
    executable = shutil.which("yt-dlp")
    actual_version, isolated = None, False
    if executable:
        version = _run([executable, "--ignore-config", "--no-plugin-dirs", "--no-cache-dir", "--version"], 5)
        parsed = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version.stdout.strip())
        if version.returncode == 0 and parsed and tuple(map(int, parsed.groups())) >= (2026, 8, 19):
            command = [executable]
            actual_version = version.stdout.strip()
        else:
            executable = None
    if not executable:
        uv = shutil.which("uv")
        if not uv:
            raise RuntimeError("YouTube needs current yt-dlp or uv with scripts/requirements-youtube.txt")
        requirements = Path(__file__).resolve().parents[2] / "scripts/requirements-youtube.txt"
        command = [uv, "run", "--isolated", "--no-project", "--with-requirements", str(requirements),
                   "python", "-m", "yt_dlp"]
        actual_version, isolated = YTDLP_VERSION, True
    runtime = _runtime()
    if not runtime:
        raise RuntimeError("YouTube needs Deno >= 2.3 or Node >= 22 for yt-dlp-ejs; no runtime was installed automatically")
    command = command + ["--ignore-config", "--no-plugin-dirs", "--no-cache-dir", "--no-remote-components",
                      "--no-js-runtimes", "--js-runtimes", f"{runtime[0]}:{runtime[1]}",
                      "--no-playlist", "--no-wait-for-video", "--skip-download", "--no-progress",
                      "--socket-timeout", "15", "--retries", "1", "--extractor-retries", "1",
                      "--fragment-retries", "1", "--ignore-no-formats-error", "--dump-single-json"]
    details = {"yt_dlp_version": actual_version, "ejs_version": EJS_VERSION if isolated else None,
               "environment": "isolated_uv" if isolated else "installed",
               "js_runtime": {"name": runtime[0], "version": runtime[2]}}
    return (command, details) if with_details else command


def _error_reason(message):
    message = str(message).lower()
    if "sign in" in message or "not a bot" in message:
        return "anonymous access was challenged; provide local materials or retry from an accessible network"
    if "private" in message or "unavailable" in message or "not available" in message:
        return "video unavailable to anonymous access"
    if "429" in message or "too many requests" in message:
        return "server rate limit reached"
    if "timed out" in message or "timeout" in message:
        return "network timeout"
    return "network or extractor failure; check optional dependencies or provide local materials"


def _extract(url):
    command, details = _command(with_details=True)
    result = _run(command + ["--", url], 120)
    if result.returncode:
        raise RuntimeError("YouTube retrieval failed: " + _error_reason(result.stderr)) from None
    if len(result.stdout.encode("utf-8")) > MAX_INFO_BYTES:
        raise RuntimeError("YouTube metadata exceeds the bounded response size")
    try:
        value = json.loads(result.stdout)
    except (ValueError, TypeError):
        raise RuntimeError("YouTube returned invalid metadata") from None
    if not isinstance(value, dict):
        raise RuntimeError("YouTube returned invalid metadata")
    value["_adapter_dependencies"] = details
    return value


def _validate_info(info, expected_id):
    if info.get("_type") not in (None, "video") or "entries" in info:
        raise ValueError("YouTube playlists are not processed; select one video")
    if info.get("id") != expected_id:
        raise ValueError("YouTube returned a different video identity")
    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming", "post_live"):
        raise ValueError("Live or still-processing YouTube videos are not supported")
    try:
        duration = float(info["duration"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("YouTube video duration is unavailable") from None
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("YouTube video duration is unavailable")
    return duration


def _language_rank(code, preferred):
    code = code.lower()
    for index, want in enumerate(preferred):
        want = want.lower()
        if code == want:
            return index, 0, code
        if code.split("-")[0] == want.split("-")[0]:
            return index, 1, code
    return len(preferred), 2, code


def caption_tracks(info, language=None):
    preferred = [language] if language and language != "auto" else [info.get("language") or "en", "en", "zh"]
    candidates = []
    for automatic, group in ((False, "subtitles"), (True, "automatic_captions")):
        for code, formats in (info.get(group) or {}).items():
            if code == "live_chat" or not isinstance(formats, list):
                continue
            rank = _language_rank(code, preferred)
            if language and language != "auto" and rank[0] == len(preferred):
                continue
            for entry in formats:
                if isinstance(entry, dict) and entry.get("ext") in ("json3", "vtt", "srt") and entry.get("url"):
                    candidates.append((automatic, rank, ("json3", "vtt", "srt").index(entry["ext"]), code, entry))
    # Prefer human captions in a useful language, then useful automatic captions;
    # unrelated languages are a last resort only when no language was requested.
    candidates.sort(key=lambda row: (row[1][0] == len(preferred), row[0], row[1], row[2]))
    tracks, seen = [], set()
    for automatic, _, _, code, entry in candidates:
        key = code, automatic
        if key not in seen:
            tracks.append((code, automatic, entry))
            seen.add(key)
    return tracks


def _public_url(value, media=False):
    parts = urlsplit(str(value))
    host = (parts.hostname or "").lower()
    youtube = host == "youtube.com" or host.endswith(".youtube.com")
    googlevideo = media and (host == "googlevideo.com" or host.endswith(".googlevideo.com"))
    if parts.scheme != "https" or not (youtube or googlevideo) or parts.username or parts.password or parts.port not in (None, 443):
        raise ValueError("Unexpected YouTube material host or URL")
    return str(value)


class _CaptionRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        _public_url(newurl)
        return super().redirect_request(request, response, code, message, headers, newurl)


def _fetch_caption(entry):
    url = _public_url(entry["url"])
    request = Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(2):
        try:
            with build_opener(_CaptionRedirects()).open(request, timeout=15) as response:
                _public_url(response.geturl())
                data = response.read(MAX_CAPTION_BYTES + 1)
            if len(data) > MAX_CAPTION_BYTES:
                raise ValueError("Caption response exceeds the bounded size")
            return data.decode("utf-8-sig")
        except HTTPError as exc:
            status = exc.code
            exc.close()
            if attempt == 0 and status in (429, 500, 502, 503, 504):
                continue
            raise RuntimeError(f"Caption request denied (HTTP {status})") from None
        except (URLError, TimeoutError, OSError):
            if attempt == 0:
                continue
            raise RuntimeError("Caption network request failed") from None


def _clean(text):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", str(text)))).strip()


def _timestamp(value):
    parts = value.replace(",", ".").split(":")
    if len(parts) not in (2, 3):
        raise ValueError("Invalid caption timestamp")
    total = 0.0
    for part in parts:
        total = total * 60 + float(part)
    return total


def _rolling_text(previous, current):
    before, after = previous.split(), current.split()
    for length in range(min(len(before), len(after)), 0, -1):
        if before[-length:] == after[:length]:
            return " ".join(after[length:])
    if re.search(r"[\u3400-\u9fff]", previous + current):
        for length in range(min(len(previous), len(current)), 1, -1):
            if previous[-length:] == current[:length]:
                return current[length:].strip()
    return current


def parse_captions(payload, ext, duration, automatic=False):
    rows = []
    if ext == "json3":
        value = json.loads(payload)
        if not isinstance(value, dict) or not isinstance(value.get("events"), list):
            raise ValueError("Invalid JSON3 captions")
        for event in value["events"]:
            if not isinstance(event, dict) or not event.get("segs"):
                continue
            start = float(event.get("tStartMs", 0)) / 1000
            end = start + float(event.get("dDurationMs", 0)) / 1000
            text = "".join(str(segment.get("utf8", "")) for segment in event["segs"] if isinstance(segment, dict))
            rows.append({"start": start, "end": end, "text": _clean(text)})
    elif ext in ("vtt", "srt"):
        for block in re.split(r"\n\s*\n", payload.replace("\r\n", "\n")):
            lines = block.splitlines()
            if not lines or lines[0].startswith(("NOTE", "STYLE", "REGION")):
                continue
            for index, line in enumerate(lines):
                match = re.match(r"\s*([\d:.,]+)\s+-->\s+([\d:.,]+)", line)
                if match:
                    rows.append({"start": _timestamp(match[1]), "end": _timestamp(match[2]),
                                 "text": _clean(" ".join(lines[index + 1:]))})
                    break
    else:
        raise ValueError("Unsupported caption format")
    result, previous = [], None
    for row in sorted(rows, key=lambda item: (item["start"], item["end"])):
        start, end, text = row["start"], min(duration, row["end"]), row["text"]
        if not math.isfinite(start) or not math.isfinite(row["end"]) or start < 0 or row["end"] < start:
            raise ValueError("Invalid caption time range")
        if not text or end <= start:
            continue
        if automatic and previous and start <= previous["end"] + .25:
            text = _rolling_text(previous["text"], text)
            if not text and result:
                result[-1]["end"] = max(result[-1]["end"], end)
        previous = {**row, "end": end}
        if text:
            result.append({"start": start, "end": end, "text": text})
        if len(result) > 20000:
            raise ValueError("Caption segment count exceeds the bounded limit")
    return result


def resolve(video, language=None):
    url, identity = canonical_video(video)
    info = _extract(url)
    duration = _validate_info(info, identity)
    content = {"source_type": "none", "language": language, "segments": [], "automatic": None,
               "acquisition": {"tool": "yt-dlp", "isolated_fallback_version": YTDLP_VERSION,
                               "isolated_fallback_ejs_version": EJS_VERSION,
                               **info.get("_adapter_dependencies", {})}}
    candidates = caption_tracks(info, language)
    # Try at most three alternatives; a denied caption does not discard metadata.
    for code, automatic, entry in candidates[:3]:
        try:
            segments = parse_captions(_fetch_caption(entry), entry["ext"], duration, automatic)
        except (ValueError, RuntimeError, TypeError, KeyError):
            continue
        if segments:
            content.update(source_type="youtube_auto" if automatic else "youtube_subtitle", language=code,
                           automatic=automatic, segments=segments)
            break
    if not content["segments"]:
        content["missing_reason"] = "caption_unavailable_or_denied" if candidates else "no_caption_in_requested_language"
    return source_record("youtube", identity, identity, info.get("title") or identity, duration, url, content=content)


def _proxy_options(url):
    # FFmpeg does not consistently honor the uppercase HTTPS_PROXY environment
    # that Python/yt-dlp use. Preserve the existing route, including NO_PROXY.
    host = urlsplit(url).hostname
    if proxy_bypass(host):
        return []
    proxies = getproxies()
    proxy = proxies.get("https") or proxies.get("all")
    if not proxy:
        return []
    parts = urlsplit(proxy)
    if parts.scheme != "http" or not parts.hostname or "\r" in proxy or "\n" in proxy:
        raise RuntimeError("YouTube FFmpeg access needs an HTTP tunnel proxy or local media; configured proxy type is unsupported")
    return ["-http_proxy", proxy]


def media(source, kind, height=720):
    if kind not in ("audio", "video"):
        raise ValueError("YouTube media kind must be audio or video")
    url, identity = canonical_video(source["source"]["canonical_url"])
    info = _extract(url)
    _validate_info(info, identity)
    candidates = []
    for entry in info.get("formats") or []:
        if not isinstance(entry, dict) or not entry.get("url") or entry.get("has_drm"):
            continue
        try:
            _public_url(entry["url"], media=True)
        except ValueError:
            continue
        if kind == "video" and entry.get("vcodec") not in (None, "none"):
            candidates.append(entry)
        if kind == "audio" and entry.get("acodec") not in (None, "none"):
            candidates.append(entry)
    if not candidates:
        raise RuntimeError("No anonymous YouTube media stream is accessible; provide local media")
    direct = [entry for entry in candidates if entry.get("protocol") in ("https", "http")]
    if direct:
        candidates = direct
    if kind == "video":
        suitable = [entry for entry in candidates if 0 < (entry.get("height") or 0) <= height]
        if suitable:
            entry = max(suitable, key=lambda item: (item.get("height") or 0, str(item.get("vcodec", "")).startswith("avc")))
        else:
            entry = min(candidates, key=lambda item: item.get("height") or math.inf)
    else:
        audio_only = [entry for entry in candidates if entry.get("vcodec") == "none"]
        if audio_only:
            candidates = audio_only
        entry = min(candidates, key=lambda item: item.get("abr") or item.get("tbr") or math.inf)
    # Only non-secret transport headers are forwarded, never cookies or auth.
    headers = {key.lower(): str(value) for key, value in {**(info.get("http_headers") or {}),
               **(entry.get("http_headers") or {})}.items()}
    user_agent = headers.get("user-agent", USER_AGENT)
    if "\r" in user_agent or "\n" in user_agent:
        user_agent = USER_AGENT
    return entry["url"], ["-user_agent", user_agent, "-rw_timeout", "15000000", *_proxy_options(entry["url"])]
