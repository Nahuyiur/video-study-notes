"""Local-only product supervisor. Credentials cross a child pipe, never a file."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import uuid
import webbrowser
import zipfile

from . import api, calls, notes
from .delivery import markdown
from .locking import file_lock
from .run import load, now

VERSION = "0.1.0"
MAX_BODY = 24_000
MAX_ARTIFACT = 64_000_000
WEB = Path(__file__).with_name("web")
TERMINAL = {"completed", "failed", "interrupted", "outcome_unknown"}
ARTIFACTS = {"html": ("note.html", "text/html; charset=utf-8"),
             "markdown": ("note.md", "text/markdown; charset=utf-8"),
             "markdown-zip": ("note-markdown.zip", "application/zip"),
             "markdown-text": ("note-text.md", "text/markdown; charset=utf-8")}


class ProductError(ValueError):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.status = code, status


def identifier(value):
    if not isinstance(value, str):
        raise ProductError("invalid_id", "任务编号无效。")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        raise ProductError("invalid_id", "任务编号无效。") from None
    if str(parsed) != value:
        raise ProductError("invalid_id", "任务编号无效。")
    return value


def _text(value, label, *, limit=2000, required=False):
    if (not isinstance(value, str) or len(value) > limit
            or (required and not value.strip()) or any(ord(c) < 32 and c not in "\n\t" for c in value)):
        raise ProductError("invalid_input", label + "格式或长度不正确。")
    return value.strip()


def _number(value, label, *, low=0, high=360000, integer=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
            or not low <= value <= high or (integer and not isinstance(value, int))):
        raise ProductError("invalid_input", label + "超出允许范围。")
    return value


def source_url(value):
    value = _text(value, "视频链接", limit=2000, required=True)
    try:
        parts = urlsplit(value)
        host, port = parts.hostname, parts.port
    except ValueError:
        raise ProductError("invalid_url", "请填写 Bilibili、YouTube 或小红书视频链接。") from None
    hosts = {"bilibili.com", "www.bilibili.com", "b23.tv", "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be",
             "www.xiaohongshu.com", "xiaohongshu.com", "xhslink.cn", "xhslink.com"}
    if (parts.scheme != "https" or host not in hosts or port not in (None, 443)
            or parts.username or parts.password or "\\" in value or any(ord(c) < 33 for c in value)):
        raise ProductError("invalid_url", "请填写 HTTPS 的 Bilibili、YouTube 或小红书视频链接。")
    if host in {"www.xiaohongshu.com", "xiaohongshu.com", "xhslink.cn", "xhslink.com"}:
        from .sources.rednote import persistence_video
        try:
            return persistence_video(value)
        except ValueError:
            raise ProductError("invalid_url", "请填写单篇小红书视频笔记或分享链接。") from None
    allowed = {"v", "p", "t", "start"}
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if k in allowed])
    return urlunsplit(("https", host, parts.path or "/", query, ""))


def credential(value):
    value = _text(value, "API 密钥", limit=4096, required=True)
    if any(ord(c) < 32 for c in value):
        raise ProductError("invalid_input", "API 密钥含有无效字符。")
    return value


def submission(payload):
    if not isinstance(payload, dict) or set(payload) - {"submission_id", "url", "provider", "api_key", "start", "end", "preset", "focus", "language", "allow_asr", "strategy", "budget"}:
        raise ProductError("invalid_input", "提交内容不正确。")
    sid = identifier(payload.get("submission_id"))
    key = credential(payload.get("api_key"))
    provider = payload.get("provider")
    if not isinstance(provider, dict) or set(provider) - {"base_url", "model", "token_parameter", "json_mode", "timeout_seconds"}:
        raise ProductError("invalid_provider", "请填写模型服务地址和模型名称。")
    provider = {"base_url": _text(provider.get("base_url"), "服务地址", limit=2000, required=True),
                "model": _text(provider.get("model"), "模型名称", limit=200, required=True),
                "token_parameter": provider.get("token_parameter", "max_completion_tokens"),
                "json_mode": provider.get("json_mode", True),
                "timeout_seconds": provider.get("timeout_seconds", 90)}
    try:
        api.ProviderConfig(**provider, api_key=key)
    except (ValueError, TypeError):
        raise ProductError("invalid_provider", "模型配置无效；请检查地址、参数和等待时间（最多 600 秒）。") from None
    budget = payload.get("budget", {})
    allowed_budget = {"max_calls": (2, 8, 3), "max_input_chars": (1000, 200000, 80000),
                      "max_images": (1, 64, 16), "output_tokens_per_call": (128, 16384, 4096),
                      "max_reserved_output_tokens": (256, 65536, 12288)}
    if not isinstance(budget, dict) or set(budget) - allowed_budget.keys():
        raise ProductError("invalid_budget", "调用预算配置无效。")
    budget = {name: _number(budget.get(name, default), "调用预算", low=low, high=high, integer=True)
              for name, (low, high, default) in allowed_budget.items()}
    if budget["max_reserved_output_tokens"] < 2 * budget["output_tokens_per_call"]:
        raise ProductError("invalid_budget", "输出预算需要至少预留两次调用。")
    start = _number(payload.get("start", 0), "开始时间")
    end = payload.get("end")
    if end is not None:
        end = _number(end, "结束时间")
        if end <= start:
            raise ProductError("invalid_range", "结束时间需要晚于开始时间。")
    preset, strategy = payload.get("preset", "economy"), payload.get("strategy", "slides")
    allow_asr = payload.get("allow_asr", True)
    if preset not in ("economy", "standard") or strategy not in ("slides", "uniform") or not isinstance(allow_asr, bool):
        raise ProductError("invalid_input", "提取设置无效。")
    spec = {"url": source_url(payload.get("url")), "provider": provider, "budget": budget,
            "start": start, "end": end, "preset": preset, "strategy": strategy, "allow_asr": allow_asr,
            "focus": _text(payload.get("focus", ""), "学习重点"),
            "language": _text(payload.get("language", "zh"), "输出语言", limit=80, required=True)}
    if key in json.dumps(spec, ensure_ascii=False) or key in payload.get("url", ""):
        raise ProductError("credential_in_input", "请只在密钥栏填写密钥。")
    return sid, spec, key


def readiness():
    from .sources.youtube import _runtime
    runtime = _runtime()  # bounded local version checks; no acquisition or API call
    checks = [("FFmpeg", bool(shutil.which("ffmpeg")), True, "用于音视频提取，请安装 FFmpeg 并加入系统 PATH。"),
              ("FFprobe", bool(shutil.which("ffprobe")), True, "随 FFmpeg 安装，用于读取视频时长。"),
              ("Pillow", importlib.util.find_spec("PIL") is not None, True, "请按安装说明安装 Pillow。"),
              ("uv", bool(shutil.which("uv")), False, "无字幕时的本地语音转写需要 uv；首次运行会下载模型。"),
              ("yt-dlp / uv", bool(shutil.which("yt-dlp") or shutil.which("uv")), False, "YouTube 提取需要 yt-dlp 或 uv，以及支持的 JavaScript 运行环境，见安装说明。"),
              ("YouTube JavaScript", runtime is not None, False, "YouTube 提取需要 Node 22 以上或 Deno 2.3 以上；请安装其中一种并加入系统 PATH。")]
    return {"ready": all(available for _, available, required, _ in checks if required),
            "items": [{"name": name, "available": available, "required": required,
                       "message": "已就绪" if available else message} for name, available, required, message in checks]}


def _read(path):
    try:
        if path.is_symlink() or path.stat().st_size > 4_000_000:
            return None
        return load(path)
    except (OSError, ValueError, TypeError):
        return None


def _contained(path, parent):
    if parent.resolve() != parent.absolute() or path.is_symlink() or not path.resolve().is_relative_to(parent.resolve()):
        raise ProductError("unsafe_file", "结果文件不可用。", 404)
    return path


def _file_hash(path):
    size = path.stat().st_size
    if size > MAX_ARTIFACT:
        raise ProductError("invalid_result", "结果文件超过允许大小。", 404)
    hashed = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hashed.update(chunk)
    return {"bytes": size, "sha256": hashed.hexdigest()}


def _unknown(directory):
    run = _read(directory / "run.json") or {}
    known = set()
    for path in (directory / "api").glob("*/attempt.json"):
        row = _read(path) or {}
        response = _read(path.parent / "response.json") or {}
        valid = (response.get("call_id") == row.get("call_id") and response.get("request_hash") == row.get("request_hash")
                 and isinstance(response.get("body"), str) and isinstance(response.get("http_status"), int)
                 and (row.get("status") != "received" or calls.digest(response) == row.get("response_sha256")))
        if valid:
            known.add(row.get("call_id"))
        else:
            return True
    return bool(run.get("api_pending") and run["api_pending"] not in known)


def make_artifacts(directory):
    """Generate a fixed allowlist from a validated frozen note, without model calls."""
    directory = Path(directory)
    note = notes.load_note(directory)
    receipt = notes.export_note(directory, ("html", "md"))
    export = directory / "public"
    export.mkdir(exist_ok=True)
    shutil.copyfile(receipt["outputs"]["html"], export / "note.html")
    md_path = Path(receipt["outputs"]["md"])
    shutil.copyfile(md_path, export / "note.md")
    (export / "note-text.md").write_text(markdown.render_note(directory, note, export / "note-text.md", text_only=True), encoding="utf-8")
    assets = md_path.with_name(md_path.stem + "-assets")
    referenced = {row["frame_id"] for row in note["figures"]}
    referenced.update(block["frame_id"] for section in note["sections"] for block in section["blocks"] if block["type"] == "image")
    asset_rows = {frame["sha256"]: frame for frame in note["snapshot"]["frames"] if frame["id"] in referenced}
    with zipfile.ZipFile(export / "note-markdown.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(md_path, md_path.name)
        for sha, frame in sorted(asset_rows.items()):
            item = _contained(assets / (sha + ".jpg"), assets)
            if _file_hash(item)["sha256"] != frame["sha256"]:
                raise ProductError("invalid_result", "Markdown 图片校验未通过。", 404)
            archive.write(item, assets.name + "/" + item.name)
    calls.durable_save(directory / "public-manifest.json", {"schema_version": 1, "note_content_hash": note["content_hash"],
        "files": {name: _file_hash(_contained(export / name, export)) for name, _ in ARTIFACTS.values()}})
    return note


def _usage(report):
    report = report if isinstance(report, dict) else {}
    counters = (report.get("tokens", {}).get("api_actual") or {})
    known = report.get("tokens", {}).get("api_known_subtotal") or {}
    calls_seen = report.get("api_calls", [])
    unknown = sum(row.get("outcome") == "unknown" for row in calls_seen)
    return {"api_calls": len(calls_seen), "input_tokens": counters.get("input_tokens"),
            "output_tokens": counters.get("output_tokens"), "total_tokens": counters.get("total_tokens"),
            "cost_usd": report.get("cost", {}).get("api_usd"), "unknown_attempts": unknown,
            "unknown_token_calls": report.get("tokens", {}).get("api_unknown_token_calls", 0),
            "known_input_tokens": known.get("input_tokens"), "known_output_tokens": known.get("output_tokens"),
            "known_total_tokens": known.get("total_tokens"),
            "known_cost_subtotal_usd": report.get("cost", {}).get("api_known_subtotal_usd"),
            "unknown_cost_calls": report.get("cost", {}).get("api_unknown_cost_calls", 0)}


def safe_failure(status, reason=""):
    """Map execution evidence to useful directions; never expose arbitrary exception text."""
    if status == "outcome_unknown":
        return "服务响应结果未知，可能已产生费用。已保留记录并停止，不能自动重试；请先核对服务方记录。"
    reason = str(reason).lower()
    if "rednote" in reason and any(marker in reason for marker in ("login_required", "verification_required")):
        return "小红书要求登录或验证，素材读取已停止。可使用你自己本机的已登录会话，或可访问的分享链接、本地视频；本任务不会自动重试。"
    if "deno" in reason or "node" in reason or "javascript" in reason or "yt-dlp-ejs" in reason:
        return "YouTube 提取缺少受支持的 JavaScript 环境。请安装 Node 22 以上或 Deno 2.3 以上并加入系统 PATH，再明确继续；本次没有自动安装或重试。"
    if "401" in reason or "403" in reason or "credential" in reason:
        return "模型服务拒绝了请求，请检查自己的密钥、模型权限和服务地址，再明确选择继续。"
    if "budget" in reason or "exceeded" in reason:
        return "本次材料或调用达到所选预算，已停止并保留记录。请用较短视频区间创建新任务。"
    if "ffmpeg" in reason or "ffprobe" in reason or "pillow" in reason or "uv" in reason:
        return "本机提取依赖未就绪，请按安装说明检查 FFmpeg、Pillow 和语音转写环境。"
    if "subtitle" in reason or "transcript" in reason or "asr" in reason:
        return "字幕或本地转写未完成，请检查提取环境。已有材料仍保留。"
    if "http" in reason or "model" in reason or "response" in reason or "json" in reason:
        return "模型服务未返回可用结果。请检查服务配置与模型能力；已有响应会保留，继续不会重发已确认的调用。"
    return "任务未完成。请检查视频访问权限、本机提取环境及模型配置；已有材料和调用记录已保留。"


class Product:
    def __init__(self, data_dir, *, executor=None, rednote_skill=None):
        self.root = Path(data_dir).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.jobs = self.root / "jobs"
        if self.jobs.is_symlink():
            raise ProductError("unsafe_directory", "任务数据目录不能是符号链接。")
        self.jobs.mkdir(exist_ok=True, mode=0o700)
        self._locks = ExitStack()
        try:
            self._locks.enter_context(file_lock(self.root / ".service.lock", blocking=False))
        except BlockingIOError:
            raise ProductError("service_busy", "已有服务正在使用该数据目录。", 409) from None
        self.session = secrets.token_urlsafe(32)
        self.mutex = threading.Lock()
        self.children = {}
        self.executor = executor or self._spawn
        # Trusted local operator setting, never accepted as a browser path.
        self.rednote_skill = str(Path(rednote_skill).expanduser().resolve()) if rednote_skill else None

    def close(self):
        # A running child owns execution independently and can finish after UI closes.
        self._locks.close()

    def folder(self, jid):
        path = self.jobs / identifier(jid)
        _contained(path, self.jobs)
        if not path.is_dir():
            raise ProductError("not_found", "任务不存在。", 404)
        return path

    def record(self, jid):
        row = _read(self.folder(jid) / "job.json")
        if not isinstance(row, dict) or row.get("id") != jid:
            raise ProductError("invalid_job", "任务记录不可用。", 404)
        return row

    def worker_busy(self):
        if any(child.poll() is None for child in self.children.values()):
            return True
        try:
            with file_lock(self.root / ".execution.lock", blocking=False):
                return False
        except BlockingIOError:
            return True

    def _spawn(self, jid, attempt, key, access_url=None):
        command = [sys.executable, "-m", "video_notes.worker", "--data-dir", str(self.root), "--job", jid, "--attempt", attempt]
        child = subprocess.Popen(command, cwd=Path(__file__).resolve().parent.parent,
                                 stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.children[attempt] = child
        encoded = json.dumps({"api_key": key, "rednote_access_url": access_url,
                              "rednote_skill": self.rednote_skill}, ensure_ascii=False).encode()
        # Waiting happens in a separate thread; requests and progress stay responsive.
        def feed():
            nonlocal encoded
            try:
                child.communicate(encoded)
            finally:
                encoded = b""
        threading.Thread(target=feed, daemon=True, name="video-notes-worker-input").start()

    def submit(self, payload):
        sid, spec, key = submission(payload)
        from .sources.rednote import is_rednote
        access_url = _text(payload["url"], "视频链接", limit=2000, required=True) if is_rednote(spec["url"]) else None
        try:
            with self.mutex:
                for path in self.jobs.iterdir():
                    row = _read(path / "job.json") if path.is_dir() and not path.is_symlink() else None
                    if row and row.get("submission_id") == sid:
                        if row.get("spec") != spec:
                            raise ProductError("submission_conflict", "该提交编号已用于其他配置，请创建新任务。", 409)
                        return self.view(row["id"]), False
                if self.worker_busy():
                    raise ProductError("busy", "已有任务正在运行，请等待完成后再开始。", 409)
                jid, attempt = str(uuid.uuid4()), str(uuid.uuid4())
                folder = self.jobs / jid
                folder.mkdir(mode=0o700)
                row = {"schema_version": 1, "id": jid, "submission_id": sid, "spec": spec,
                       "created_at": now(), "attempt": attempt, "imported": False}
                calls.durable_save(folder / "job.json", row)
                try:
                    self.executor(jid, attempt, key, access_url)
                except Exception:
                    calls.durable_save(folder / (attempt + ".json"), {"status": "failed", "message": "执行进程未能启动，请检查本机 Python 环境。"})
                return self.view(jid), True
        finally:
            key = None
            access_url = None

    def resume(self, jid, payload):
        if not isinstance(payload, dict) or set(payload) - {"api_key", "url"} or "api_key" not in payload:
            raise ProductError("invalid_input", "继续任务需要本次使用的密钥。")
        key = credential(payload["api_key"])
        access_url = None
        try:
            with self.mutex:
                row, view = self.record(jid), self.view(jid)
                if not view["can_resume"]:
                    raise ProductError("cannot_resume", "此任务不能继续；未知调用需要先核对服务方记录。", 409)
                if self.worker_busy():
                    raise ProductError("busy", "已有任务正在运行，请等待完成。", 409)
                if key in json.dumps(row, ensure_ascii=False):
                    raise ProductError("credential_in_input", "请只在密钥栏填写密钥。")
                from .sources.rednote import is_rednote
                if payload.get("url"):
                    raw_url = _text(payload["url"], "视频链接", limit=2000, required=True)
                    clean_url = source_url(raw_url)
                    source = _read(self.folder(jid) / "run/source.json") or {}
                    expected = source.get("source", {}).get("canonical_url") or row["spec"]["url"]
                    if not is_rednote(row["spec"]["url"]) or clean_url not in {expected, row["spec"]["url"]}:
                        raise ProductError("source_mismatch", "继续时需填写同一篇小红书笔记的链接。")
                    if key in raw_url:
                        raise ProductError("credential_in_input", "请只在密钥栏填写密钥。")
                    access_url = raw_url
                attempt = str(uuid.uuid4())
                row["attempt"] = attempt
                calls.durable_save(self.folder(jid) / "job.json", row)
                try:
                    self.executor(jid, attempt, key, access_url)
                except Exception:
                    calls.durable_save(self.folder(jid) / (attempt + ".json"), {"status": "failed", "message": "执行进程未能启动，请检查本机 Python 环境。"})
                return self.view(jid)
        finally:
            key = None
            access_url = None

    def view(self, jid):
        folder, row = self.folder(jid), self.record(jid)
        directory = folder / "run"
        record = _read(folder / (row["attempt"] + ".json")) or {}
        child = self.children.get(row["attempt"])
        run = _read(directory / "run.json") or {}
        status = record.get("status")
        if status not in TERMINAL:
            active = _read(self.root / ".execution-active.json") or {}
            owns_lock = active.get("job") == jid and active.get("attempt") == row["attempt"] and self.worker_busy()
            status = "running" if (child is not None and child.poll() is None) or owns_lock else "interrupted"
        if _unknown(directory):
            if status not in ("running", "completed"):
                status = "outcome_unknown"
        stage = "正在提取视频与字幕"
        if (directory / "api/synthesis-inputs.json").exists() or any(((_read(p) or {}).get("stage") == "synthesis") for p in (directory / "api").glob("*/attempt.json")):
            stage = "正在整理课程笔记"
        elif (directory / "api/detail-inputs.json").exists():
            stage = "正在细读关键画面"
        elif (directory / "api/overview-inputs.json").exists():
            stage = "正在阅读字幕与抽样画面"
        elif run.get("frames"):
            stage = "关键画面已提取"
        elif run.get("transcript_status") == "ready":
            stage = "字幕已就绪"
        title, url = "视频学习笔记", row.get("spec", {}).get("url")
        scope = {"requested_range": [row.get("spec", {}).get("start", 0), row.get("spec", {}).get("end")],
                 "processed_range": None, "remaining_range": None, "video_duration_seconds": None}
        report = _read(directory / "usage.json") or {}
        if run:
            scope.update({name: run.get(name) for name in scope})
        source = _read(directory / "source.json") or {}
        title = source.get("metadata", {}).get("title") or title
        artifact = {}
        if status == "completed":
            try:
                note = notes.load_note(directory)
                title, url = note["title"], note["snapshot"]["source"].get("canonical_url")
                scope.update({name: note["snapshot"]["run"].get(name) for name in scope})
                report = note["snapshot"]["usage"]
                stage = "笔记已生成"
                for name in ("html", "markdown", "markdown_zip", "markdown_text"):
                    path, _ = self.artifact(jid, name.replace("_", "-"), verify_status=False)
                    if path.is_file():
                        artifact[name] = f"/api/jobs/{jid}/artifacts/{name.replace('_', '-')}"
            except (ValueError, OSError, KeyError, TypeError):
                status, stage = "failed", "结果校验未通过"
        elif run:
            from .engine import _progress
            try:
                report = _progress(directory) or report
            except (ValueError, OSError, KeyError, TypeError):
                pass
        can_resume = (status in ("failed", "interrupted") and not row.get("imported")
                      and record.get("recoverable", True) and not _unknown(directory))
        message = record.get("message", "")
        if status == "failed" and not message:
            message = safe_failure(status)
        if status == "interrupted":
            message = "上次执行已中断，未自动重启。填写密钥后可明确继续已有任务。"
        if status == "outcome_unknown":
            message = safe_failure(status)
        return {"id": jid, "title": str(title)[:500], "source_url": url, "status": status,
                "stage": stage, "message": message, "created_at": row["created_at"], "scope": scope,
                "usage": _usage(report), "artifacts": artifact, "can_resume": can_resume}

    def history(self):
        result = []
        for path in self.jobs.iterdir():
            try:
                if path.is_dir() and not path.is_symlink():
                    result.append(self.view(path.name))
            except (ProductError, ValueError, KeyError, TypeError, OSError):
                continue
        return sorted(result, key=lambda row: row["created_at"], reverse=True)

    def artifact(self, jid, name, *, verify_status=True):
        if name not in ARTIFACTS:
            raise ProductError("not_found", "结果文件不存在。", 404)
        folder = self.folder(jid)
        if verify_status:
            row = self.record(jid)
            record = _read(folder / (row["attempt"] + ".json")) or {}
            if record.get("status") != "completed":
                raise ProductError("not_ready", "任务还没有可下载的结果。", 404)
        try:
            note = notes.load_note(folder / "run")
            manifest = _read(folder / "run/public-manifest.json") or {}
            if (manifest.get("schema_version") != 1 or manifest.get("note_content_hash") != note["content_hash"]
                    or set(manifest.get("files", {})) != {name for name, _ in ARTIFACTS.values()}):
                raise ValueError("Output manifest differs from frozen note")
        except (ValueError, OSError, KeyError, TypeError):
            raise ProductError("invalid_result", "结果校验未通过。", 404) from None
        name, mime = ARTIFACTS[name]
        path = _contained(folder / "run/public" / name, folder / "run/public")
        if not path.is_file() or path.stat().st_size > MAX_ARTIFACT:
            raise ProductError("not_found", "结果文件不存在或超过下载上限。", 404)
        if _file_hash(path) != manifest["files"][name]:
            raise ProductError("invalid_result", "下载文件校验未通过，请使用原始冻结笔记重新导出。", 404)
        return path, mime

    def import_run(self, source):
        # Operator-only CLI path. Copy just the latest frozen note and its hashed JPEGs.
        source = Path(source).expanduser().resolve()
        note = notes.load_note(source)
        jid, attempt = str(uuid.uuid4()), str(uuid.uuid4())
        folder = self.jobs / jid
        (folder / "run/notes/assets").mkdir(parents=True, mode=0o700)
        directory = folder / "run"
        revision = note["revision"]
        try:
            for frame in note["snapshot"]["frames"]:
                asset = notes.frame_asset(source, note, frame["id"])
                target = _contained(directory / frame["asset"], directory / "notes/assets")
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(asset, target)
            calls.durable_save(directory / "notes" / f"note-v{revision:03d}.json", note)
            make_artifacts(directory)
            calls.durable_save(folder / "job.json", {"schema_version": 1, "id": jid, "submission_id": str(uuid.uuid4()),
                "spec": {"url": note["snapshot"]["source"].get("canonical_url")}, "created_at": now(), "attempt": attempt, "imported": True})
            calls.durable_save(folder / (attempt + ".json"), {"status": "completed", "message": "从已有冻结笔记导入；没有新增模型调用。"})
        except Exception:
            shutil.rmtree(folder)
            raise
        return jid


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, product, host="127.0.0.1", port=8765):
        if host not in ("127.0.0.1", "localhost"):
            raise ProductError("invalid_host", "本地版只监听 127.0.0.1 或 localhost。")
        self.product = product
        super().__init__((host, port), Handler)
        actual_port = self.server_address[1]
        self.hosts = {f"127.0.0.1:{actual_port}", f"localhost:{actual_port}"}
        self.origins = {"http://" + host for host in self.hosts}
        self.url = f"http://127.0.0.1:{actual_port}"


class Handler(BaseHTTPRequestHandler):
    server_version = "VideoStudyNotes"
    def log_message(self, *args):
        pass

    def _headers(self, status, mime, size):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")

    def _send(self, status, payload):
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
        self._headers(status, "application/json; charset=utf-8", len(encoded))
        self.end_headers()
        self.wfile.write(encoded)

    def _guard(self, *, post=False):
        if len(self.headers.get_all("Host", [])) != 1 or self.headers.get("Host") not in self.server.hosts:
            raise ProductError("bad_host", "请从本机地址打开页面。", 403)
        origin = self.headers.get("Origin")
        expected_origin = "http://" + self.headers["Host"]
        if len(self.headers.get_all("Origin", [])) > 1 or origin is not None and origin != expected_origin:
            raise ProductError("bad_origin", "请求来源不允许。", 403)
        if self.headers.get("Sec-Fetch-Site") in ("cross-site", "same-site"):
            raise ProductError("bad_origin", "请求来源不允许。", 403)
        if post and origin != expected_origin:
            raise ProductError("bad_origin", "提交需要来自本机页面。", 403)
        if self.path.startswith("/api/") and self.path != "/api/session":
            if len(self.headers.get_all("X-Video-Notes-Session", [])) != 1 or not secrets.compare_digest(self.headers.get("X-Video-Notes-Session", ""), self.server.product.session):
                raise ProductError("bad_session", "页面会话已失效，请刷新后重试。", 403)

    def _payload(self):
        if self.headers.get("Transfer-Encoding") is not None or len(self.headers.get_all("Content-Length", [])) != 1:
            raise ProductError("invalid_body", "提交内容长度无效。", 400)
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
            raise ProductError("invalid_body", "提交内容需要 JSON 格式。", 415)
        try:
            size = int(self.headers["Content-Length"])
        except (ValueError, TypeError):
            raise ProductError("invalid_body", "提交内容长度无效。") from None
        if not 0 < size <= MAX_BODY:
            raise ProductError("body_too_large", "提交内容超过允许大小。", 413)
        self.connection.settimeout(10)
        data = self.rfile.read(size)
        if len(data) != size:
            raise ProductError("invalid_body", "提交内容不完整。")
        try:
            return json.loads(data, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, UnicodeError):
            raise ProductError("invalid_body", "提交 JSON 内容无效。") from None

    def do_GET(self):
        self._dispatch(False)

    def do_POST(self):
        self._dispatch(True)

    def _dispatch(self, post):
        try:
            self._guard(post=post)
            if "?" in self.path or "#" in self.path or "%" in self.path:
                raise ProductError("not_found", "页面不存在。", 404)
            product = self.server.product
            if post:
                payload = self._payload()
                if self.path == "/api/jobs":
                    view, created = product.submit(payload)
                    self._send(201 if created else 200, view)
                    return
                match = re.fullmatch(r"/api/jobs/([0-9a-f-]+)/resume", self.path)
                if match:
                    self._send(202, product.resume(match[1], payload))
                    return
            else:
                if self.path == "/api/session":
                    self._send(200, {"csrf_token": product.session, "version": VERSION, "readiness": readiness()})
                    return
                if self.path == "/api/jobs":
                    self._send(200, {"jobs": product.history()})
                    return
                match = re.fullmatch(r"/api/jobs/([0-9a-f-]+)", self.path)
                if match:
                    self._send(200, product.view(match[1]))
                    return
                match = re.fullmatch(r"/api/jobs/([0-9a-f-]+)/artifacts/([a-z-]+)", self.path)
                if match:
                    path, mime = product.artifact(*match.groups())
                    data = path.read_bytes()
                    self._headers(200, mime, len(data))
                    self.send_header("Content-Disposition", 'attachment; filename="' + path.name + '"')
                    self.send_header("Content-Security-Policy", "sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'")
                    self.end_headers()
                    self.wfile.write(data)
                    return
                static = {"/": ("index.html", "text/html; charset=utf-8"), "/app.css": ("app.css", "text/css; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8")}
                if self.path in static:
                    filename, mime = static[self.path]
                    data = (WEB / filename).read_bytes()
                    self._headers(200, mime, len(data))
                    self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src data:; frame-src 'self' blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
                    self.end_headers()
                    self.wfile.write(data)
                    return
            raise ProductError("not_found", "页面不存在。", 404)
        except ProductError as error:
            self._send(error.status, {"error": {"code": error.code, "message": str(error)}})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self._send(500, {"error": {"code": "internal_error", "message": "本地服务暂时无法处理请求，请刷新页面或检查启动环境。"}})


def main(argv=None):
    parser = argparse.ArgumentParser(description="Start the independent local video learning web app")
    parser.add_argument("--host", choices=("127.0.0.1", "localhost"), default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", default=str(Path.home() / ".video-study-notes"))
    parser.add_argument("--open", action="store_true")
    parser.add_argument("--import-run", action="append", default=[])
    parser.add_argument("--rednote-skill", help="Optional external read-only RedNote skill directory; uses its existing visible main session")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("port must be between 0 and 65535")
    product = None
    try:
        product = Product(args.data_dir, rednote_skill=args.rednote_skill)
        for path in args.import_run:
            product.import_run(path)
        with LocalServer(product, args.host, args.port) as server:
            print("Video Study Notes: " + server.url, flush=True)
            if args.open:
                webbrowser.open(server.url)
            server.serve_forever(poll_interval=.5)
    except KeyboardInterrupt:
        return 0
    except (ProductError, OSError, ValueError):
        print("本地服务启动失败。请检查端口、数据目录和导入笔记，或确认没有其他服务使用同一数据目录。", file=sys.stderr)
        return 1
    finally:
        if product is not None:
            product.close()
    return 0
