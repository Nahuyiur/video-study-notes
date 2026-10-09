"""Fixed source adapters and shared source links."""
from pathlib import Path
from contextlib import nullcontext
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..contracts import source_record, safe_url
from ..run import load, selected_duration, segments_from


def normalize_source(raw):
    platform = raw.get("source", {}).get("platform", "bilibili")
    meta, part = raw["metadata"], raw.get("selection", {})
    content = {k: v for k, v in raw.get("content", {}).items()
               if k in ("source_type", "language", "automatic", "subtitle_status", "provider_version", "acquisition", "missing_reason")}
    content["segments"] = segments_from(raw)
    return source_record(platform, meta.get("media_id") or meta.get("bvid") or "provided",
                         part.get("part_id") or part.get("cid") or part.get("page", 1),
                         meta.get("title", "视频"), selected_duration(raw),
                         raw.get("source", {}).get("canonical_url"), part.get("page", 1),
                         content, raw.get("source", {}).get("authenticated", False))


def resolve_source(video, page=None, use_cookie=False, language=None, rednote_skill=None):
    if Path(video).is_file():
        from .local import resolve
        return resolve(video)
    host = (urlsplit(video).hostname or "").lower()
    if host in ("www.xiaohongshu.com", "xiaohongshu.com", "xhslink.cn", "xhslink.com"):
        from .rednote import resolve
        return resolve(video, skill_path=rednote_skill)
    if host in ("youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"):
        from .youtube import resolve
        return resolve(video, language=language)
    if video.startswith("BV") or host in ("bilibili.com", "www.bilibili.com", "b23.tv"):
        from .bilibili import resolve
        return resolve(video, page, use_cookie)
    raise ValueError("Unsupported source; use a Bilibili/YouTube/RedNote URL or local file")


def source_access(video=None, *, rednote_access_url=None, rednote_skill=None):
    """Access links are execution context, never persisted source fields."""
    from .rednote import access, is_rednote
    if rednote_access_url is not None or rednote_skill is not None or (video and is_rednote(video)):
        return access(rednote_access_url or (video if video and is_rednote(video) else None), rednote_skill)
    return nullcontext()


def persistence_video(video):
    from .rednote import persistence_video as sanitized
    return sanitized(video)


def media_input(directory, run, kind, use_cookie=False, override=None, height=720):
    if override or run.get("local_media"):
        path = Path(override or run["local_media"])
        if not path.is_file():
            raise ValueError("Media must be an existing local file")
        return str(path.resolve()), []
    source = load(Path(directory) / "source.json")
    platform = source["source"]["platform"]
    if platform == "bilibili":
        from .bilibili import media
        return media(source, kind, use_cookie, height)
    if platform == "youtube":
        from .youtube import media
        return media(source, kind, height=height)
    if platform == "rednote":
        from .rednote import media
        return media(source, kind, height=height)
    raise ValueError("Local media path unavailable; supply --media")


def timestamp_url(url, second):
    if not url:
        return ""
    parts = urlsplit(safe_url(url))
    host = parts.hostname
    if host not in ("bilibili.com", "www.bilibili.com", "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"):
        return url
    query = [(k, v) for k, v in parse_qsl(parts.query) if k not in ("t", "start")]
    return urlunsplit(parts._replace(query=urlencode(query + [("t", str(max(0, int(second))))])))
