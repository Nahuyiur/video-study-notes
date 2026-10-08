"""Portable Markdown with relative, content-addressed assets; no media/model calls."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from urllib.parse import quote

from ..notes import frame_asset, safe_url, scope_text, usage_explanations, usage_metrics, validate_note
from ..run import stamp
from ..sources import timestamp_url


def esc(value):
    value = str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"([\\`*_{}\[\]()#+.!|$~-])", r"\\\1", value)


def link(label, url):
    # Parentheses, quotes and whitespace cannot terminate a Markdown destination.
    return f"[{esc(label)}]({quote(safe_url(url), safe=':/?=&%#@+;,')})"


def _evidence(note, refs):
    snapshot = note["snapshot"]
    url = snapshot["source"].get("canonical_url")
    times = {"segment:" + row["id"]: row["start"] for row in snapshot["segments"]}
    times.update({"frame:" + row["id"]: row["timestamp"] for row in snapshot["frames"]})
    entries = []
    for ref in refs:
        second = times[ref]
        label = f"{ref} · {stamp(second)}"
        entries.append(link(label, timestamp_url(url, second)) if url else esc(label))
    return "证据：" + " · ".join(entries) if entries else ""


def _fenced(value, language=""):
    width = max([len(match) for match in re.findall(r"`+", value)] + [2]) + 1
    fence = "`" * width
    return f"{fence}{language}\n{value}\n{fence}"


def _figure(directory, note, row, output, text_only):
    frame = next(f for f in note["snapshot"]["frames"] if f["id"] == row["frame_id"])
    title = row.get("title") or frame["id"]
    parts = [f"**{esc(title)}**"]
    if not text_only:
        source = frame_asset(directory, note, frame["id"])
        folder = output.parent / (output.stem + "-assets")
        if folder.is_symlink():
            raise ValueError("Export assets folder cannot be a symlink")
        folder.mkdir(parents=True, exist_ok=True)
        asset = folder / f"{frame['sha256']}.jpg"
        if asset.is_symlink():
            raise ValueError("Export asset cannot be a symlink")
        payload = source.read_bytes()
        if asset.exists() and hashlib.sha256(asset.read_bytes()).hexdigest() != frame["sha256"]:
            raise ValueError("Export asset hash differs")
        if not asset.exists():
            asset.write_bytes(payload)
        relative = asset.relative_to(output.parent).as_posix()
        parts.append(f"![{esc(title)}]({quote(relative, safe='/')})")
    else:
        parts.append("（文字版省略图片）")
    if row.get("caption"):
        parts.append(esc(row["caption"]))
    url = note["snapshot"]["source"].get("canonical_url")
    moment = stamp(frame["timestamp"])
    parts.append(link(moment + " · 返回视频", timestamp_url(url, frame["timestamp"])) if url else moment)
    if row.get("evidence_refs"):
        parts.append(_evidence(note, row["evidence_refs"]))
    return "\n\n".join(parts)


def _block(directory, note, row, output, text_only):
    kind = row["type"]
    if kind == "paragraph":
        body = esc(row["text"])
    elif kind == "list":
        body = "\n".join(f"{index}. {esc(item)}" if row["ordered"] else f"- {esc(item)}"
                         for index, item in enumerate(row["items"], 1))
    elif kind == "code":
        body = _fenced(row["text"], row["language"])
    elif kind == "formula":
        # A fenced math source remains readable even in viewers without equation plugins.
        body = _fenced(row["text"], "math")
    elif kind == "table":
        clean = lambda cell: esc(cell).replace("\n", " ").replace("\r", " ")
        lines = ["| " + " | ".join(clean(c) for c in row["headers"]) + " |",
                 "| " + " | ".join("---" for _ in row["headers"]) + " |"]
        lines.extend("| " + " | ".join(clean(c) for c in cells) + " |" for cells in row["rows"])
        body = "\n".join(lines)
    else:
        return _figure(directory, note, row, output, text_only)
    if row["attribution"] != "speaker":
        body += "\n\n" + ("Agent 解读" if row["attribution"] == "agent" else "尚不确定")
    evidence = _evidence(note, row["evidence_refs"])
    return body + ("\n\n" + evidence if evidence else "")


def render_note(directory, note, output, *, text_only=False):
    """Return deterministic Markdown, copying only this note's read image assets."""
    validate_note(note)
    directory, output = Path(directory).resolve(), Path(output).resolve()
    parts = ["# " + esc(note["title"])]
    if note.get("subtitle"):
        parts.append(esc(note["subtitle"]))
    parts.extend(["## 核心总结", esc(note["takeaway"])])
    if note["takeaway_evidence_refs"]:
        parts.append(_evidence(note, note["takeaway_evidence_refs"]))
    for section in note["sections"]:
        parts.append("## " + esc(section["title"]))
        parts.extend(_block(directory, note, row, output, text_only) for row in section["blocks"])
        if section["evidence_refs"]:
            parts.append(_evidence(note, section["evidence_refs"]))
    url = note["snapshot"]["source"].get("canonical_url")
    if note["timeline"]:
        parts.append("## 视频时间轴")
        for row in note["timeline"]:
            time = f"{stamp(row['start'])}–{stamp(row['end'])}"
            label = link(time, timestamp_url(url, row["start"])) if url else time
            parts.extend(["### " + esc(row["title"]), label])
            if row["text"]:
                parts.append(esc(row["text"]))
            if row["evidence_refs"]:
                parts.append(_evidence(note, row["evidence_refs"]))
    if note["figures"]:
        parts.append("## 关键画面与解释")
        parts.extend(_figure(directory, note, row, output, text_only) for row in note["figures"])
    parts.extend(["## 范围与边界", esc(scope_text(note))])
    if note["caveats"]:
        parts.append("\n".join("- " + esc(item) for item in note["caveats"]))
    parts.append("## 本次用量")
    parts.append("\n".join(f"- {esc(label)}：{esc(value)}" for label, value in usage_metrics(note)))
    parts.extend(esc(line) for line in usage_explanations(note))
    sources = ([{"label": "原视频", "url": url}] if url else []) + note["sources"]
    if sources:
        parts.extend(["## 来源", "\n".join("- " + link(row["label"], row["url"]) for row in sources)])
    parts.append(f"笔记版本：{note.get('revision', 'preview')} · {note.get('content_hash', '')[:12]}")
    return "\n\n".join(part for part in parts if part) + "\n"
