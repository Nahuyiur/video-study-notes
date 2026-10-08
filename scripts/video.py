"""Acquire Bilibili/local materials and timestamped frames without paid AI calls."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import urlencode

from core import (PRESETS, bounded_segments, ensure_open, load, nearby, now,
                  offset_segments, open_run, sample_times, save, segments_from, selected_duration,
                  stamp, transcript_coverage)

UPSTREAM = Path(__file__).parent / "upstream"
spec = importlib.util.spec_from_file_location("bililens_extractor", UPSTREAM / "bilibili_extract.py")
bili = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bili)


def execute(command, timeout=90):
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Media command timed out; retry once or use a local media file") from None
    if result.returncode:
        # ffmpeg errors can contain signed URLs or cookies. Do not echo them.
        raise RuntimeError("Media command failed; check access/FFmpeg or supply a local media file")
    return result.stdout


def probe(path):
    return float(execute(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)], 20).strip())


def native_extract(video, page, use_cookie):
    args = argparse.Namespace(video=video, page=page, subtitle_language=None,
                              include_danmaku=False, danmaku_limit=0,
                              use_env_cookie=use_cookie, retries=2)
    return asyncio.run(bili.extract(args))


def prepare(args):
    directory = Path(args.out).resolve()
    if (directory / "run.json").exists():
        raise ValueError("Run already exists; use its cached materials or choose a new output directory")
    if args.source_json:
        source = load(args.source_json)
    elif Path(args.video).is_file():
        source = {"source": {"platform": "local", "canonical_url": None},
                  "metadata": {"title": Path(args.video).stem, "duration_seconds": probe(args.video)},
                  "selection": {"page": 1}, "content": {"source_type": "none", "segments": []}}
    else:
        source = native_extract(args.video, args.page, args.use_env_cookie)
    if args.transcript:
        source["content"] = {"source_type": "provided_transcript", "segments": segments_from(load(args.transcript))}
    rows = segments_from(source)
    duration = selected_duration(source)
    if duration <= 0:
        raise ValueError("Source duration is missing; supply a valid source or local media")
    end = duration if args.end is None else min(args.end, duration)
    if not 0 <= args.start < end:
        raise ValueError("Processing interval must be within the selected part")
    budget = PRESETS[args.preset].copy()
    chosen, processed_end = bounded_segments(rows, args.start, end, int(budget["text_chars"] * .8))
    directory.mkdir(parents=True, exist_ok=True)
    save(directory / "source.json", source)
    run = {"schema_version": 1, "run_id": str(uuid.uuid4()), "started_at": now(), "status": "prepared",
           "video": str(Path(args.video).resolve()) if Path(args.video).is_file() else args.video,
           "local_media": str(Path(args.video).resolve()) if Path(args.video).is_file() else None,
           "preset": args.preset, "budget": budget,
           "video_duration_seconds": duration,
           "requested_range": [args.start, end], "processed_range": [args.start, processed_end],
           "remaining_range": [processed_end, end] if processed_end < end else None,
           "content_source": source.get("content", {}).get("source_type", "none"),
           "transcript_coverage_seconds": transcript_coverage(chosen, args.start, processed_end),
           "transcript_status": "ready" if chosen else "unavailable", "frames": [], "packs": [],
           "api_calls": [], "read_text_chars_reserved": 0, "session_log": args.session_log,
           "ledger": getattr(args, "ledger", None),
           "authenticated": bool(source.get("source", {}).get("authenticated"))}
    from usage import counter_snapshot
    run["native_counter_before"] = counter_snapshot(args.session_log)
    save(directory / "segments.json", chosen)
    save(directory / "run.json", run)
    print(json.dumps({"run": str(directory), "title": source["metadata"].get("title"),
                      "part": source.get("selection"), "duration_seconds": duration,
                      "processed_range": run["processed_range"], "remaining_range": run["remaining_range"],
                      "transcript_status": run["transcript_status"], "budget": budget}, ensure_ascii=False))


def media_input(directory, run, kind, use_cookie=False, override=None, height=720):
    if override:
        if not Path(override).is_file():
            raise ValueError("--media must be a local file")
        return str(Path(override).resolve()), []
    if run.get("local_media"):
        return run["local_media"], []
    source = load(directory / "source.json")
    client = bili.BilibiliClient(use_env_cookie=use_cookie, retries=2)
    query = urlencode({"bvid": source["metadata"]["bvid"], "cid": source["selection"]["cid"], "fnval": 16, "qn": 80 if height > 720 else 64})
    data = client.get_json(f"https://api.bilibili.com/x/player/playurl?{query}", "media", source["source"]["canonical_url"])["data"]
    streams = data.get("dash", {}).get(kind, [])
    if not streams:
        raise RuntimeError("No accessible DASH stream; supply a local media file")
    if kind == "video":
        suitable = [s for s in streams if s.get("height", 9999) <= height]
        stream = max(suitable, key=lambda s: (s.get("height", 0), str(s.get("codecs", "")).startswith("avc"))) if suitable else min(streams, key=lambda s: s.get("height", 9999))
    else:
        stream = min(streams, key=lambda s: s.get("bandwidth", 0))
    url = stream.get("baseUrl") or stream.get("base_url")
    if not url:
        raise RuntimeError("Media stream contains no playable URL")
    # Authentication is only for the Bilibili API; never forward session cookies to a CDN.
    headers = f"Referer: {source['source']['canonical_url']}\r\nUser-Agent: {bili.USER_AGENT}\r\n"
    return url, ["-headers", headers, "-rw_timeout", "15000000"]


def frames(args):
    directory, run = open_run(args.run)
    ensure_open(run)
    if not shutil.which("ffmpeg"):
        raise RuntimeError("FFmpeg is required for frame extraction")
    start, end = run["processed_range"]
    cap = run["budget"][args.kind + "_frames"]
    existing = [f for f in run["frames"] if f["kind"] == args.kind]
    count = min(cap, max(3, int((end - start + 119) // 120)))
    times = [float(t) for t in args.times.split(",")] if args.times else sample_times(start, end, count)
    if args.kind == "detail" and not args.times:
        raise ValueError("Detail frames need explicit timestamps selected after overview review")
    if any(not start <= t < end for t in times):
        raise ValueError("Frame timestamp is outside the processed range")
    attempted = run.setdefault("sampling_history", {}).setdefault(args.kind, [f["timestamp"] for f in existing])
    new_times = sorted(set(round(t, 3) for t in times) - set(attempted))
    if len(attempted) + len(new_times) > cap:
        raise ValueError(f"{args.kind} frame budget exceeded ({cap})")
    if not new_times:
        print(json.dumps({"cached": True, "frames": [
            {"id": f["id"], "timestamp": f["timestamp"], "path": f["path"]}
            for f in existing]}, ensure_ascii=False)); return
    media, options = media_input(directory, run, "video", args.use_env_cookie, args.media,
                                 height=1080 if args.kind == "detail" else 720)
    rows = load(directory / "segments.json")
    output = directory / "frames"; output.mkdir(exist_ok=True)
    kept_before = len(run["frames"])
    for second in new_times:
        identifier = f"f{len(run['frames']) + 1:04d}"
        path = output / f"{identifier}_{args.kind}.jpg"
        width = 640 if args.kind == "overview" else 1440
        execute(["ffmpeg", "-hide_banner", "-loglevel", "error", *options, "-ss", str(second), "-i", media,
                 "-frames:v", "1", "-vf", f"scale='min({width},iw)':-2", "-q:v", "3", "-y", str(path)])
        if not path.is_file() or path.stat().st_size < 100:
            raise RuntimeError("Extraction produced no valid image")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        attempted.append(second)
        if args.kind == "overview" and any(f.get("sha256") == digest and f["kind"] == "overview" for f in run["frames"]):
            path.unlink()
            run["overview_exact_duplicates_skipped"] = run.get("overview_exact_duplicates_skipped", 0) + 1
            save(directory / "run.json", run)
            continue
        run["frames"].append({"id": identifier, "kind": args.kind, "timestamp": second,
                              "path": str(path), "sha256": digest, "nearby_segments": nearby(rows, second)})
        save(directory / "run.json", run)  # Resume after a failed network frame.
    print(json.dumps({"kind": args.kind, "sampled": len(new_times), "extracted": len(run["frames"]) - kept_before,
                      "frames": [{"id": f["id"], "timestamp": f["timestamp"], "path": f["path"]} for f in run["frames"] if f["kind"] == args.kind]}, ensure_ascii=False))


def transcribe(args):
    directory, run = open_run(args.run)
    ensure_open(run)
    if run["packs"]:
        raise ValueError("Transcribe before reserving any reading packs")
    if run["transcript_status"] == "ready":
        raise ValueError("A transcript is already available; ASR is unnecessary")
    if not shutil.which("uv"):
        raise RuntimeError("ASR fallback needs uv and its isolated faster-whisper runtime")
    start, end = run["requested_range"]
    # A single invocation never starts multi-hour ASR. Continue in later scoped runs.
    end = min(end, start + 1800)
    audio = directory / "asr-audio.wav"
    if not audio.exists() or abs(probe(audio) - (end - start)) > 1:
        media, options = media_input(directory, run, "audio", args.use_env_cookie, args.media)
        execute(["ffmpeg", "-hide_banner", "-loglevel", "error", *options, "-ss", str(start), "-i", media,
                 "-t", str(end - start), "-vn", "-ar", "16000", "-ac", "1", "-y", str(audio)], 300)
    raw = directory / "asr.json"
    execute(["uv", "run", "--with-requirements", str(Path(__file__).parent / "requirements-asr.txt"),
             "python", str(UPSTREAM / "transcribe_faster_whisper.py"),
             str(audio), "--model", args.model, "--language", args.language, "--json", str(raw)], 1800)
    transcript = load(raw)
    rows = offset_segments(segments_from(transcript), start, end)
    chosen, processed_end = bounded_segments(rows, start, end, int(run["budget"]["text_chars"] * .8))
    source = load(directory / "source.json")
    source["content"] = {"source_type": "asr", "language": transcript.get("language"), "segments": rows}
    save(directory / "source.json", source); save(directory / "segments.json", chosen)
    run.update({"content_source": "asr", "transcript_status": "ready" if chosen else "unavailable",
                "asr_model": args.model, "asr_runtime": {"faster-whisper": "1.2.1", "av": "18.0.0", "ctranslate2": "4.8.2"},
                "processed_range": [start, processed_end],
                "remaining_range": [processed_end, run["requested_range"][1]] if processed_end < run["requested_range"][1] else None,
                "transcript_coverage_seconds": transcript_coverage(chosen, start, processed_end)})
    save(directory / "run.json", run)
    audio.unlink(missing_ok=True)
    print(json.dumps({"status": run["transcript_status"], "processed_range": run["processed_range"], "remaining_range": run["remaining_range"]}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare"); p.add_argument("--video", required=True); p.add_argument("--out", required=True)
    p.add_argument("--source-json"); p.add_argument("--transcript"); p.add_argument("--page", type=int)
    p.add_argument("--preset", choices=PRESETS, default="economy"); p.add_argument("--start", type=float, default=0)
    p.add_argument("--end", type=float); p.add_argument("--session-log"); p.add_argument("--ledger")
    p.add_argument("--use-env-cookie", action="store_true")
    p.set_defaults(func=prepare)
    p = sub.add_parser("frames"); p.add_argument("--run", required=True); p.add_argument("--kind", choices=("overview", "detail"), default="overview")
    p.add_argument("--times"); p.add_argument("--media"); p.add_argument("--use-env-cookie", action="store_true"); p.set_defaults(func=frames)
    p = sub.add_parser("transcribe"); p.add_argument("--run", required=True); p.add_argument("--media")
    p.add_argument("--model", choices=("base", "small"), default="small"); p.add_argument("--language", default="auto")
    p.add_argument("--use-env-cookie", action="store_true"); p.set_defaults(func=transcribe)
    args = parser.parse_args()
    started = now()
    try:
        args.func(args)
    except (ValueError, RuntimeError, OSError, KeyError, bili.ParserError) as exc:
        message = str(exc)
        if isinstance(exc, bili.ParserError):
            message = f"Bilibili {exc.stage} retrieval failed; check access or supply local materials"
        if args.command == "prepare":
            record_failed_prepare(args, started, message)
        print(json.dumps({"ok": False, "message": message}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0


def record_failed_prepare(args, started, message):
    """A retrieval failure still gets a usage row, without inventing duration."""
    directory = Path(args.out).resolve()
    if (directory / "run.json").exists():
        return  # Never overwrite an existing run on an accidental repeat.
    directory.mkdir(parents=True, exist_ok=True)
    if not (directory / "source.json").exists():
        save(directory / "source.json", {"source": {"canonical_url": None}, "metadata": {"title": None}, "selection": {"page": args.page}})
    save(directory / "segments.json", [])
    save(directory / "run.json", {"schema_version": 1, "run_id": str(uuid.uuid4()), "started_at": started,
         "status": "prepare_failed", "video": args.video, "preset": args.preset, "budget": PRESETS[args.preset],
         "video_duration_seconds": None, "requested_range": [args.start, args.end], "processed_range": [args.start, args.start],
         "remaining_range": None, "content_source": "none", "transcript_status": "unavailable",
         "transcript_coverage_seconds": 0, "frames": [], "packs": [], "api_calls": [],
         "ledger": getattr(args, "ledger", None), "session_log": args.session_log, "native_counter_before": None})
    save(directory / "prepare_error.json", {"message": message})
    from usage import finish
    finish(argparse.Namespace(run=str(directory), ledger=getattr(args, "ledger", None), read_packs="", status="failed",
                              native_usage=None, quota_before=None, quota_after=None))


if __name__ == "__main__":
    raise SystemExit(main())
