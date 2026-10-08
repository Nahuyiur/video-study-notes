"""Publish frozen notes through the installed lark-cli, with durable receipts.

This module creates only new, skill-owned documents. It never overwrites a
user document. A write whose result is ambiguous is reconciled or stopped.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
import fcntl
import hashlib
import html
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Protocol

from ..sources import timestamp_url


class DeliveryError(RuntimeError):
    """A safe, actionable publication failure; no credentials or raw output."""


class AmbiguousDelivery(DeliveryError):
    """A remote mutation may have succeeded; never blindly repeat it."""


class Transport(Protocol):
    def create(self, content: str, parent_token: str | None) -> dict: ...
    def fetch(self, document_id: str) -> dict: ...
    def insert_image(self, document_id: str, path: Path, caption: str, selection: str) -> dict: ...
    def download_image(self, token: str) -> bytes: ...


def _digest(value):
    if isinstance(value, bytes):
        return hashlib.sha256(value).hexdigest()
    return _digest(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())


def _save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".receipt-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        parent = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def _lock(directory):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".feishu.lock").open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def _token(value):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", str(value)):
        raise ValueError("Expected a Feishu token, not a URL or shell expression")
    return str(value)


def _text(value):
    return html.escape(str(value), quote=True)


def _inline(value):
    return _text(value).replace("\n", "<br/>")


def _stamp(seconds):
    seconds = int(seconds)
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def _link(label, url):
    from ..contracts import safe_url
    return f'<a href="{_text(safe_url(url))}">{_inline(label)}</a>' if url else _inline(label)


def _time(source, seconds):
    url = source.get("canonical_url")
    return _link(_stamp(seconds), timestamp_url(url, seconds)) if url else _stamp(seconds)


def _evidence(snapshot, refs):
    """Expose original-time evidence without platform or renderer internals."""
    segments = {"segment:" + str(row["id"]): row for row in snapshot.get("segments", [])}
    frames = {"frame:" + str(row["id"]): row for row in snapshot.get("frames", [])}
    links = []
    for ref in dict.fromkeys(refs):
        row = segments.get(ref) or frames.get(ref)
        if not row or not row.get("claimed_read"):
            raise ValueError("Cannot publish unavailable or unread evidence")
        second = row["start"] if ref.startswith("segment:") else row["timestamp"]
        label = ("讲解 " + _stamp(second) + "–" + _stamp(row["end"])) if ref.startswith("segment:") else ("画面 " + _stamp(second))
        url = snapshot["source"].get("canonical_url")
        links.append(_link(label, timestamp_url(url, second)) if url else _inline(label))
    return "<p>证据：" + " · ".join(links) + "</p>" if links else ""


def _attribution(block):
    attribution = block.get("attribution", "speaker")
    if attribution not in ("speaker", "agent", "uncertain"):
        raise ValueError("Unknown note attribution")
    return {"speaker": "", "agent": "Agent 解读：", "uncertain": "尚不确定："}[attribution]


def _block(block):
    kind = block["type"]
    if kind == "paragraph":
        return f'<p>{_inline(block["text"])}</p>'
    if kind == "list":
        tag = "ol" if block.get("ordered") else "ul"
        return f'<{tag}>' + "".join(f'<li>{_inline(item)}</li>' for item in block["items"]) + f'</{tag}>'
    if kind == "code":
        language = block.get("language") or "plain text"
        return f'<pre lang="{_text(language)}"><code>{_text(block["text"])}</code></pre>'
    if kind == "formula":
        # Keep readable TeX as well: unsupported formula rendering cannot lose it.
        return f'<p><latex>{_text(block["text"])}</latex></p>'
    if kind == "table":
        headers = block.get("headers", [])
        head = "<thead><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in headers) + "</tr></thead>" if headers else ""
        rows = "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in row) + "</tr>" for row in block["rows"])
        return "<table>" + head + "<tbody>" + rows + "</tbody></table>"
    if kind == "image":
        return ""  # figure plan owns image content and evidence checks
    raise ValueError(f"Unsupported note block: {kind}")


def build_plan(note, snapshot):
    """Map validated semantic content to DocxXML and image insertion anchors."""
    source, run, usage = snapshot["source"], snapshot["run"], snapshot["usage"]
    rows = [f'<title>{_inline(note["title"])}</title>']
    if note.get("subtitle"):
        rows.append(f'<p>{_inline(note["subtitle"])}</p>')
    rows.append(f'<p>{_inline(note["takeaway"])}</p>')
    rows.append(_evidence(snapshot, note.get("takeaway_evidence_refs", [])))
    for section in note.get("sections", []):
        rows.append(f'<h1>{_inline(section["title"])}</h1>')
        rows.append(_evidence(snapshot, section.get("evidence_refs", [])))
        for block in section.get("blocks", []):
            label = _attribution(block)
            if block["type"] == "paragraph":
                rows.append(f'<p>{_inline(label + block["text"])}</p>')
            else:
                if label:
                    rows.append(f'<p>{_inline(label)}</p>')
                if block["type"] == "image":
                    rows.append(f'<p>图像：{_inline(block.get("title", "关键画面"))}。{_inline(block.get("caption", ""))}</p>')
                else:
                    rows.append(_block(block))
            rows.append(_evidence(snapshot, block.get("evidence_refs", [])))
    if note.get("timeline"):
        rows.append("<h1>视频时间轴</h1>")
        for moment in note["timeline"]:
            label = _time(source, moment["start"]) + "–" + _stamp(moment["end"])
            rows.append(f'<p>{label} · {_inline(moment["title"])}：{_inline(moment.get("text", ""))}</p>')
            rows.append(_evidence(snapshot, moment.get("evidence_refs", [])))
    frames = {str(f["id"]): f for f in snapshot.get("frames", [])}
    figures = list(note.get("figures", []))
    seen = {str(row["frame_id"]) for row in figures}
    for section in note.get("sections", []):
        for block in section.get("blocks", []):
            if block["type"] == "image" and str(block["frame_id"]) not in seen:
                figures.append({"frame_id": block["frame_id"], "title": block.get("title", "关键画面"), "caption": block.get("caption", ""), "evidence_refs": block.get("evidence_refs", [])})
                seen.add(str(block["frame_id"]))
    images = []
    if figures:
        rows.append("<h1>关键画面与解释</h1>")
    for i, figure in enumerate(figures, 1):
        fid = str(figure["frame_id"])
        frame = frames[fid]
        if not frame.get("claimed_read"):
            raise ValueError("Only actually read frames can be published")
        caption = f'图 {i}：{figure.get("title", "关键画面")}。{figure.get("caption", "")}'
        anchor = caption + "（" + _stamp(frame["timestamp"]) + "）"
        rows.append(f'<p>{_inline(anchor)} · {_time(source, frame["timestamp"])}</p>')
        rows.append(_evidence(snapshot, figure.get("evidence_refs", [])))
        images.append({"frame_id": fid, "caption": caption, "selection": anchor, "sha256": frame["sha256"]})
    rows.append("<h1>范围与用量</h1>")
    from ..notes import scope_text, usage_metrics, usage_explanations
    rows.append(f'<p>{_inline(scope_text(note))}</p>')
    rows.extend(f'<p>{_inline(c)}</p>' for c in note.get("caveats", []))
    rows.append(_block({"type": "table", "headers": ["项目", "本次记录"],
                        "rows": [list(row) for row in usage_metrics(note)]}))
    rows.extend(f'<p>{_inline(explanation)}</p>' for explanation in usage_explanations(note))
    sources = ([{"label": "原视频", "url": source["canonical_url"]}] if source.get("canonical_url") else []) + note.get("sources", [])
    if sources:
        rows.append("<h1>来源</h1>")
        rows.extend(f'<p>{_link(s["label"], s["url"])}</p>' for s in sources)
    # A managed footer makes explicit reconciliation reject unrelated documents.
    marker = f'video-study-notes · {note["note_id"]} · 第 {note["revision"]} 版 · {note["content_hash"][:16]}'
    rows.append(f'<p>{_inline(marker)}</p>')
    content = "".join(rows)
    if len(content.encode("utf-8")) > 160_000:
        raise ValueError("This note is too long for one safe delivery; split it into chapter notes")
    _xml(content)  # Parser/runtime failures must happen before any remote write.
    return {"content": content, "images": images, "marker": marker,
            "payload_hash": _digest({"content": content, "images": images})}


def _xml(content):
    try:
        import xml.etree.ElementTree as ET
    except ImportError as error:
        raise DeliveryError("This Python runtime cannot load its XML parser; use a working Python 3.11+ runtime before publication") from error
    try:
        return ET.fromstring("<document>" + content + "</document>")
    except ET.ParseError as error:
        raise DeliveryError("Publication/readback was not valid DocxXML") from error
    except ImportError as error:
        raise DeliveryError("This Python runtime cannot load its XML parser; use a working Python 3.11+ runtime before publication") from error


def _squash(text):
    return re.sub(r"\s+", "", text)


def _verify_text(plan, document):
    expected, actual = _xml(plan["content"]), _xml(document["content"])
    text = _squash("".join(actual.itertext()))
    if _squash(plan["marker"]) not in text:
        raise DeliveryError("Remote document is not this skill-owned note revision")
    # Verify each semantic atom, so reordered rich wrappers cannot hide loss.
    atoms = ("title", "h1", "p", "li", "code", "latex", "th", "td")
    for element in expected.iter():
        if element.tag in atoms:
            fragment = _squash("".join(element.itertext()))
            if fragment and fragment not in text:
                raise DeliveryError("Remote document is missing expected content")
    counts = Counter(e.tag for e in actual.iter())
    planned = Counter(e.tag for e in expected.iter())
    for kind in ("title", "h1", "li", "pre", "latex", "table", "tr"):
        if counts[kind] != planned[kind]:
            raise DeliveryError(f"Remote rich-block count mismatch: {kind}; no completion claim")
    expected_links = Counter(e.get("href") for e in expected.iter("a"))
    actual_links = Counter(e.get("href") for e in actual.iter("a"))
    if not (expected_links <= actual_links):
        raise DeliveryError("Remote document lost a source or timestamp link")
    return actual


def _remote_image(tree, image, transport):
    matches = [e for e in tree.iter("img") if (e.get("caption") or "").strip() == image["caption"].strip()]
    if len(matches) != 1:
        raise AmbiguousDelivery("Could not uniquely reconcile the image; inspect remote document before retrying")
    element = matches[0]
    token = element.get("token") or element.get("src")
    if not token:
        raise AmbiguousDelivery("Remote image block has no media token; publication remains incomplete")
    if _digest(transport.download_image(token)) != image["sha256"]:
        raise DeliveryError("Remote image bytes do not match the frozen note asset")
    return {"block_id": element.get("id"), "file_token": token, "sha256": image["sha256"]}


class LarkCLI:
    """Thin argv-only transport; installed CLI owns authentication."""
    def __init__(self, timeout=60):
        self.executable = shutil.which("lark-cli")
        if not self.executable:
            raise DeliveryError("lark-cli is unavailable; local HTML/Markdown remain usable")
        self.timeout = timeout

    def _call(self, args, content=None, cwd=None):
        env = dict(os.environ, LARKSUITE_CLI_NO_UPDATE_NOTIFIER="1", LARKSUITE_CLI_NO_SKILLS_NOTIFIER="1")
        try:
            result = subprocess.run([self.executable, "docs", *args, "--as", "user", "--format", "json"],
                                    input=content, text=True, capture_output=True, cwd=cwd,
                                    timeout=self.timeout, env=env, check=False)
        except subprocess.TimeoutExpired as error:
            raise DeliveryError("lark-cli timed out; remote result must be reconciled") from error
        try:
            data = json.loads(result.stdout if result.returncode == 0 else result.stderr)
        except (ValueError, TypeError) as error:
            raise DeliveryError("lark-cli returned an unreadable result; remote result must be reconciled") from error
        if result.returncode or data.get("ok") is not True:
            reason = data.get("error", {})
            # Never echo tokens, auth links, or raw remote error messages.
            raise DeliveryError(f'lark-cli failed ({reason.get("type", "error")}/{reason.get("subtype", "unknown")}); inspect CLI authorization/status')
        return data["data"]

    def create(self, content, parent_token=None):
        args = ["+create", "--doc-format", "xml", "--content", "-"]
        if parent_token:
            args += ["--parent-token", _token(parent_token)]
        return self._call(args, content)["document"]

    def fetch(self, document_id):
        return self._call(["+fetch", "--doc", _token(document_id), "--detail", "full", "--doc-format", "xml"])["document"]

    def insert_image(self, document_id, path, caption, selection):
        path = Path(path).resolve()
        return self._call(["+media-insert", "--doc", _token(document_id), "--file", path.name,
                           "--caption", caption, "--selection-with-ellipsis", selection, "--align", "center"], cwd=path.parent)

    def document_url(self, document_id, title):
        # +fetch omits URLs on some CLI versions. Search is read-only and bounded.
        page = None
        for _ in range(4):
            args = ["+search", "--query", title, "--page-size", "20"]
            if page:
                args += ["--page-token", page]
            result = self._call(args)
            for row in result.get("results", []):
                meta = row.get("result_meta", {})
                if meta.get("token") == document_id and meta.get("url"):
                    return meta["url"]
            if not result.get("has_more") or not result.get("page_token"):
                break
            page = result["page_token"]
        raise DeliveryError("Document URL was not found in bounded read-only search; retain receipt and retry readback later")

    def download_image(self, token):
        with tempfile.TemporaryDirectory(prefix="video-notes-media-") as temp:
            path = Path(temp) / "media.bin"
            self._call(["+media-download", "--token", _token(token), "--output", path.name], cwd=temp)
            if not path.is_file():
                raise DeliveryError("Media readback did not return the expected local file")
            return path.read_bytes()


def publish_note(note, run_dir, *, target="new", transport=None, reconcile_document=None, receipt_dir=None, asset_resolver=None):
    """Publish a validated, frozen note. Every cached success is read back again.

    target is 'new' or 'folder:TOKEN'. reconcile_document is only accepted for
    a previously inflight/unknown create, and never authorizes overwriting.
    """
    run_dir = Path(run_dir).resolve()
    snapshot = note["snapshot"]
    plan = build_plan(note, snapshot)
    if target == "new":
        parent_token = None
    elif str(target).startswith("folder:"):
        parent_token = _token(target.removeprefix("folder:"))
    else:
        raise ValueError("Feishu delivery only creates a new skill-owned document; use new or folder:TOKEN")
    identity = {"note_id": note["note_id"], "revision": note["revision"], "content_hash": note["content_hash"], "target": target}
    key = _digest(identity)
    directory = Path(receipt_dir).resolve() if receipt_dir else run_dir / "deliveries"
    path = directory / f"feishu-{key}.json"
    if asset_resolver is None:
        from ..notes import frame_asset
        asset_resolver = frame_asset
    # Check all immutable assets before the first external write.
    assets = {i["frame_id"]: Path(asset_resolver(run_dir, note, i["frame_id"])) for i in plan["images"]}
    for image in plan["images"]:
        if _digest(assets[image["frame_id"]].read_bytes()) != image["sha256"]:
            raise ValueError("Frozen image hash mismatch")
    transport = transport or LarkCLI()
    with _lock(directory):
        receipt = json.loads(path.read_text()) if path.exists() else {
            "schema_version": 1, "delivery_id": key, **identity, "payload_hash": plan["payload_hash"],
            "note_fingerprint": _digest(note), "plan": plan,
            "status": "planned", "create": {"status": "planned"}, "images": {}}
        if any(receipt.get(k) != v for k, v in identity.items()) or receipt.get("note_fingerprint") != _digest(note):
            raise ValueError("Delivery receipt identity/content mismatch; do not reuse it")
        plan = receipt["plan"]  # Frozen layout permits safe reuse after renderer upgrades.
        if receipt["payload_hash"] != _digest({"content": plan["content"], "images": plan["images"]}):
            raise ValueError("Persisted delivery plan hash mismatch")
        _xml(plan["content"])
        def save():
            _save(path, receipt)
        save()
        create = receipt["create"]
        if create["status"] in ("inflight", "unknown"):
            if not reconcile_document:
                raise AmbiguousDelivery("Document creation result is unknown. Supply --reconcile-document after locating this managed note; no duplicate was created")
            remote = transport.fetch(_token(reconcile_document))
            _verify_text(plan, remote)
            create.update(status="created", document_id=_token(reconcile_document), url=remote.get("url"), reconciled=True)
            save()
        elif reconcile_document:
            raise ValueError("Reconciliation is only allowed for an ambiguous create receipt")
        if create["status"] == "planned":
            create["status"] = "inflight"
            receipt["status"] = "publishing"
            save()
            try:
                remote = transport.create(plan["content"], parent_token)
                document_id = _token(remote["document_id"])
            except Exception as error:
                create["status"] = "unknown"
                receipt["status"] = "unknown"
                save()
                raise AmbiguousDelivery("Document creation result is unknown; reconcile before another publication attempt") from error
            create.update(status="created", document_id=document_id, url=remote.get("url"))
            receipt["block_receipts"] = remote.get("new_blocks", [])
            save()
        document_id = create["document_id"]
        remote = transport.fetch(document_id)
        tree = _verify_text(plan, remote)
        for image in plan["images"]:
            fid = image["frame_id"]
            phase = receipt["images"].setdefault(fid, {"status": "planned"})
            if phase["status"] in ("inflight", "unknown", "inserted", "verified"):
                phase.update(_remote_image(tree, image, transport), status="verified")
                save()
                continue
            phase["status"] = "inflight"
            save()
            try:
                media = transport.insert_image(document_id, assets[fid], image["caption"], image["selection"])
            except Exception as error:
                phase["status"] = "unknown"
                receipt["status"] = "unknown"
                save()
                # The CLI may have completed before the timeout reached us.
                try:
                    tree = _verify_text(plan, transport.fetch(document_id))
                    phase.update(_remote_image(tree, image, transport), status="verified", reconciled=True)
                    save()
                    continue
                except Exception as reconcile_error:
                    raise AmbiguousDelivery("Image insertion result is unknown; remote inspection is required, no duplicate write was attempted") from reconcile_error
            phase.update(status="inserted", block_id=media.get("block_id"), file_token=media.get("file_token"))
            save()
            tree = _verify_text(plan, transport.fetch(document_id))
            phase.update(_remote_image(tree, image, transport), status="verified")
            save()
        # Fetch latest again, including count verification after all media actions.
        remote = transport.fetch(document_id)
        tree = _verify_text(plan, remote)
        if len(list(tree.iter("img"))) != len(plan["images"]):
            raise DeliveryError("Remote image count mismatch; publication is incomplete")
        for image in plan["images"]:
            receipt["images"][image["frame_id"]].update(_remote_image(tree, image, transport), status="verified")
        url = remote.get("url") or create.get("url")
        if not url and hasattr(transport, "document_url"):
            url = transport.document_url(document_id, note["title"])
            create["url"] = url
            save()
        if not url:
            raise DeliveryError("Remote service did not provide a document URL; publication is not verified")
        from ..contracts import safe_url
        receipt.update(status="verified", url=safe_url(url), document_id=document_id,
                       remote_revision=remote.get("revision_id"), readback={"content": True, "media_sha256": True, "image_count": len(plan["images"])})
        save()
        return receipt


def publish_run(run_dir, revision=None, **kwargs):
    from ..notes import load_note
    return publish_note(load_note(run_dir, revision=revision), run_dir, **kwargs)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True)
    parser.add_argument("--destination", choices=["feishu"], default="feishu")
    parser.add_argument("--revision", type=int)
    parser.add_argument("--target", default="new", help="new or folder:TOKEN; existing documents are never overwritten")
    parser.add_argument("--reconcile-document", help="explicitly reconcile an ambiguous create; must contain this frozen note")
    args = parser.parse_args(argv)
    try:
        receipt = publish_run(args.run, revision=args.revision, target=args.target, reconcile_document=args.reconcile_document)
    except (DeliveryError, ValueError, OSError, KeyError) as error:
        parser.exit(1, f"Feishu publication incomplete: {error}\n")
    print(json.dumps({"status": receipt["status"], "url": receipt["url"], "document_id": receipt["document_id"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
