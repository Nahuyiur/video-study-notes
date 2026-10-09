"""Versioned StudyNotes bound to frozen, agent-declared reading evidence.

Saving and exporting are local operations. This module does not read media, call a
model, or establish that an explanation is supported merely because its IDs exist.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

from .locking import file_lock
from .run import load, save

SCHEMA_VERSION = 2
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
BLOCK_TYPES = {"paragraph", "list", "code", "formula", "table", "image"}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def safe_url(value):
    if not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 for c in value) or "\\" in value:
        raise ValueError("Source links must be plain HTTP(S) URLs")
    parts = urlsplit(value)
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("Invalid source URL port") from exc
    if parts.scheme not in ("https", "http") or not parts.hostname or parts.username or parts.password:
        raise ValueError("Source links must be HTTP(S) URLs without credentials")
    return value


def text(value, label, required=False):
    if not isinstance(value, str) or (required and not value.strip()):
        raise ValueError(f"{label} must be {'nonempty ' if required else ''}text")
    return value


def objects(value, label):
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError(f"{label} must be a list of objects")
    return value


def strings(value, label):
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list of text")
    return [text(item, label) for item in value]


def identifier(value, label):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ValueError(f"Invalid {label}")
    return value


def interval(run, start, end):
    left, right = run["processed_range"]
    if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x)
           for x in (start, end)) or not left <= start < end <= right:
        raise ValueError("Timeline entry is outside the processed interval")


def _source_snapshot(directory, usage):
    raw = load(directory / "source.json") if (directory / "source.json").exists() else {}
    source, meta, selected = raw.get("source", {}), raw.get("metadata", {}), raw.get("selection", {})
    url = source.get("canonical_url") or usage.get("video", {}).get("url")
    return {"platform": source.get("platform", "bilibili" if url and "bilibili.com" in url else "local"),
            "canonical_url": safe_url(url) if url else None,
            "title": meta.get("title") or usage.get("video", {}).get("title") or "视频",
            "media_id": str(meta.get("media_id") or meta.get("bvid") or "provided"),
            "part_id": str(selected.get("part_id") or selected.get("cid") or selected.get("page") or 1)}


def _usage_snapshot(usage):
    # No session files, local paths, raw provider receipts, authentication or media URLs.
    keys = ("schema_version", "run_id", "recorded_at", "elapsed_seconds", "preset", "result_status",
            "content_source", "transcript_status", "transcript_coverage_seconds", "materials", "tokens",
            "cost", "native_tokens_per_processed_minute")
    result = {key: copy.deepcopy(usage[key]) for key in keys if key in usage}
    result["video"] = {key: copy.deepcopy(usage.get("video", {}).get(key)) for key in
                       ("title", "url", "page", "duration_seconds", "processed_range", "processed_minutes", "remaining_range")}
    # Public counters retain provenance, never receipt contents or credential-bearing links.
    result["api_calls"] = [{key: copy.deepcopy(row[key]) for key in ("id", "stage", "model", "tokens", "cost", "call_id", "provider_response_id", "request_hash", "reading_method") if key in row}
                           for row in usage.get("api_calls", [])]
    return result


def _frame_path(directory, row):
    raw = Path(row["path"])
    path = (raw if raw.is_absolute() else directory / raw).resolve()
    if not path.is_relative_to(directory / "frames") or not path.is_file():
        raise ValueError("Frame path must stay inside this run's frames folder")
    return path


def create_snapshot(run_dir):
    """Freeze finished run facts; infer old overview declarations without inventing new reads."""
    directory = Path(run_dir).resolve()
    run, usage = load(directory / "run.json"), load(directory / "usage.json")
    if run.get("status") != "finished" or usage.get("run_id") != run.get("run_id"):
        raise ValueError("Finalize this run's matching usage before saving a note")
    scope = {key: copy.deepcopy(run.get(key)) for key in
             ("run_id", "processed_range", "remaining_range", "video_duration_seconds", "requested_range",
              "preset", "content_source", "transcript_status", "transcript_coverage_seconds", "finished_at")}
    scope["status"] = "finished"
    interval(scope, *scope["processed_range"])
    segment_rows = load(directory / "segments.json") if (directory / "segments.json").exists() else []
    segments = []
    seen = set()
    for index, row in enumerate(objects(segment_rows, "transcript segments"), 1):
        sid = identifier(row.get("id", f"s{index:04d}"), "segment ID")
        if sid in seen:
            raise ValueError("Duplicate segment ID")
        seen.add(sid)
        interval(scope, row["start"], row["end"])
        segments.append({"id": sid, "start": row["start"], "end": row["end"],
                         "text": text(row["text"], "transcript text"), "claimed_read": False})
    reading, read_frames, read_segments = [], set(), set()
    for index, pack in enumerate(run.get("packs", []), 1):
        frame_ids = list(pack.get("frame_ids", []))
        segment_ids = list(pack.get("segment_ids", []))
        inferred = "segment_ids" not in pack and pack.get("kind") == "overview" and pack.get("text_chars", 0) > 0
        if inferred:
            segment_ids = [s["id"] for s in segments]
        if set(segment_ids) - seen:
            raise ValueError("Reading pack references unavailable segments")
        declared = pack.get("claimed_read") is True
        method = pack.get("reading_method", "native_agent_declaration")
        api_provenance = {}
        if method == "api_material_submission_and_response":
            receipt = next((row for row in usage.get("api_calls", []) if row.get("call_id") == pack.get("api_call_id")), None)
            if (not receipt or receipt.get("reading_method") != method or receipt.get("request_hash") != pack.get("request_hash")
                    or receipt.get("inputs", {}).get("pack_id") != pack.get("id")
                    or receipt.get("inputs", {}).get("frame_ids") != frame_ids
                    or receipt.get("inputs", {}).get("segment_ids") != segment_ids):
                raise ValueError("API reading pack provenance differs from frozen usage receipts")
            api_provenance = {"reading_method": method, "api_call_id": pack["api_call_id"], "request_hash": pack["request_hash"]}
        elif method != "native_agent_declaration":
            raise ValueError("Unknown reading method")
        reading.append({"id": str(pack.get("id", f"p{index:03d}")), "kind": pack.get("kind"),
                        "frame_ids": frame_ids, "segment_ids": segment_ids, "claimed_read": declared,
                        "text_chars": pack.get("text_chars", 0), "images": pack.get("images", 0),
                        "provenance": method if api_provenance else "legacy_overview_transcript" if inferred else "explicit_pack_ids",
                        **api_provenance})
        if declared:
            read_frames.update(frame_ids)
            read_segments.update(segment_ids)
    for row in segments:
        row["claimed_read"] = row["id"] in read_segments
    frames, frame_ids = [], set()
    for row in run.get("frames", []):
        fid = identifier(row["id"], "frame ID")
        if fid in frame_ids:
            raise ValueError("Duplicate frame ID")
        frame_ids.add(fid)
        if fid not in read_frames:
            continue
        second = row.get("timestamp")
        if isinstance(second, bool) or not isinstance(second, (int, float)) or not math.isfinite(second):
            raise ValueError("Frame timestamps must be finite seconds")
        if not scope["processed_range"][0] <= second < scope["processed_range"][1]:
            raise ValueError("Frame evidence is outside the processed interval")
        path = _frame_path(directory, row)
        picture = path.read_bytes()
        if not picture.startswith(b"\xff\xd8\xff"):
            raise ValueError("Expected a JPEG frame")
        sha = hashlib.sha256(picture).hexdigest()
        asset = f"notes/assets/{sha}.jpg"
        target = directory / asset
        if target.parent.is_symlink() or target.is_symlink():
            raise ValueError("Snapshot assets cannot use symlinks")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() != sha:
            raise ValueError("Existing snapshot asset hash differs")
        if not target.exists():
            temp = target.with_suffix(".tmp")
            temp.write_bytes(picture)
            temp.replace(target)
        frames.append({"id": fid, "timestamp": row["timestamp"], "kind": row.get("kind", "overview"),
                       "sha256": sha, "asset": asset, "claimed_read": True})
    if read_frames - frame_ids:
        raise ValueError("Reading pack references unavailable frames")
    return {"run": scope, "source": _source_snapshot(directory, usage), "usage": _usage_snapshot(usage),
            "segments": segments, "frames": frames, "reading": reading}


def _refs(value, snapshot):
    if not isinstance(value, list) or any(not isinstance(ref, str) for ref in value):
        raise ValueError("evidence_refs must be a list of segment:<id> or frame:<id>")
    available = {"segment:" + s["id"] for s in snapshot["segments"] if s.get("claimed_read")}
    available |= {"frame:" + f["id"] for f in snapshot["frames"] if f.get("claimed_read")}
    if set(value) - available:
        raise ValueError("Evidence reference is unavailable or not agent-declared read")
    left, right = snapshot["run"]["processed_range"]
    for frame in snapshot["frames"]:
        if "frame:" + frame["id"] in value and not left <= frame["timestamp"] < right:
            raise ValueError("Frame evidence is outside the processed interval")
    return list(dict.fromkeys(value))


def _block(row, snapshot):
    if not isinstance(row, dict) or row.get("type") not in BLOCK_TYPES:
        raise ValueError("Unknown StudyNote block type")
    if "evidence_refs" not in row:
        raise ValueError("Each block needs explicit evidence_refs; use [] for ungrounded commentary")
    kind = row["type"]
    result = {"type": kind, "evidence_refs": _refs(row.get("evidence_refs", []), snapshot),
              "attribution": row.get("attribution", "speaker")}
    if result["attribution"] not in ("speaker", "agent", "uncertain"):
        raise ValueError("Attribution must be speaker, agent or uncertain")
    if kind in ("paragraph", "code", "formula"):
        result["text"] = text(row.get("text"), "block text", True)
    if kind == "code":
        language = row.get("language", "")
        if not isinstance(language, str) or not re.fullmatch(r"[A-Za-z0-9_+.-]{0,40}", language):
            raise ValueError("Invalid code language")
        result["language"] = language
    if kind == "list":
        if not isinstance(row.get("items"), list) or not row["items"]:
            raise ValueError("A list needs text items")
        result["items"] = [text(item, "list item", True) for item in row["items"]]
        ordered = row.get("ordered", False)
        if not isinstance(ordered, bool):
            raise ValueError("List ordered must be a boolean")
        result["ordered"] = ordered
    if kind == "table":
        headers, rows = row.get("headers"), row.get("rows")
        if not isinstance(headers, list) or not headers or not isinstance(rows, list):
            raise ValueError("Table needs headers and rows")
        result["headers"] = [text(cell, "table header") for cell in headers]
        if any(not isinstance(cells, list) or len(cells) != len(headers) for cells in rows):
            raise ValueError("Table rows must match header width")
        result["rows"] = [[text(cell, "table cell") for cell in cells] for cells in rows]
    if kind == "image":
        fid = identifier(row.get("frame_id"), "frame ID")
        _refs(["frame:" + fid], snapshot)
        if "frame:" + fid not in result["evidence_refs"]:
            raise ValueError("Image blocks must explicitly reference their frame")
        result.update(frame_id=fid, title=text(row.get("title", ""), "image title"),
                      caption=text(row.get("caption", ""), "image caption"))
    return result


def normalize_note(data, snapshot):
    """Validate only structural grounding; semantic support still needs agent review."""
    if not isinstance(data, dict) or data.get("schema_version") != 2:
        raise ValueError("Expected StudyNote schema_version 2")
    result = {"schema_version": 2, "title": text(data.get("title"), "title", True),
              "subtitle": text(data.get("subtitle", ""), "subtitle"),
              "takeaway": text(data.get("takeaway"), "takeaway", True),
              "takeaway_evidence_refs": _refs(data.get("takeaway_evidence_refs", []), snapshot),
              "sections": [], "timeline": [], "figures": [], "caveats": [], "sources": [],
              "usage_note": text(data.get("usage_note", ""), "usage note")}
    section_ids = set()
    for index, row in enumerate(objects(data.get("sections", []), "sections"), 1):
        sid = identifier(row.get("id", f"sec-{index}"), "section ID")
        if sid in section_ids:
            raise ValueError("Duplicate section ID")
        section_ids.add(sid)
        result["sections"].append({"id": sid, "title": text(row.get("title"), "section title", True),
                                   "evidence_refs": _refs(row.get("evidence_refs", []), snapshot),
                                   "blocks": [_block(block, snapshot) for block in objects(row.get("blocks", []), "blocks")]})
    for row in objects(data.get("timeline", []), "timeline"):
        interval(snapshot["run"], row["start"], row["end"])
        item = {"start": row["start"], "end": row["end"], "title": text(row.get("title", ""), "timeline title"),
                "text": text(row.get("text", ""), "timeline text"),
                "evidence_refs": _refs(row.get("evidence_refs", []), snapshot)}
        if row.get("section_id"):
            if row["section_id"] not in section_ids:
                raise ValueError("Timeline references unknown section")
            item["section_id"] = row["section_id"]
        result["timeline"].append(item)
    for row in objects(data.get("figures", []), "figures"):
        block = _block({**row, "type": "image"}, snapshot)
        result["figures"].append({key: block[key] for key in ("frame_id", "title", "caption", "evidence_refs")})
    result["caveats"] = strings(data.get("caveats", []), "caveats")
    result["sources"] = [{"label": text(row.get("label"), "source label", True), "url": safe_url(row.get("url"))}
                         for row in objects(data.get("sources", []), "sources")]
    if data.get("provenance"):
        # Keep import provenance controlled; callers cannot smuggle arbitrary runtime data.
        provenance = data["provenance"]
        if not isinstance(provenance, dict):
            raise ValueError("provenance must be an object")
        result["provenance"] = {"kind": text(provenance.get("kind"), "provenance kind", True),
                                "reference_policy": text(provenance.get("reference_policy", ""), "reference policy")}
    return result


def import_legacy_summary(data, snapshot):
    """Explicitly migrate plain legacy fields; inferred refs are marked, never semantic proof."""
    result = {"schema_version": 2, **{key: copy.deepcopy(data[key]) for key in
              ("title", "subtitle", "takeaway", "caveats", "sources", "usage_note") if key in data},
              "takeaway_evidence_refs": [], "sections": [], "timeline": [], "figures": [],
              "provenance": {"kind": "legacy_summary_import", "reference_policy":
                             "Legacy paragraphs have no exact evidence mapping; timeline refs are inferred by overlap. Imported text needs semantic review."}}
    for index, row in enumerate(objects(data.get("sections", []), "sections"), 1):
        blocks = [{"type": "paragraph", "text": p, "evidence_refs": []} for p in row.get("paragraphs", [])]
        if row.get("bullets"):
            blocks.append({"type": "list", "items": row["bullets"], "evidence_refs": []})
        result["sections"].append({"id": f"sec-{index}", "title": row["title"], "blocks": blocks,
                                   "evidence_refs": []})
    for row in objects(data.get("timeline", []), "timeline"):
        refs = ["segment:" + s["id"] for s in snapshot["segments"]
                if s.get("claimed_read") and s["start"] < row["end"] and s["end"] > row["start"]]
        result["timeline"].append({**row, "evidence_refs": refs})
    for row in data.get("visuals", []):
        result["figures"].append({**row, "evidence_refs": ["frame:" + row["frame_id"]]})
    return normalize_note(result, snapshot)


def validate_note(note, snapshot=None):
    evidence = snapshot or note.get("snapshot")
    if not evidence:
        raise ValueError("StudyNote needs a frozen evidence snapshot")
    canonical = normalize_note(note, evidence)
    if note.get("content_hash") and note["content_hash"] != digest({**canonical, "snapshot": evidence}):
        raise ValueError("StudyNote content or evidence snapshot was modified")
    return canonical


def frame_asset(run_dir, note, frame_id):
    """Resolve only hash-verified assets from this note's immutable snapshot."""
    directory = Path(run_dir).resolve()
    row = next((f for f in note["snapshot"]["frames"] if f["id"] == frame_id and f.get("claimed_read")), None)
    if row is None:
        raise ValueError("Unknown or unread snapshot frame")
    asset = Path(row["asset"])
    allowed = directory / "notes/assets"
    path = (directory / asset).resolve()
    if asset.is_absolute() or ".." in asset.parts or allowed.resolve() != allowed or not path.is_relative_to(allowed):
        raise ValueError("Unsafe snapshot asset path")
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != row["sha256"] or not payload.startswith(b"\xff\xd8\xff"):
        raise ValueError("Snapshot frame hash or JPEG type differs")
    return path


def save_note(run_dir, data, *, legacy=False):
    """Append a frozen revision; repeating identical content and snapshot reuses it."""
    directory = Path(run_dir).resolve()
    folder = directory / "notes"
    if folder.is_symlink():
        raise ValueError("Notes directory cannot be a symlink")
    folder.mkdir(parents=True, exist_ok=True)
    with file_lock(folder / ".save.lock"):
        snapshot = create_snapshot(directory)
        content = import_legacy_summary(data, snapshot) if legacy else normalize_note(data, snapshot)
        content_hash = digest({**content, "snapshot": snapshot})
        versions = sorted(folder.glob("note-v[0-9][0-9][0-9].json"))
        if versions:
            last = load_note(directory, int(versions[-1].stem.split("v")[-1]))
            if last["content_hash"] == content_hash:
                return last
        revision = int(versions[-1].stem.split("v")[-1]) + 1 if versions else 1
        if revision > 999:
            raise ValueError("Revision limit reached")
        note = {**content, "note_id": "note-" + hashlib.sha256(str(snapshot["run"]["run_id"]).encode()).hexdigest()[:16],
                "revision": revision, "content_hash": content_hash, "snapshot": snapshot}
        save(folder / f"note-v{revision:03d}.json", note)
        return note


def load_note(run_dir, revision=None):
    directory = Path(run_dir).resolve()
    if revision is None:
        versions = sorted((directory / "notes").glob("note-v[0-9][0-9][0-9].json"))
        if not versions:
            raise ValueError("No StudyNote exists; save/import one first")
        path = versions[-1]
    else:
        if isinstance(revision, bool) or not isinstance(revision, int) or not 1 <= revision <= 999:
            raise ValueError("Invalid note revision")
        path = directory / "notes" / f"note-v{revision:03d}.json"
    if path.is_symlink() or not path.resolve().is_relative_to(directory / "notes"):
        raise ValueError("Unsafe note path")
    note = load(path)
    file_revision = int(path.stem.split("v")[-1])
    expected_id = "note-" + hashlib.sha256(str(note["snapshot"]["run"]["run_id"]).encode()).hexdigest()[:16]
    if isinstance(note.get("revision"), bool) or note.get("revision") != file_revision or note.get("note_id") != expected_id:
        raise ValueError("Note identity or revision differs from its snapshot/file")
    validate_note(note)
    for row in note["snapshot"]["frames"]:
        frame_asset(directory, note, row["id"])
    return note



def scope_text(note):
    from .run import stamp
    run, usage = note["snapshot"]["run"], note["snapshot"]["usage"]
    left, right = run["processed_range"]
    duration = run.get("video_duration_seconds")
    result = f"本次处理 {stamp(left)}–{stamp(right)}；视频/所选分 P 总时长 {stamp(duration) if duration else '未知'}。"
    status = usage.get("result_status", "partial")
    result += "讲解区间已处理；关键帧为抽样，不代表逐帧或逐页覆盖。" if status == "complete" else f"本次状态：{status}，请按已处理材料理解。"
    if run.get("remaining_range"):
        result += "尚有未处理区间：" + "–".join(stamp(t) for t in run["remaining_range"]) + "。"
    return result


def usage_metrics(note):
    usage = note["snapshot"]["usage"]
    materials, tokens, cost = usage.get("materials", {}), usage.get("tokens", {}), usage.get("cost", {})
    native = (tokens.get("native_actual") or {}).get("total_tokens")
    api_actual = (tokens.get("api_actual") or {}).get("total_tokens")
    api_calls = usage.get("api_calls", [])
    processed = usage.get("video", {}).get("processed_minutes")
    if processed is None:
        left, right = note["snapshot"]["run"]["processed_range"]
        processed = (right - left) / 60
    estimate = tokens.get("material_text_estimate")
    has_api_reading = any(row.get("reading_method") == "api_material_submission_and_response" for row in note["snapshot"].get("reading", []))
    result = [("处理时长", f"{processed:g} 分钟"),
              ("本地候选扫描帧", str(materials.get('candidate_frames_scanned', 0))),
              ("概览 / 细读帧", f"{materials.get('overview_frames_extracted', 0)} / {materials.get('detail_frames_extracted', 0)}"),
              ("API 已提交画面帧" if has_api_reading else "已声明阅读帧", str(len(note["snapshot"]["frames"]))),
              ("原生实际 token" if api_calls else "实际总 token", str(native) if native is not None else "不可得")]
    if api_calls:
        result.append(("API 实际总 token", str(api_actual) if api_actual is not None else "不可得"))
        if tokens.get("api_unknown_token_calls"):
            subtotal = (tokens.get("api_known_subtotal") or {}).get("total_tokens")
            result += [("API 已知 token 小计", str(subtotal) if subtotal is not None else "不可得"),
                       ("API 未知计数调用", str(tokens["api_unknown_token_calls"]))]
    result += [("文字材料粗估", f"{estimate:,} token" if estimate is not None else "不可得"),
               ("独立 API 费用", str(cost["api_usd"]) + " USD" if cost.get("api_usd") is not None else "不可得" if api_calls else "未记录")]
    if cost.get("api_unknown_cost_calls") and cost.get("api_known_subtotal_usd") is not None:
        result.append(("API 已知费用小计", str(cost["api_known_subtotal_usd"]) + " USD（总费用未知）"))
    result.append(("原生美元费用", "不可换算"))
    return result


def usage_explanations(note):
    usage = note["snapshot"]["usage"]
    elapsed = usage.get("elapsed_seconds")
    prefix = f"提取与阅读记录耗时 {elapsed / 60:.1f} 分钟；" if elapsed is not None else ""
    result = [prefix + "文字材料估算不含图片、推理、工具及历史上下文。订阅额度不折算为美元。"]
    api_engine = any(row.get("reading_method") == "api_material_submission_and_response"
                     for row in note["snapshot"].get("reading", []))
    if not usage.get("api_calls"):
        result.append("该 run 未记录独立付费 API 调用。")
    if api_engine:
        result.append("API 概览、细读（如有）与笔记生成调用均计入 finish 前冻结的用量；后续导出不调用模型。未知调用计数或价格使总计保留未知，已知小计不代表完整账单。")
        result.append("API 阅读回执证明指定字幕和 JPEG 已提交并获得响应，不保证模型逐图理解正确或逐页覆盖。费用按实际 counters 与提供的可靠价格计算，不能替代供应商账单。")
    else:
        result.append("此处为 prepare 到 finish 冻结的运行用量；笔记撰写、后续对话与发布不包含在此窗口中。导出脚本无模型调用。")
    if note.get("provenance", {}).get("kind") == "legacy_summary_import":
        result.append("从旧摘要导入：段落缺少精确证据映射；时间轴引用按时间重合推定，不能替代语义核对。")
    if note.get("usage_note"):
        result.append(note["usage_note"])
    return result

def export_note(run_dir, formats=("html",), *, revision=None, out=None, text_only=False):
    directory = Path(run_dir).resolve()
    note = load_note(directory, revision)
    formats = tuple(dict.fromkeys(formats))
    if not formats or set(formats) - {"html", "md"}:
        raise ValueError("Formats must be html and/or md")
    if out and len(formats) != 1 and Path(out).suffix:
        raise ValueError("Multiple formats need an output directory")
    outputs = {}
    for format_name in formats:
        if out:
            base = Path(out).expanduser().resolve()
            path = base if len(formats) == 1 and base.suffix else base / f"note-v{note['revision']:03d}.{format_name}"
        else:
            path = directory / "exports" / f"note-v{note['revision']:03d}.{format_name}"
        path.parent.mkdir(parents=True, exist_ok=True)
        if format_name == "html":
            from .delivery.html import render_note
            path.write_text(render_note(directory, note), encoding="utf-8")
        else:
            from .delivery.markdown import render_note
            path.write_text(render_note(directory, note, path, text_only=text_only), encoding="utf-8")
        outputs[format_name] = str(path)
    receipt = {"note_id": note["note_id"], "revision": note["revision"], "content_hash": note["content_hash"],
               "outputs": outputs, "text_only": bool(text_only), "model_calls": 0}
    save(directory / "exports" / f"receipt-v{note['revision']:03d}.json", receipt)
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("note")
    p.add_argument("--run", required=True); p.add_argument("--input", required=True)
    p.add_argument("--legacy-summary", action="store_true", help="Explicit import of old summary.json")
    p = sub.add_parser("export")
    p.add_argument("--run", required=True); p.add_argument("--format", default="html")
    p.add_argument("--revision", type=int); p.add_argument("--out"); p.add_argument("--text-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "note":
            note = save_note(args.run, load(args.input), legacy=args.legacy_summary)
            result = {key: note[key] for key in ("note_id", "revision", "content_hash")}
        else:
            result = export_note(args.run, args.format.split(","), revision=args.revision,
                                 out=args.out, text_only=args.text_only)
        print(json.dumps(result, ensure_ascii=False)); return 0
    except (ValueError, KeyError, TypeError, OSError) as error:
        print(json.dumps({"ok": False, "message": str(error)}, ensure_ascii=False), file=sys.stderr); return 1


if __name__ == "__main__":
    raise SystemExit(main())
