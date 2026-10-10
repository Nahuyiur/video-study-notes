"""Bounded local visual-change candidates, independent of model reading."""
from __future__ import annotations

import math
from pathlib import Path

from .run import sample_times

STRATEGIES = ("hybrid", "slides", "uniform")


def overview_target(start, end, cap):
    if (isinstance(cap, bool) or not isinstance(cap, int) or cap < 1
            or not math.isfinite(start) or not math.isfinite(end) or end <= start):
        raise ValueError("Invalid sampling scope or cap")
    return min(cap, max(6, math.ceil((end - start) / 20)))


def _bounded_uniform(start, end, count):
    left, right = math.ceil(start * 1000) / 1000, (math.ceil(end * 1000) - 1) / 1000
    if right < left:
        raise ValueError("Sampling scope has no millisecond timestamp")
    return sorted({min(right, max(left, t)) for t in sample_times(start, end, count)})


def select_hybrid(candidates, start, end, cap):
    target = overview_target(start, end, cap)
    anchors = _bounded_uniform(start, end, (target + 1) // 2)
    selected = [{"timestamp": t, "reason": "uniform_anchor"} for t in anchors]
    separation = min(5, (end - start) / (2 * target))
    slots = target - len(selected)
    for index in range(slots):
        left, right = start + index * (end - start) / slots, start + (index + 1) * (end - start) / slots
        options = [c for c in candidates if math.isfinite(c["timestamp"])
                   and left <= c["timestamp"] < right and start <= round(c["timestamp"], 3) < end
                   and all(abs(round(c["timestamp"], 3) - row["timestamp"]) >= separation for row in selected)]
        if options:
            choice = max(options, key=lambda c: (c.get("score", 0), -abs(c["timestamp"] - (left + right) / 2),
                                                -c["timestamp"], c["reason"]))
            selected.append({"timestamp": round(choice["timestamp"], 3), "reason": choice["reason"]})
    while len(selected) < target:
        times = [start, *sorted(row["timestamp"] for row in selected), end]
        options = {millisecond / 1000 for left, right in zip(times, times[1:])
                   for millisecond in (math.floor((left + right) * 500), math.ceil((left + right) * 500))}
        options = [t for t in options if start <= t < end
                   and all(abs(t - row["timestamp"]) >= separation for row in selected)]
        if not options:
            break
        chosen = max(options, key=lambda t: (min(abs(t - row["timestamp"]) for row in selected), -t))
        selected.append({"timestamp": chosen, "reason": "uniform_supplement"})
    return sorted(selected, key=lambda row: row["timestamp"])


def select_overview(candidates, start, end, cap, strategy):
    if strategy == "hybrid":
        return select_hybrid(candidates, start, end, cap)
    if strategy == "slides":
        return select_candidates(candidates, start, end, cap)
    if strategy == "uniform":
        count = min(cap, max(3, int((end - start + 119) // 120)))
        return [{"timestamp": t, "reason": "uniform"} for t in sample_times(start, end, count)]
    raise ValueError("Unknown sampling strategy")


def change(left, right):
    from PIL import ImageChops, ImageStat
    return sum(ImageStat.Stat(ImageChops.difference(left, right)).mean) / (255 * len(left.getbands()))


def scene_candidates(paths, start, step, threshold=.035, stability=.02, timestamps=None):
    from PIL import Image
    pictures = []
    for path in paths:
        with Image.open(path) as picture:
            pictures.append(picture.convert("RGB").resize((96, 54)))
    result = []
    baseline = pictures[0] if pictures else None
    for index in range(1, len(pictures) - 1):
        score = change(baseline, pictures[index])
        if score >= threshold and change(pictures[index], pictures[index + 1]) <= stability:
            result.append({"timestamp": round(timestamps[index] if timestamps else start + index * step, 3), "reason": "stable_visual_change",
                           "score": round(score, 5)})
            baseline = pictures[index]
    return result


def select_candidates(candidates, start, end, cap):
    if not 0 < cap or not math.isfinite(start + end) or end <= start:
        raise ValueError("Invalid sampling scope or cap")
    anchors = sample_times(start, end, cap)
    selected = []
    width = (end - start) / cap
    for index, anchor in enumerate(anchors):
        left, right = start + index * width, start + (index + 1) * width
        options = [c for c in candidates if left <= c["timestamp"] < right]
        if options:
            choice = max(options, key=lambda c: (c.get("score", 0), -abs(c["timestamp"] - anchor)))
            selected.append({"timestamp": choice["timestamp"], "reason": choice["reason"]})
        else:
            selected.append({"timestamp": anchor, "reason": "uniform_fallback"})
    return selected


def scan(directory, media, options, start, end, *, max_candidates=180, step=5):
    from .materials import execute
    if end - start > 1800:
        raise ValueError("Candidate scan is bounded to 30 minutes per run")
    max_candidates = min(180, max_candidates)
    step = max(5, step, (end - start) / max_candidates)
    folder = Path(directory) / "sampling-candidates"
    folder.mkdir(exist_ok=True)
    for old in folder.glob("c*.jpg"):
        old.unlink()
    execute(["ffmpeg", "-hide_banner", "-loglevel", "error", *options,
             "-ss", str(start), "-i", media, "-t", str(end - start),
             "-vf", f"select='isnan(prev_selected_t)+gte(t-prev_selected_t,{step})',scale=192:-2",
             "-fps_mode", "vfr", "-enc_time_base", "1/1000", "-frame_pts", "1", "-frames:v", str(max_candidates),
             "-q:v", "6", "-y", str(folder / "c%04d.jpg")], timeout=300)
    paths = sorted(folder.glob("c*.jpg"))
    if not paths:
        raise RuntimeError("Candidate scan produced no frames")
    paths.sort(key=lambda p: int(p.stem[1:]))
    timestamps = [start + int(p.stem[1:]) / 1000 for p in paths]
    candidates = scene_candidates(paths, start, step, timestamps=timestamps)
    return {"scan_range": [start, end], "step_seconds": step, "candidate_frames": len(paths),
            "candidates": candidates, "reader_frames": 0, "method": "pixel_change_stability"}
