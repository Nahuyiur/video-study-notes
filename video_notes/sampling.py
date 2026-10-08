"""Bounded local visual-change candidates, independent of model reading."""
from __future__ import annotations

import math
from pathlib import Path

from .run import sample_times


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
    step = max(step, (end - start) / max_candidates)
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
