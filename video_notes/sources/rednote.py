"""Single-video RedNote acquisition with ephemeral access links and media URLs."""
from __future__ import annotations

import contextvars
import html
import json
import math
import re
import shutil
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ..contracts import source_record

PAGE_HOSTS = frozenset(("www.xiaohongshu.com", "xiaohongshu.com", "xhslink.cn", "xhslink.com"))
NOTE_PATH = re.compile(r"/(?:explore|discovery/item)/([0-9a-f]{24})/?\Z", re.I)
USER_AGENT = "Mozilla/5.0 (compatible; VideoStudyNotes/1.0)"
MAX_PAGE_BYTES = 4 * 1024 * 1024
MAX_REDIRECTS = 5
MAX_BRIDGE_BYTES = 256 * 1024


class AccessError(RuntimeError):
    """Safe diagnostics never contain the supplied access link or browser state."""

    def __init__(self, code="access_unavailable", canonical_url=None, *, phase=None, url_path=None):
        self.code = code
        self.canonical_url = canonical_url
        self.phase = phase
        self.url_path = url_path
        super().__init__("RedNote " + code + "; provide a fresh share link, local media, or explicitly configure the read-only RedNote skill")


def is_rednote(video):
    try:
        return (urlsplit(str(video)).hostname or "").lower() in PAGE_HOSTS
    except ValueError:
        return False


def _page_url(value):
    try:
        parts = urlsplit(str(value))
        valid = (parts.scheme == "https" and (parts.hostname or "").lower() in PAGE_HOSTS
                 and not parts.username and not parts.password and parts.port in (None, 443))
    except ValueError:
        valid = False
    if not valid or any(ord(char) < 33 or char == "\\" for char in str(value)):
        raise ValueError("Expected a public HTTPS RedNote note or share link without credentials")
    return str(value)


def canonical_video(video):
    parts = urlsplit(_page_url(video))
    match = NOTE_PATH.fullmatch(parts.path)
    if (parts.hostname or "").lower() not in ("www.xiaohongshu.com", "xiaohongshu.com") or not match:
        raise ValueError("Expected one RedNote video note; short links require bounded resolution")
    identity = match.group(1).lower()
    return "https://www.xiaohongshu.com/explore/" + identity, identity


def persistence_video(video):
    """Remove access queries before writing a run, including failed acquisition."""
    if not is_rednote(video):
        return str(video)
    parts = urlsplit(_page_url(video))
    if NOTE_PATH.fullmatch(parts.path):
        return canonical_video(video)[0]
    if (parts.hostname or "").lower() not in ("xhslink.cn", "xhslink.com") or not re.fullmatch(r"/[A-Za-z0-9/_-]{1,160}", parts.path):
        raise ValueError("Select one RedNote note or share link")
    return parts._replace(query="", fragment="").geturl()


def _media_url(value):
    try:
        parts = urlsplit(str(value))
        host = (parts.hostname or "").lower()
        valid = (parts.scheme == "https" and host.endswith(".xhscdn.com")
                 and not parts.username and not parts.password and parts.port in (None, 443))
    except ValueError:
        valid = False
    if not valid or any(ord(char) < 33 or char == "\\" for char in str(value)):
        raise ValueError("Unexpected RedNote media host or URL")
    return str(value)


def _positive(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) and result > 0 else None


def _unwrap(value):
    if isinstance(value, dict):
        if isinstance(value.get("value"), dict):
            return value["value"]
        if isinstance(value.get("_value"), dict):
            return value["_value"]
    return value


def _streams(value):
    stack, count = [value], 0
    while stack:
        row = stack.pop()
        count += 1
        if count > 4096:
            raise ValueError("RedNote stream metadata exceeds the bounded limit")
        if isinstance(row, dict):
            if "masterUrl" in row or "backupUrls" in row:
                yield row
            else:
                stack.extend(reversed(list(row.values())))
        elif isinstance(row, list):
            stack.extend(reversed(row))


def parse_detail(detail, identity):
    """Normalize only video material fields; descriptions are never transcripts."""
    detail = _unwrap(detail)
    note = _unwrap(detail.get("note", detail)) if isinstance(detail, dict) else None
    if not isinstance(note, dict) or note.get("type") != "video" or not isinstance(note.get("video"), dict):
        raise AccessError("not_a_video")
    if note.get("noteId") and str(note["noteId"]).lower() != identity:
        raise ValueError("RedNote page identity does not match the selected note")
    video = note["video"]
    media = video.get("media") or {}
    candidates, seen = [], set()
    for entry in _streams(media.get("stream", {}) if isinstance(media, dict) else {}):
        values = [entry.get("masterUrl")]
        backups = entry.get("backupUrls", [])
        if isinstance(backups, list):
            values.extend(backups)
        for value in values:
            if not isinstance(value, str):
                continue
            try:
                url = _media_url(value)
            except ValueError:
                continue
            if url in seen:
                continue
            seen.add(url)
            duration = _positive(entry.get("duration"))
            candidates.append({"url": url, "duration_seconds": duration / 1000 if duration else None,
                               "width": _positive(entry.get("width")), "height": _positive(entry.get("height")),
                               "video_codec": str(entry.get("videoCodec") or ""),
                               "audio_codec": str(entry.get("audioCodec") or "")})
            if len(candidates) > 64:
                raise ValueError("RedNote stream count exceeds the bounded limit")
    if not candidates:
        consumer = video.get("consumer") or {}
        key = consumer.get("originVideoKey") if isinstance(consumer, dict) else None
        if isinstance(key, str) and re.fullmatch(r"[A-Za-z0-9/_-]{1,1024}", key) and ".." not in key:
            candidates.append({"url": "https://sns-video-bd.xhscdn.com/" + key.lstrip("/"),
                               "duration_seconds": None, "width": None, "height": None,
                               "video_codec": "", "audio_codec": ""})
    if not candidates:
        raise AccessError("media_unavailable")
    durations = [row["duration_seconds"] for row in candidates if row["duration_seconds"]]
    capa = video.get("capa") or {}
    duration = durations[0] if durations else _positive(capa.get("duration") if isinstance(capa, dict) else None)
    if durations and any(abs(value - duration) > max(2, duration * .05) for value in durations):
        raise ValueError("RedNote streams have inconsistent video duration")
    return {"note_id": identity, "title": str(note.get("title") or identity)[:1000],
            "duration_seconds": duration, "streams": candidates}


def _json_state(page):
    match = re.search(r"window\.__INITIAL_STATE__\s*=\s*", page)
    if not match or match.end() >= len(page) or page[match.end()] != "{":
        raise AccessError("video_metadata_unavailable")
    start, depth, quoted, escaped, end = match.end(), 0, False, False, None
    for index in range(start, len(page)):
        char = page[index]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
            if depth == 0:
                end = index + 1
                break
    if end is None:
        raise AccessError("invalid_video_metadata")
    payload = page[start:end]
    # Replace the JavaScript undefined literal only outside JSON strings.
    pieces, index, quoted, escaped = [], 0, False, False
    while index < len(payload):
        char = payload[index]
        if not quoted and payload.startswith("undefined", index):
            before = payload[index - 1] if index else ""
            after = payload[index + 9] if index + 9 < len(payload) else ""
            if not (before.isalnum() or before == "_") and not (after.isalnum() or after == "_"):
                pieces.append("null")
                index += 9
                continue
        pieces.append(char)
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        index += 1
    def invalid_constant(value):
        raise ValueError("Non-JSON constant")
    try:
        value = json.loads("".join(pieces), parse_constant=invalid_constant)
    except (ValueError, TypeError, RecursionError):
        raise AccessError("invalid_video_metadata") from None
    return value


def parse_page(page, identity):
    if not isinstance(page, str) or len(page.encode("utf-8")) > MAX_PAGE_BYTES:
        raise ValueError("RedNote page exceeds the bounded size")
    state = _json_state(page)
    maps = _unwrap((state.get("note") or {}).get("noteDetailMap")) if isinstance(state, dict) else None
    detail = maps.get(identity) if isinstance(maps, dict) else None
    if not detail:
        raise AccessError("video_metadata_unavailable")
    return parse_detail(detail, identity)


def _access_signal(url, page=""):
    parts = urlsplit(url)
    target = (parts.path + "?" + parts.query).lower()
    if any(marker in target for marker in ("captcha", "security-verification", "verifytype", "verifybiz")):
        return "verification_required"
    if re.search(r"/(?:login|signin)(?:/|$)", parts.path, re.I):
        return "login_required"
    title = re.search(r"<title[^>]*>(.*?)</title>", page, re.I | re.S)
    text = html.unescape(title.group(1)) if title else ""
    if any(marker in text for marker in ("安全验证", "验证码")):
        return "verification_required"
    if any(marker in text for marker in ("登录", "登入")):
        return "login_required"
    return None


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        return None


def resolve_share(video):
    """Follow a share link only until its first legitimate note URL.

    Return the provided redirect target unchanged in memory. The authenticated
    browser must see that note access URL before any anonymous login redirect.
    """
    current = _page_url(video)
    persistence_video(current)
    try:
        canonical_video(current)
        return current
    except ValueError:
        pass
    opener, seen = build_opener(_NoRedirect()), set()
    for attempt in range(MAX_REDIRECTS + 1):
        current = _page_url(current)
        signal = _access_signal(current)
        if signal:
            raise AccessError(signal, phase="share_redirect", url_path=urlsplit(current).path)
        if current in seen:
            raise AccessError("redirect_loop", phase="share_redirect")
        seen.add(current)
        try:
            response = opener.open(Request(current, headers={"User-Agent": USER_AGENT}), timeout=20)
        except HTTPError as error:
            if error.code not in (301, 302, 303, 307, 308):
                code = "access_denied" if error.code in (401, 403) else "access_unavailable"
                error.close()
                raise AccessError(code, phase="share_redirect") from None
            location = error.headers.get("Location")
            error.close()
            if not location or attempt == MAX_REDIRECTS:
                raise AccessError("redirect_limit", phase="share_redirect") from None
            current = _page_url(urljoin(current, location))
            try:
                canonical_video(current)
                return current
            except ValueError:
                pass
            continue
        except (URLError, TimeoutError, OSError):
            raise AccessError("network_unavailable", phase="share_redirect") from None
        with response:
            data = response.read(MAX_PAGE_BYTES + 1)
        if len(data) > MAX_PAGE_BYTES:
            raise ValueError("RedNote page exceeds the bounded size")
        signal = _access_signal(current, data.decode("utf-8", errors="replace"))
        raise AccessError(signal or "share_target_unavailable", phase="share_redirect", url_path=urlsplit(current).path)
    raise AccessError("redirect_limit", phase="share_redirect")


def _fetch_page(video):
    current = _page_url(video)
    persistence_video(current)
    opener, seen, canonical = build_opener(_NoRedirect()), set(), None
    for attempt in range(MAX_REDIRECTS + 1):
        current = _page_url(current)
        try:
            canonical = canonical_video(current)[0]
        except ValueError:
            pass
        signal = _access_signal(current)
        if signal:
            raise AccessError(signal, canonical)
        if current in seen:
            raise AccessError("redirect_loop", canonical)
        seen.add(current)
        try:
            response = opener.open(Request(current, headers={"User-Agent": USER_AGENT}), timeout=20)
        except HTTPError as error:
            if error.code in (301, 302, 303, 307, 308):
                location = error.headers.get("Location")
                error.close()
                if not location or attempt == MAX_REDIRECTS:
                    raise AccessError("redirect_limit", canonical) from None
                current = _page_url(urljoin(current, location))
                continue
            code = "access_denied" if error.code in (401, 403) else "access_unavailable"
            error.close()
            raise AccessError(code, canonical) from None
        except (URLError, TimeoutError, OSError):
            raise AccessError("network_unavailable", canonical) from None
        with response:
            data = response.read(MAX_PAGE_BYTES + 1)
        if len(data) > MAX_PAGE_BYTES:
            raise ValueError("RedNote page exceeds the bounded size")
        page = data.decode("utf-8", errors="replace")
        signal = _access_signal(current, page)
        if signal:
            raise AccessError(signal, canonical)
        url, identity = canonical_video(current)
        return page, url, identity
    raise AccessError("redirect_limit", canonical)


def _bridge_extract(video, skill_path):
    directory = Path(skill_path).expanduser().resolve()
    if not (directory / "scripts/feed.py").is_file() or not (directory / "scripts/client.py").is_file():
        raise ValueError("RedNote skill directory lacks its read-only feed/client modules")
    executable = directory / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    script = Path(__file__).with_name("rednote_bridge.py")
    if executable.is_file():
        command = [str(executable), str(script)]
    elif shutil.which("uv"):
        command = [shutil.which("uv"), "run", "--project", str(directory), "--frozen", "--offline", "--no-sync", "python", str(script)]
    else:
        raise AccessError("skill_runtime_unavailable")
    try:
        result = subprocess.run(command, input=json.dumps({"skill_dir": str(directory), "url": _page_url(video)}),
                                capture_output=True, text=True, timeout=150)
    except (OSError, subprocess.TimeoutExpired):
        raise AccessError("skill_read_failed") from None
    if len(result.stdout.encode("utf-8")) > MAX_BRIDGE_BYTES:
        raise AccessError("invalid_skill_response")
    try:
        payload = json.loads(result.stdout)
    except (ValueError, TypeError):
        raise AccessError("skill_read_failed") from None
    if not isinstance(payload, dict):
        raise AccessError("invalid_skill_response")
    if result.returncode or payload.get("status") != "ok":
        code = payload.get("code")
        phase = payload.get("phase")
        path = payload.get("url_path")
        phase = phase if phase in ("share_redirect", "bridge_start", "bridge_navigation", "bridge_state", "bridge_detail", "bridge_parse") else None
        path = path if isinstance(path, str) and re.fullmatch(r"/[A-Za-z0-9/_-]{0,160}", path) else None
        raise AccessError(code if code in ("login_required", "verification_required", "not_a_video", "media_unavailable", "skill_runtime_unavailable") else "skill_read_failed", phase=phase, url_path=path)
    return validate_material(payload.get("material"))


def validate_material(value):
    """Validate the subprocess boundary without trusting an external skill response."""
    if not isinstance(value, dict) or not re.fullmatch(r"[0-9a-f]{24}", str(value.get("note_id", ""))):
        raise AccessError("invalid_skill_response")
    streams = value.get("streams")
    if not isinstance(streams, list) or not 1 <= len(streams) <= 64:
        raise AccessError("invalid_skill_response")
    rows = []
    for row in streams:
        if not isinstance(row, dict):
            raise AccessError("invalid_skill_response")
        rows.append({"url": _media_url(row.get("url")), "duration_seconds": _positive(row.get("duration_seconds")),
                     "width": _positive(row.get("width")), "height": _positive(row.get("height")),
                     "video_codec": str(row.get("video_codec") or ""), "audio_codec": str(row.get("audio_codec") or "")})
    return {"note_id": value["note_id"], "title": str(value.get("title") or value["note_id"])[:1000],
            "duration_seconds": _positive(value.get("duration_seconds")), "streams": rows}


@dataclass
class _Access:
    url: str | None
    skill_path: str | None
    materials: dict = field(default_factory=dict)


_CURRENT = contextvars.ContextVar("rednote_access", default=None)


@contextmanager
def access(video=None, skill_path=None):
    """Scope original access links and media URLs to one in-memory analysis."""
    previous = _CURRENT.get()
    if video is None and skill_path is None and previous is not None:
        yield previous
        return
    context = _Access(_page_url(video) if video else None, str(skill_path) if skill_path else None)
    token = _CURRENT.set(context)
    try:
        yield context
    finally:
        context.materials.clear()
        context.url = context.skill_path = None
        _CURRENT.reset(token)


def _transport_options(referer):
    return ["-headers", f"Referer: {referer}\r\nUser-Agent: {USER_AGENT}\r\n", "-rw_timeout", "15000000"]


def _probe_duration(material):
    from ..materials import execute
    url = material["streams"][0]["url"]
    referer = "https://www.xiaohongshu.com/explore/" + material["note_id"]
    try:
        result = execute(["ffprobe", "-v", "error", *_transport_options(referer), "-show_entries", "format=duration",
                          "-of", "default=nw=1:nk=1", url], 25)
        duration = _positive(result.strip())
    except (ValueError, RuntimeError, OSError):
        duration = None
    if not duration:
        raise AccessError("duration_unavailable")
    return duration


def _material(video, skill_path=None):
    context = _CURRENT.get()
    selected = context.url if context and context.url else video
    selected_skill = skill_path or (context.skill_path if context else None)
    try:
        expected = canonical_video(video)[1]
    except ValueError:
        expected = None
    if context and expected in context.materials:
        return context.materials[expected]
    if selected_skill:
        material = _bridge_extract(selected, selected_skill)
    else:
        page, _, identity = _fetch_page(selected)
        material = parse_page(page, identity)
    if expected and material["note_id"] != expected:
        raise ValueError("RedNote access link does not match this prepared note")
    if not material["duration_seconds"]:
        material["duration_seconds"] = _probe_duration(material)
    if context:
        context.materials[material["note_id"]] = material
    return material


def resolve(video, skill_path=None):
    material = _material(video, skill_path)
    identity = material["note_id"]
    context = _CURRENT.get()
    authenticated = bool(skill_path or (context and context.skill_path))
    return source_record("rednote", identity, identity, material["title"], material["duration_seconds"],
                         "https://www.xiaohongshu.com/explore/" + identity, authenticated=authenticated,
                         content={"source_type": "none", "segments": [], "missing_reason": "rednote_timestamped_captions_unavailable",
                                  "acquisition": {"tool": "rednote_skill_bridge" if authenticated else "rednote_public_page"}})


def media(source, kind, height=720):
    if kind not in ("audio", "video"):
        raise ValueError("RedNote media kind must be audio or video")
    url, identity = canonical_video(source["source"]["canonical_url"])
    if identity != source["metadata"]["media_id"]:
        raise ValueError("RedNote source identity mismatch")
    material = _material(url)
    rows = material["streams"]
    if kind == "video":
        suitable = [row for row in rows if row["height"] and row["height"] <= height]
        if suitable:
            row = max(suitable, key=lambda item: (item["height"], item["video_codec"].lower().startswith(("avc", "h264"))))
        else:
            row = min(rows, key=lambda item: item["height"] or math.inf)
    else:
        audio = [row for row in rows if row["audio_codec"] and row["audio_codec"].lower() not in ("none", "null")]
        row = min(audio or rows, key=lambda item: item["height"] or math.inf)
    return row["url"], _transport_options(url)


def stdin_access(stream=None):
    """Read one ephemeral URL from stdin rather than command arguments or env."""
    text = (stream or sys.stdin).read(32769)
    if len(text) > 32768:
        raise ValueError("RedNote access input exceeds the bounded size")
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        raise ValueError("RedNote access stdin expects a JSON object containing url") from None
    if not isinstance(value, dict) or set(value) != {"url"} or not isinstance(value["url"], str):
        raise ValueError("RedNote access stdin expects only a url field")
    persistence_video(value["url"])
    return value["url"]
