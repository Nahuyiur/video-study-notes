"""BiliLens metadata/subtitles and Bilibili DASH access."""
import argparse
import asyncio
import importlib.util
from pathlib import Path
from urllib.parse import urlencode

from . import normalize_source


def extractor():
    path = Path(__file__).resolve().parents[2] / "scripts/upstream/bilibili_extract.py"
    spec = importlib.util.spec_from_file_location("bililens_extractor", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def native_extract(video, page, use_cookie):
    bili = extractor()
    args = argparse.Namespace(video=video, page=page, subtitle_language=None,
                              include_danmaku=False, danmaku_limit=0,
                              use_env_cookie=use_cookie, retries=2)
    try:
        return asyncio.run(bili.extract(args))
    except bili.ParserError as exc:
        raise RuntimeError(f"Bilibili {exc.stage} retrieval failed; use local materials or retry once") from None


def resolve(video, page=None, use_cookie=False):
    return normalize_source(native_extract(video, page, use_cookie))


def media(source, kind, use_cookie=False, height=720):
    bili = extractor()
    query = urlencode({"bvid": source["metadata"]["media_id"], "cid": source["selection"]["part_id"],
                       "fnval": 16, "qn": 80 if height > 720 else 64})
    try:
        data = bili.BilibiliClient(use_env_cookie=use_cookie, retries=2).get_json(
            f"https://api.bilibili.com/x/player/playurl?{query}", "media", source["source"]["canonical_url"])["data"]
    except bili.ParserError as exc:
        raise RuntimeError(f"Bilibili {exc.stage} media retrieval failed") from None
    streams = data.get("dash", {}).get(kind, [])
    if not streams:
        raise RuntimeError("No accessible DASH stream; supply local media")
    if kind == "video":
        suitable = [s for s in streams if s.get("height", 9999) <= height]
        stream = max(suitable, key=lambda s: (s.get("height", 0), str(s.get("codecs", "")).startswith("avc"))) if suitable else min(streams, key=lambda s: s.get("height", 9999))
    else:
        stream = min(streams, key=lambda s: s.get("bandwidth", 0))
    url = stream.get("baseUrl") or stream.get("base_url")
    if not url:
        raise RuntimeError("Media stream contains no URL")
    headers = f"Referer: {source['source']['canonical_url']}\r\nUser-Agent: {bili.USER_AGENT}\r\n"
    return url, ["-headers", headers, "-rw_timeout", "15000000"]
