"""Acquire source-neutral materials and timestamped frames without paid AI calls."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from .run import (PRESETS, bounded_segments, ensure_open, load, nearby, now,
                  offset_segments, open_run, save, segments_from, selected_duration,
                  stamp, transcript_coverage)
from .sampling import STRATEGIES, scan, select_overview

UPSTREAM = Path(__file__).resolve().parents[1] / "scripts/upstream"
from .sources import resolve_source, media_input as source_media, normalize_source, source_access, persistence_video

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


def prepare(args):
    directory = Path(args.out).resolve()
    if (directory / "run.json").exists():
        raise ValueError("Run already exists; use its cached materials or choose a new output directory")
    if args.source_json:
        source = normalize_source(load(args.source_json))
    else:
        source = resolve_source(args.video, args.page, args.use_env_cookie,
                                getattr(args, "language", None), getattr(args, "rednote_skill", None))
    if args.transcript:
        source["content"] = {"source_type": "provided_transcript", "segments": segments_from(load(args.transcript))}
    rows = segments_from(source)
    duration = selected_duration(source)
    if duration <= 0:
        raise ValueError("Source duration is missing; supply a valid source or local media")
    requested_end = duration if args.end is None else min(args.end, duration)
    end = min(requested_end, args.start + 1800)
    if not 0 <= args.start < end:
        raise ValueError("Processing interval must be within the selected part")
    budget = PRESETS[args.preset].copy()
    chosen, processed_end = bounded_segments(rows, args.start, end, int(budget["text_chars"] * .8))
    directory.mkdir(parents=True, exist_ok=True)
    save(directory / "source.json", source)
    run = {"schema_version": 2, "run_id": str(uuid.uuid4()), "started_at": now(), "status": "prepared",
           "video": (source["source"]["canonical_url"] if source["source"]["platform"] == "rednote" else
                     str(Path(args.video).resolve()) if Path(args.video).is_file() else args.video),
           "local_media": str(Path(args.video).resolve()) if Path(args.video).is_file() else None,
           "preset": args.preset, "budget": budget,
           "video_duration_seconds": duration,
           "requested_range": [args.start, requested_end], "processed_range": [args.start, processed_end],
           "remaining_range": [processed_end, requested_end] if processed_end < requested_end else None,
           "content_source": source.get("content", {}).get("source_type", "none"),
           "transcript_coverage_seconds": transcript_coverage(chosen, args.start, processed_end),
           "transcript_status": "ready" if chosen else "unavailable", "frames": [], "packs": [],
           "api_calls": [], "read_text_chars_reserved": 0, "session_log": args.session_log,
           "ledger": getattr(args, "ledger", None),
           "authenticated": bool(source.get("source", {}).get("authenticated"))}
    from .usage import counter_snapshot
    run["native_counter_before"] = counter_snapshot(args.session_log)
    chosen = [{**r, "id": r.get("id", f"s{i:04d}")} for i, r in enumerate(chosen, 1)]
    save(directory / "segments.json", chosen)
    save(directory / "run.json", run)
    print(json.dumps({"run": str(directory), "title": source["metadata"].get("title"),
                      "part": source.get("selection"), "duration_seconds": duration,
                      "processed_range": run["processed_range"], "remaining_range": run["remaining_range"],
                      "transcript_status": run["transcript_status"], "budget": budget}, ensure_ascii=False))


def media_input(directory, run, kind, use_cookie=False, override=None, height=720):
    return source_media(directory, run, kind, use_cookie, override, height)


def frames(args):
    directory, run = open_run(args.run)
    ensure_open(run)
    if not shutil.which("ffmpeg"):
        raise RuntimeError("FFmpeg is required for frame extraction")
    start, end = run["processed_range"]
    cap = run["budget"][args.kind + "_frames"]
    existing = [f for f in run["frames"] if f["kind"] == args.kind]
    strategy = getattr(args, "strategy", "hybrid")
    if strategy not in STRATEGIES:
        raise ValueError("Unknown sampling strategy")
    selected = []
    if args.kind == "overview" and not args.times:
        sampling = run.get("sampling", {})
        previous_strategy = sampling.get("strategy")
        if previous_strategy is None and (existing or run.get("sampling_history", {}).get("overview")):
            previous_strategy = "uniform"
        if previous_strategy is not None and previous_strategy != strategy:
            raise ValueError("Sampling strategy changed; create a new run or request explicit timestamps")
        if "selected" in sampling:
            if sampling["selection_range"] != [start, end]:
                raise ValueError("Sampling plan scope changed; create a new run")
            selected = sampling["selected"]
        else:
            plan = {}
            if strategy in ("slides", "hybrid"):
                plan_path = directory / "sampling-plan.json"
                if plan_path.exists():
                    plan = load(plan_path)
                    if plan["scan_range"] != [start, end]:
                        raise ValueError("Sampling plan scope changed; create a new run")
                else:
                    media, options = media_input(directory, run, "video", args.use_env_cookie, args.media, height=360)
                    plan = scan(directory, media, options, start, end)
                    save(plan_path, plan)
            selected = select_overview(plan.get("candidates", []), start, end, cap, strategy)
            run["sampling"] = {**{k: v for k, v in plan.items() if k != "candidates"},
                               "strategy": strategy, "selection_range": [start, end], "selected": selected}
        times = [r["timestamp"] for r in selected]
    else:
        times = [float(t) for t in args.times.split(",")] if args.times else []
    reasons = {r["timestamp"]: r["reason"] for r in selected}
    if args.kind == "detail" and not args.times:
        raise ValueError("Detail frames need explicit timestamps selected after overview review")
    times = sorted(set(round(t, 3) for t in times))
    if any(not start <= t < end for t in times):
        raise ValueError("Frame timestamp is outside the processed range")
    attempted = run.setdefault("sampling_history", {}).setdefault(args.kind, [f["timestamp"] for f in existing])
    new_times = sorted(set(round(t, 3) for t in times) - set(attempted))
    if len(attempted) + len(new_times) > cap:
        raise ValueError(f"{args.kind} frame budget exceeded ({cap})")
    if selected:
        save(directory / "run.json", run)
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
                              "path": str(path), "sha256": digest, "sampling_reason": reasons.get(second, "targeted_detail" if args.kind == "detail" else "uniform"), "nearby_segments": nearby(rows, second)})
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
    execute(["uv", "run", "--with-requirements", str(UPSTREAM.parent / "requirements-asr.txt"),
             "python", str(UPSTREAM / "transcribe_faster_whisper.py"),
             str(audio), "--model", args.model, "--language", args.language, "--json", str(raw)], 1800)
    transcript = load(raw)
    rows = offset_segments(segments_from(transcript), start, end)
    chosen, processed_end = bounded_segments(rows, start, end, int(run["budget"]["text_chars"] * .8))
    source = load(directory / "source.json")
    source["content"] = {"source_type": "asr", "language": transcript.get("language"), "segments": rows}
    chosen = [{**r, "id": r.get("id", f"s{i:04d}")} for i, r in enumerate(chosen, 1)]
    save(directory / "source.json", source); save(directory / "segments.json", chosen)
    run.update({"content_source": "asr", "transcript_status": "ready" if chosen else "unavailable",
                "asr_model": args.model, "asr_runtime": {"faster-whisper": "1.2.1", "av": "18.0.0", "ctranslate2": "4.8.2"},
                "processed_range": [start, processed_end],
                "remaining_range": [processed_end, run["requested_range"][1]] if processed_end < run["requested_range"][1] else None,
                "transcript_coverage_seconds": transcript_coverage(chosen, start, processed_end)})
    save(directory / "run.json", run)
    audio.unlink(missing_ok=True)
    print(json.dumps({"status": run["transcript_status"], "processed_range": run["processed_range"], "remaining_range": run["remaining_range"]}))


def continue_run(args):
    directory, previous = open_run(args.run)
    if not previous.get("remaining_range"):
        raise ValueError("This run has no remaining interval")
    if previous.get("status") != "finished":
        raise ValueError("Finish the previous run before continuing")
    start, end = previous["remaining_range"]
    prepare(argparse.Namespace(video=previous["video"], out=args.out,
        source_json=str(directory / "source.json"), transcript=None, page=None,
        start=start, end=end, preset=args.preset or previous["preset"],
        session_log=args.session_log, ledger=args.ledger or previous.get("ledger"),
        use_env_cookie=False))
    target = Path(args.out).resolve()
    run = load(target / "run.json")
    run["previous_run_id"] = previous["run_id"]
    save(target / "run.json", run)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare"); p.add_argument("--video", required=True); p.add_argument("--out", required=True)
    p.add_argument("--source-json"); p.add_argument("--transcript"); p.add_argument("--page", type=int)
    p.add_argument("--preset", choices=PRESETS, default="economy"); p.add_argument("--start", type=float, default=0)
    p.add_argument("--language"); p.add_argument("--end", type=float); p.add_argument("--session-log"); p.add_argument("--ledger")
    p.add_argument("--use-env-cookie", action="store_true")
    p.add_argument("--rednote-skill", help="Explicit external read-only RedNote skill directory")
    p.add_argument("--rednote-access-stdin", action="store_true", help="Read a fresh RedNote access URL from stdin JSON")
    p.set_defaults(func=prepare)
    p = sub.add_parser("frames"); p.add_argument("--run", required=True); p.add_argument("--kind", choices=("overview", "detail"), default="overview")
    p.add_argument("--strategy", choices=STRATEGIES, default="hybrid"); p.add_argument("--times"); p.add_argument("--media"); p.add_argument("--use-env-cookie", action="store_true"); p.set_defaults(func=frames)
    p.add_argument("--rednote-skill"); p.add_argument("--rednote-access-stdin", action="store_true")
    p = sub.add_parser("transcribe"); p.add_argument("--run", required=True); p.add_argument("--media")
    p.add_argument("--model", choices=("base", "small"), default="small"); p.add_argument("--language", default="auto")
    p.add_argument("--use-env-cookie", action="store_true"); p.set_defaults(func=transcribe)
    p.add_argument("--rednote-skill"); p.add_argument("--rednote-access-stdin", action="store_true")
    p = sub.add_parser("continue"); p.add_argument("--run", required=True); p.add_argument("--out", required=True)
    p.add_argument("--preset", choices=PRESETS); p.add_argument("--session-log"); p.add_argument("--ledger")
    p.set_defaults(func=continue_run)
    args = parser.parse_args(argv)
    started = now()
    try:
        access_url = None
        if getattr(args, "rednote_access_stdin", False):
            from .sources.rednote import stdin_access
            access_url = stdin_access()
        video = getattr(args, "video", None)
        from .sources.rednote import is_rednote
        from urllib.parse import urlsplit
        if video and is_rednote(video) and urlsplit(video).query:
            raise ValueError("RedNote access queries must be supplied through --rednote-access-stdin")
        with source_access(video, rednote_access_url=access_url, rednote_skill=getattr(args, "rednote_skill", None)):
            args.func(args)
    except (ValueError, RuntimeError, OSError, KeyError) as exc:
        message = str(exc)
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
    try:
        persisted_video = persistence_video(args.video)
    except ValueError:
        persisted_video = None
    directory.mkdir(parents=True, exist_ok=True)
    if not (directory / "source.json").exists():
        save(directory / "source.json", {"source": {"canonical_url": None}, "metadata": {"title": None}, "selection": {"page": args.page}})
    save(directory / "segments.json", [])
    save(directory / "run.json", {"schema_version": 2, "run_id": str(uuid.uuid4()), "started_at": started,
         "status": "prepare_failed", "video": persisted_video, "preset": args.preset, "budget": PRESETS[args.preset],
         "video_duration_seconds": None, "requested_range": [args.start, args.end], "processed_range": [args.start, args.start],
         "remaining_range": None, "content_source": "none", "transcript_status": "unavailable",
         "transcript_coverage_seconds": 0, "frames": [], "packs": [], "api_calls": [],
         "ledger": getattr(args, "ledger", None), "session_log": args.session_log, "native_counter_before": None})
    save(directory / "prepare_error.json", {"message": message})
    from .usage import finish
    finish(argparse.Namespace(run=str(directory), ledger=getattr(args, "ledger", None), read_packs="", status="failed",
                              native_usage=None, quota_before=None, quota_after=None))


if __name__ == "__main__":
    raise SystemExit(main())
