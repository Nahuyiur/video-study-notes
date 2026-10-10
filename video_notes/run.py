"""Shared data contracts and bounded lecture planning; no model/API calls."""
from __future__ import annotations

import json
import math
import re
import time
import uuid
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .locking import file_lock

PRESETS = {
    "economy": {"text_chars": 12000, "overview_frames": 24, "detail_frames": 6, "read_batches": 4},
    "standard": {"text_chars": 24000, "overview_frames": 48, "detail_frames": 12, "read_batches": 8},
}


def now():
    return datetime.now(timezone.utc).isoformat()


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def stamp(second):
    second = max(0, int(second))
    return f"{second // 3600:02d}:{second % 3600 // 60:02d}:{second % 60:02d}"


def segments_from(value):
    if isinstance(value, list):
        rows = value
    elif "content" in value:
        rows = value["content"].get("segments", [])
    else:
        rows = value.get("segments", value.get("body", []))
    result = []
    for row in rows:
        start, end = float(row.get("start", row.get("from", 0))), float(row.get("end", row.get("to", 0)))
        text = str(row.get("text", row.get("content", ""))).strip()
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end < start:
            raise ValueError("Invalid transcript timestamp")
        if text:
            result.append({"start": start, "end": end, "text": text})
    return sorted(result, key=lambda row: (row["start"], row["end"]))


def selected_duration(source):
    page = source.get("selection", {}).get("page", 1)
    for item in source.get("metadata", {}).get("pages", []):
        if item.get("page") == page and item.get("duration_seconds"):
            return float(item["duration_seconds"])
    return float(source.get("metadata", {}).get("duration_seconds") or 0)


def bounded_segments(rows, start, end, cap):
    chosen, used, processed_end = [], 0, end
    for row in rows:
        if row["end"] <= start or row["start"] >= end:
            continue
        cost = len(row["text"]) + 34  # Includes printed timestamps and separators.
        if used + cost > cap:
            processed_end = max(start, min(end, row["start"]))
            break
        chosen.append(row)
        used += cost
    if rows and not chosen and processed_end < end:
        raise ValueError("A transcript segment exceeds this budget; split it before preparing")
    clipped = [{**r, "start": max(start, r["start"]), "end": min(processed_end, r["end"])}
               for r in chosen if min(processed_end, r["end"]) > max(start, r["start"])]
    return clipped, processed_end


def offset_segments(rows, start, end):
    return [{**r, "start": max(start, r["start"] + start), "end": min(end, r["end"] + start)}
            for r in rows if r["start"] + start < end and r["end"] > 0]


def transcript_coverage(rows, start, end):
    intervals = sorted((max(start, r["start"]), min(end, r["end"])) for r in rows if r["end"] > start and r["start"] < end)
    total, last = 0.0, start
    for left, right in intervals:
        total += max(0.0, right - max(left, last))
        last = max(last, right)
    return round(total, 3)


def sample_times(start, end, count):
    if end <= start or count < 1:
        raise ValueError("Invalid frame interval/count")
    # Centers of bins cover the entire requested interval, including the final bin.
    return sorted(set(round(start + (i + .5) * (end - start) / count, 3) for i in range(count)))


def nearby(rows, second, radius=10):
    return [r for r in rows if r["end"] >= second - radius and r["start"] <= second + radius]


def text_estimate(text):
    cjk = len(re.findall(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]", text))
    other = re.sub(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]", "", text)
    return cjk + math.ceil(len(other.encode("utf-8")) / 4)


def open_run(directory):
    directory = Path(directory).resolve()
    run = load(directory / "run.json")
    return directory, run


def ensure_open(run):
    if run.get("api_pending"):
        raise ValueError("An API request has an unknown outcome; resume the API task before changing or finishing this run")
    if run.get("status") == "finished":
        raise ValueError("Run is finished; start a new run for further reading")


@contextmanager
def run_lock(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        try:
            stack.enter_context(file_lock(directory / ".run.lock", blocking=False))
        except BlockingIOError:
            raise RuntimeError("Another command owns this run; retry after it finishes") from None
        yield


@contextmanager
def activity(directory, command):
    started, tick, status = now(), time.monotonic(), "failed"
    try:
        yield
        status = "completed"
    finally:
        row = {"schema_version": 1, "event_id": str(uuid.uuid4()), "command": command,
               "started_at": started, "finished_at": now(), "status": status,
               "elapsed_seconds": round(time.monotonic() - tick, 3),
               "native_tokens": None, "api_usd": None,
               "observed_scope": "local command wall time; excludes Agent reading, synthesis and conversation"}
        with (Path(directory) / "activities.jsonl").open("a") as output:
            output.write(json.dumps(row, ensure_ascii=False) + "\n")
