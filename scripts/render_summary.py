"""Render an already-read video summary as one offline HTML file; no AI calls."""
from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import math
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from core import load, stamp


def esc(value):
    return html.escape(str(value), quote=True)


def safe_url(value):
    value = str(value)
    parts = urlsplit(value)
    if parts.scheme not in ("https", "http") or not parts.netloc or parts.username or parts.password:
        raise ValueError("Source links must be ordinary HTTP(S) URLs without credentials")
    return value


def jump(url, second):
    if not url:
        return ""
    parts = urlsplit(safe_url(url))
    if parts.hostname not in ("www.bilibili.com", "bilibili.com"):
        return url
    query = [(k, v) for k, v in parse_qsl(parts.query) if k != "t"]
    return urlunsplit(parts._replace(query=urlencode(query + [("t", str(int(second)))])))


def link(label, url):
    return f'<a href="{esc(safe_url(url))}" target="_blank" rel="noopener noreferrer">{esc(label)} ↗</a>'


def items(rows):
    return "<ul>" + "".join(f"<li>{esc(row)}</li>" for row in rows) + "</ul>" if rows else ""


def interval(run, start, end):
    a, b = run["processed_range"]
    if not all(isinstance(x, (int, float)) and math.isfinite(x) for x in (start, end)):
        raise ValueError("Timeline bounds must be finite seconds")
    if not a <= start < end <= b:
        raise ValueError("Timeline entry is outside the processed interval")


def render(directory, data):
    directory = Path(directory).resolve()
    run, usage = load(directory / "run.json"), load(directory / "usage.json")
    if run["status"] != "finished" or usage["run_id"] != run["run_id"]:
        raise ValueError("Finalize this run's usage before rendering")
    if not str(data.get("title", "")).strip() or not str(data.get("takeaway", "")).strip():
        raise ValueError("A summary needs a title and a substantive takeaway")
    url = usage["video"].get("url")
    if url:
        safe_url(url)
    chunks, nav = [], []

    def section(identifier, title, body):
        if body:
            nav.append(f'<a href="#{identifier}">{esc(title)}</a>')
            chunks.append(f'<section id="{identifier}"><h2>{esc(title)}</h2>{body}</section>')

    section("overview", "核心总结", f'<div class="takeaway">{esc(data["takeaway"])}</div>')
    body = ""
    for row in data.get("sections", []):
        body += f'<article><h3>{esc(row["title"])}</h3>'
        body += "".join(f"<p>{esc(p)}</p>" for p in row.get("paragraphs", []))
        body += items(row.get("bullets", [])) + "</article>"
    section("explanation", "内容解释", body)

    rows = []
    for row in data.get("timeline", []):
        interval(run, row["start"], row["end"])
        time = f'{stamp(row["start"])}–{stamp(row["end"])}'
        time = link(time, jump(url, row["start"])) if url else esc(time)
        rows.append(f'<div class="moment"><div class="time">{time}</div><div><h3>{esc(row["title"])}</h3><p>{esc(row["text"])}</p></div></div>')
    section("timeline", "视频时间轴", "".join(rows))

    viewed = {fid for p in run["packs"] if p.get("claimed_read") for fid in p["frame_ids"]}
    frames = {f["id"]: f for f in run["frames"]}
    cards = []
    for row in data.get("visuals", []):
        fid = row["frame_id"]
        if fid not in viewed:
            raise ValueError("Only actually read frames may appear in the summary")
        frame = frames[fid]
        path = Path(frame["path"]).resolve()
        if not path.is_relative_to(directory / "frames"):
            raise ValueError("Frame path must stay inside this run's frames folder")
        picture = path.read_bytes()
        if not picture.startswith(b"\xff\xd8\xff"):
            raise ValueError("Expected a JPEG frame")
        encoded = base64.b64encode(picture).decode("ascii")
        time = stamp(frame["timestamp"])
        time_link = link(time + " · 返回视频", jump(url, frame["timestamp"])) if url else esc(time)
        cards.append(f'<figure><div class="figure-top"><h3>{esc(row["title"])}</h3><span>{time_link}</span></div><img src="data:image/jpeg;base64,{encoded}" alt="{esc(row["title"])}" loading="lazy"><figcaption>{esc(row["caption"])}</figcaption></figure>')
    section("visuals", "关键画面与解释", "".join(cards))

    status = usage["result_status"]
    a, b = run["processed_range"]
    scope = f"本次处理 {stamp(a)}–{stamp(b)}；视频/所选分 P 总时长 {stamp(run['video_duration_seconds'] or 0)}。"
    scope += "讲解区间已处理；关键帧为抽样，不代表逐帧或逐页覆盖。" if status == "complete" else f"本次状态：{status}，请按已处理材料理解。"
    if run.get("remaining_range"):
        scope += "尚有未处理区间：" + "–".join(stamp(t) for t in run["remaining_range"]) + "。"
    section("limits", "范围与边界", f'<p>{esc(scope)}</p>' + items(data.get("caveats", [])))

    tokens, cost, materials = usage["tokens"], usage["cost"], usage["materials"]
    native = tokens.get("native_actual")
    actual = native.get("total_tokens") if native else None
    metrics = [
        ("处理时长", f'{usage["video"]["processed_minutes"]:g} 分钟'),
        ("概览 / 细读帧", f'{materials["overview_frames_extracted"]} / {materials["detail_frames_extracted"]}'),
        ("实际总 token", actual if actual is not None else "不可得"),
        ("文字材料粗估", f'{tokens["material_text_estimate"]:,} token'),
        ("独立 API 费用", str(cost["api_usd"]) + " USD" if cost["api_usd"] is not None else "未记录"),
        ("原生美元费用", "不可换算"),
    ]
    body = '<div class="metrics">' + "".join(f'<div><span>{esc(k)}</span><strong>{esc(v)}</strong></div>' for k, v in metrics) + "</div>"
    body += f'<p class="muted">提取与阅读记录耗时 {usage["elapsed_seconds"] / 60:.1f} 分钟；文字材料估算不含图片、推理、工具及历史上下文。订阅额度不折算为美元。</p>'
    if not usage.get("api_calls"):
        body += '<p class="muted">该 run 未调用独立付费 API。</p>'
    if data.get("usage_note"):
        body += f'<p class="muted">{esc(data["usage_note"])}</p>'
    section("usage", "本次用量", body)
    sources = ([{"label": "原视频", "url": url}] if url else []) + data.get("sources", [])
    section("sources", "来源", '<div class="sources">' + " · ".join(link(s["label"], s["url"]) for s in sources) + "</div>" if sources else "")

    style = (Path(__file__).resolve().parents[1] / "assets/summary.css").read_text()
    digest = hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    coverage = "讲解全段 · 画面抽样" if status == "complete" else "材料覆盖有限 · " + status
    subtitle = f'<p class="subtitle">{esc(data["subtitle"])}</p>' if data.get("subtitle") else ""
    return f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="summary-sha256" content="{digest}"><title>{esc(data["title"])} · 视频总结</title><style>{style}</style></head>
<body><aside><div class="brand">VIDEO NOTES<span>视频阅读笔记</span></div><nav aria-label="目录">{"".join(nav)}</nav><p class="aside-note">讲解与画面一起读<br>原视频时间戳可点击</p></aside><main><header><div class="eyebrow">视频总结 <span>{esc(coverage)}</span></div><h1>{esc(data["title"])}</h1>{subtitle}<div class="header-meta">{esc(stamp(a))}–{esc(stamp(b))} · {esc(usage["content_source"])} · {materials["overview_frames_extracted"] + materials["detail_frames_extracted"]} 个采样画面</div></header>{"".join(chunks)}<footer>图片已嵌入 · 支持离线阅读与浏览器打印 · 摘要版本 {digest[:12]}</footer></main></body></html>'''


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run", required=True)
    p.add_argument("--summary")
    p.add_argument("--out")
    args = p.parse_args()
    directory = Path(args.run).resolve()
    summary = Path(args.summary) if args.summary else directory / "summary.json"
    out = Path(args.out) if args.out else directory / "summary.html"
    try:
        result = render(directory, load(summary))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(result, encoding="utf-8")
    except (ValueError, KeyError, OSError, TypeError) as error:
        p.exit(1, f"HTML summary failed: {error}\n")
    print(json.dumps({"html": str(out.resolve()), "bytes": out.stat().st_size, "offline": True}, ensure_ascii=False))


if __name__ == "__main__":
    main()
