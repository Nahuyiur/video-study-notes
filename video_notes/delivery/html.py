"""One offline HTML renderer consuming validated, frozen StudyNotes."""
from __future__ import annotations

import base64
import html
from pathlib import Path

from ..notes import (digest, frame_asset, safe_url, scope_text, usage_explanations,
                     usage_metrics, validate_note)
from ..run import stamp
from ..sources import timestamp_url


def esc(value):
    return html.escape(str(value), quote=True)


def jump(url, second):
    return timestamp_url(url, second)


def link(label, url):
    return f'<a href="{esc(safe_url(url))}" target="_blank" rel="noopener noreferrer">{esc(label)} ↗</a>'


def items(rows, ordered=False):
    tag = "ol" if ordered else "ul"
    return f"<{tag}>" + "".join(f"<li>{esc(row)}</li>" for row in rows) + f"</{tag}>" if rows else ""


def _evidence(note, refs):
    url = note["snapshot"]["source"].get("canonical_url")
    segments = {"segment:" + row["id"]: row["start"] for row in note["snapshot"]["segments"]}
    frames = {"frame:" + row["id"]: row["timestamp"] for row in note["snapshot"]["frames"]}
    labels = []
    for ref in refs:
        second = {**segments, **frames}[ref]
        label = f"{ref} · {stamp(second)}"
        labels.append(link(label, jump(url, second)) if url else esc(label))
    return '<p class="muted evidence">证据：' + " · ".join(labels) + "</p>" if labels else ""


def _figure(directory, note, row):
    frame = next(f for f in note["snapshot"]["frames"] if f["id"] == row["frame_id"])
    encoded = base64.b64encode(frame_asset(directory, note, frame["id"]).read_bytes()).decode("ascii")
    url = note["snapshot"]["source"].get("canonical_url")
    time = stamp(frame["timestamp"])
    time_link = link(time + " · 返回视频", jump(url, frame["timestamp"])) if url else esc(time)
    return (f'<figure><div class="figure-top"><h3>{esc(row.get("title", ""))}</h3><span>{time_link}</span></div>'
            f'<img src="data:image/jpeg;base64,{encoded}" alt="{esc(row.get("title", ""))}" loading="lazy">'
            f'<figcaption>{esc(row.get("caption", ""))}</figcaption>{_evidence(note, row.get("evidence_refs", []))}</figure>')


def _block(directory, note, row):
    kind = row["type"]
    if kind == "paragraph":
        body = f'<p>{esc(row["text"])}</p>'
    elif kind == "list":
        body = items(row["items"], row["ordered"])
    elif kind == "code":
        body = f'<pre><code class="language-{esc(row["language"])}">{esc(row["text"])}</code></pre>'
    elif kind == "formula":
        body = f'<pre class="formula" aria-label="公式源码">{esc(row["text"])}</pre>'
    elif kind == "table":
        body = '<div class="table-scroll"><table><thead><tr>' + "".join(f'<th>{esc(c)}</th>' for c in row["headers"]) + '</tr></thead><tbody>'
        body += "".join('<tr>' + "".join(f'<td>{esc(c)}</td>' for c in cells) + '</tr>' for cells in row["rows"])
        body += '</tbody></table></div>'
    else:
        return _figure(directory, note, row)
    if row["attribution"] != "speaker":
        body += '<p class="muted">' + ("Agent 解读" if row["attribution"] == "agent" else "尚不确定") + '</p>'
    return body + _evidence(note, row["evidence_refs"])


def render_note(directory, note):
    """Render only a frozen note; never reopen acquisition or accounting artifacts."""
    validate_note(note)
    directory = Path(directory).resolve()
    snapshot = note["snapshot"]
    run, usage = snapshot["run"], snapshot["usage"]
    url = snapshot["source"].get("canonical_url")
    if url:
        safe_url(url)
    chunks, nav = [], []

    def section(identifier, title, body):
        if body:
            nav.append(f'<a href="#{identifier}">{esc(title)}</a>')
            chunks.append(f'<section id="{identifier}"><h2>{esc(title)}</h2>{body}</section>')

    section("overview", "核心总结", f'<div class="takeaway">{esc(note["takeaway"])}</div>' + _evidence(note, note["takeaway_evidence_refs"]))
    body = ""
    for row in note["sections"]:
        body += f'<article id="{esc(row["id"])}"><h3>{esc(row["title"])}</h3>'
        body += "".join(_block(directory, note, block) for block in row["blocks"])
        body += _evidence(note, row["evidence_refs"]) + '</article>'
    section("explanation", "内容解释", body)
    rows = []
    for row in note["timeline"]:
        time = f'{stamp(row["start"])}–{stamp(row["end"])}'
        time = link(time, jump(url, row["start"])) if url else esc(time)
        rows.append(f'<div class="moment"><div class="time">{time}</div><div><h3>{esc(row["title"])}</h3><p>{esc(row["text"])}</p>{_evidence(note, row["evidence_refs"])}</div></div>')
    section("timeline", "视频时间轴", "".join(rows))
    section("visuals", "关键画面与解释", "".join(_figure(directory, note, row) for row in note["figures"]))
    section("limits", "范围与边界", f'<p>{esc(scope_text(note))}</p>' + items(note["caveats"]))
    body = '<div class="metrics">' + "".join(f'<div><span>{esc(k)}</span><strong>{esc(v)}</strong></div>' for k, v in usage_metrics(note)) + '</div>'
    body += "".join(f'<p class="muted">{esc(line)}</p>' for line in usage_explanations(note))
    section("usage", "本次用量", body)
    sources = ([{"label": "原视频", "url": url}] if url else []) + note["sources"]
    section("sources", "来源", '<div class="sources">' + " · ".join(link(s["label"], s["url"]) for s in sources) + '</div>' if sources else "")
    style = (Path(__file__).resolve().parents[2] / "assets/summary.css").read_text()
    style += '\npre{white-space:pre-wrap;overflow-wrap:anywhere;padding:1rem;background:#f5f5f2;border-radius:8px}code{font-family:monospace}.table-scroll{overflow:auto}table{border-collapse:collapse;width:100%}th,td{padding:.7rem;border:1px solid #ddd;text-align:left}.evidence{font-size:.78rem}.evidence a{overflow-wrap:anywhere}'
    summary_hash = note.get("content_hash") or digest({key: value for key, value in note.items() if key != "snapshot"})
    status = usage.get("result_status", "partial")
    coverage = "讲解全段 · 画面抽样" if status == "complete" else "材料覆盖有限 · " + status
    subtitle = f'<p class="subtitle">{esc(note["subtitle"])}</p>' if note.get("subtitle") else ""
    left, right = run["processed_range"]
    materials = usage.get("materials", {})
    count = materials.get("overview_frames_extracted", 0) + materials.get("detail_frames_extracted", 0)
    return f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="summary-sha256" content="{summary_hash}"><title>{esc(note["title"])} · 视频总结</title><style>{style}</style></head>
<body><aside><div class="brand">VIDEO NOTES<span>视频阅读笔记</span></div><nav aria-label="目录">{"".join(nav)}</nav><p class="aside-note">讲解与画面一起读<br>原视频时间戳可点击</p></aside><main><header><div class="eyebrow">视频总结 <span>{esc(coverage)}</span></div><h1>{esc(note["title"])}</h1>{subtitle}<div class="header-meta">{esc(stamp(left))}–{esc(stamp(right))} · {esc(usage.get("content_source", "unknown"))} · {count} 个采样画面</div></header>{"".join(chunks)}<footer>图片已嵌入 · 支持离线阅读与浏览器打印 · 摘要版本 {summary_hash[:12]}</footer></main></body></html>'''
