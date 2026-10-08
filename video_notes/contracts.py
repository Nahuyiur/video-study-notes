"""Stable source boundary; provider responses never escape an adapter."""
from __future__ import annotations

import math
from typing import TypedDict
from urllib.parse import urlsplit


class Segment(TypedDict):
    id: str
    start: float
    end: float
    text: str


class SourceRecord(TypedDict):
    schema_version: int
    source: dict
    metadata: dict
    selection: dict
    content: dict


def safe_url(value):
    parts = urlsplit(str(value))
    if parts.scheme not in ("http", "https") or not parts.netloc or parts.username or parts.password:
        raise ValueError("Expected an HTTP(S) URL without credentials")
    return str(value)


def source_record(platform, media_id, part_id, title, duration, url=None,
                  page=1, content=None, authenticated=False):
    duration = float(duration)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Source duration must be positive finite seconds")
    if platform not in ("bilibili", "youtube", "local") or not media_id or not part_id:
        raise ValueError("Invalid source identity")
    return {"schema_version": 2,
            "source": {"platform": platform, "canonical_url": safe_url(url) if url else None,
                       "authenticated": bool(authenticated)},
            "metadata": {"media_id": str(media_id), "title": str(title), "duration_seconds": duration},
            "selection": {"part_id": str(part_id), "page": int(page or 1)},
            "content": content or {"source_type": "none", "segments": []}}
