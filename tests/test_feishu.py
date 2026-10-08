"""Publication invariants: immutable revisions, verified media, no blind retry."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from video_notes.delivery.feishu import (AmbiguousDelivery, DeliveryError, LarkCLI,
                                         build_plan, publish_note, _digest, _text)


class FakeTransport:
    def __init__(self, picture):
        self.picture = picture
        self.documents = {}
        self.creates = self.inserts = self.fetches = self.downloads = 0
        self.create_timeout = self.image_timeout = self.image_failure = False
        self.mutate_content = None
        self.receipt_dir = None

    def create(self, content, parent_token):
        self.creates += 1
        if self.receipt_dir:
            receipt = json.loads(next(self.receipt_dir.glob("feishu-*.json")).read_text())
            if receipt["create"]["status"] != "inflight":
                raise AssertionError("Mutation occurred before durable inflight receipt")
        self.documents["doc1"] = {"document_id": "doc1", "url": "https://test.feishu.cn/docx/doc1", "content": content, "revision_id": 1}
        if self.create_timeout:
            raise TimeoutError("Remote create finished but response lost")
        return copy.deepcopy(self.documents["doc1"])

    def fetch(self, document_id):
        self.fetches += 1
        result = copy.deepcopy(self.documents[document_id])
        if self.mutate_content:
            result["content"] = self.mutate_content(result["content"])
        return result

    def insert_image(self, document_id, path, caption, selection):
        self.inserts += 1
        if self.receipt_dir:
            receipt = json.loads(next(self.receipt_dir.glob("feishu-*.json")).read_text())
            assert next(iter(receipt["images"].values()))["status"] == "inflight"
        if self.image_failure:
            raise TimeoutError("No conclusive remote result")
        self.documents[document_id]["content"] += f'<img id="blk1" token="token1" caption="{_text(caption)}"/>'
        if self.image_timeout:
            raise TimeoutError("Image inserted but response lost")
        return {"block_id": "blk1", "file_token": "token1"}

    def download_image(self, token):
        self.downloads += 1
        assert token == "token1"
        return self.picture


class FeishuTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.asset = self.directory / "picture.jpg"
        self.picture = (Path(__file__).parent / "fixture.jpg").read_bytes()
        self.asset.write_bytes(self.picture)
        digest = hashlib.sha256(self.picture).hexdigest()
        self.note = {"schema_version": 2, "note_id": "fixture", "revision": 1,
                     "content_hash": "c" * 64, "title": "验收 & 笔记", "takeaway": "核心：1 < 2。",
                     "sections": [{"title": "方法", "blocks": [
                         {"type": "paragraph", "text": "正文应当保留。"},
                         {"type": "list", "items": ["要点一", "要点二"], "ordered": False},
                         {"type": "code", "language": "python", "text": "print('检查')\nprint(2)"},
                         {"type": "formula", "text": "E = mc^2"},
                         {"type": "table", "headers": ["项目", "结果"], "rows": [["图像", "完整"]]}]}],
                     "timeline": [{"start": 10, "end": 20, "title": "演示", "text": "检查关键帧。"}],
                     "figures": [{"frame_id": "f1", "title": "自制色块", "caption": "仅用于验收。"}],
                     "caveats": ["测试素材。"], "sources": [],
                     "snapshot": {"source": {"platform": "bilibili", "canonical_url": "https://www.bilibili.com/video/BV1mDHJ6xEBP/"},
                                  "run": {"processed_range": [0, 60]},
                                  "usage": {"tokens": {"native_actual": None}, "cost": {"api_usd": None}},
                                  "frames": [{"id": "f1", "timestamp": 10, "sha256": digest, "claimed_read": True}]}}
        self.transport = FakeTransport(self.picture)
        self.receipt_dir = self.directory / "deliveries"
        self.transport.receipt_dir = self.receipt_dir

    def publish(self, **kwargs):
        return publish_note(self.note, self.directory, transport=self.transport,
                            asset_resolver=lambda *args: self.asset, **kwargs)

    def test_rich_mapping_escapes_text_and_preserves_links(self):
        plan = build_plan(self.note, self.note["snapshot"])
        self.assertIn("验收 &amp; 笔记", plan["content"])
        self.assertIn("1 &lt; 2", plan["content"])
        for tag in ("<table>", "<latex>", '<pre lang="python">', "<ul>"):
            self.assertIn(tag, plan["content"])
        self.assertIn("t=10", plan["content"])
        self.assertIn("实际总 token", plan["content"])
        self.assertIn("不可得", plan["content"])

    def test_all_deliveries_preserve_claim_attribution_and_original_time_evidence(self):
        from video_notes.notes import normalize_note, digest
        from video_notes.delivery import html as html_delivery, markdown
        import html as entities
        import xml.etree.ElementTree as ET
        snapshot = self.note["snapshot"]
        snapshot["segments"] = [
            {"id": "s1", "start": 5, "end": 15, "text": "讲者事实", "claimed_read": True},
            {"id": "s2", "start": 25, "end": 30, "text": "章节依据", "claimed_read": True},
            {"id": "s3", "start": 35, "end": 40, "text": "待核实依据", "claimed_read": True}]
        self.note["takeaway_evidence_refs"] = ["segment:s1"]
        section = self.note["sections"][0]
        section["evidence_refs"] = ["segment:s2"]
        section["blocks"][0]["text"] = "讲者事实"
        for block in section["blocks"]:
            block["evidence_refs"] = ["segment:s1"]
        section["blocks"] += [
            {"type": "paragraph", "text": "这是我的补充", "attribution": "agent", "evidence_refs": ["frame:f1"]},
            {"type": "paragraph", "text": "这个结论仍待确认", "attribution": "uncertain", "evidence_refs": ["segment:s3"]}]
        self.note["timeline"][0]["evidence_refs"] = ["segment:s2"]
        self.note["figures"][0]["evidence_refs"] = ["frame:f1"]
        asset_dir = self.directory / "notes/assets"
        asset_dir.mkdir(parents=True)
        sha = snapshot["frames"][0]["sha256"]
        (asset_dir / (sha + ".jpg")).write_bytes(self.picture)
        snapshot["frames"][0]["asset"] = "notes/assets/" + sha + ".jpg"
        normalized = normalize_note(self.note, snapshot)
        self.note = {**normalized, "note_id": "fixture", "revision": 1,
                     "content_hash": digest({**normalized, "snapshot": snapshot}), "snapshot": snapshot}
        receipt = self.publish()
        remote = self.transport.fetch(receipt["document_id"])["content"]
        tree = ET.fromstring("<doc>" + remote + "</doc>")
        paragraphs = ["".join(p.itertext()) for p in tree.iter("p")]
        # An explanation and uncertainty cannot become an unattributed speaker claim.
        self.assertIn("讲者事实", paragraphs)
        self.assertIn("Agent 解读：这是我的补充", paragraphs)
        self.assertIn("尚不确定：这个结论仍待确认", paragraphs)
        html_output = html_delivery.render_note(self.directory, self.note)
        md_output = markdown.render_note(self.directory, self.note, self.directory / "note.md", text_only=True)
        for output in (remote, html_output, md_output):
            visible = entities.unescape(output)
            for meaning in ("讲者事实", "这是我的补充", "这个结论仍待确认", "Agent 解读", "尚不确定"):
                self.assertIn(meaning, visible)
            for second in (5, 10, 25, 35):
                self.assertIn("t=" + str(second), visible)
        evidence = [p for p in paragraphs if p.startswith("证据：")]
        self.assertIn("证据：讲解 00:05–00:15", evidence)  # takeaway / speaker
        self.assertIn("证据：讲解 00:25–00:30", evidence)  # section / timeline
        self.assertIn("证据：讲解 00:35–00:40", evidence)  # uncertainty
        self.assertIn("证据：画面 00:10", evidence)  # agent explanation / figure
        self.assertEqual((self.transport.creates, self.transport.inserts), (1, 1))

    def test_success_verified_bytes_and_cached_publish_rechecks(self):
        receipt = self.publish()
        self.assertEqual(receipt["status"], "verified")
        self.assertEqual(receipt["readback"]["image_count"], 1)
        fetches = self.transport.fetches
        self.assertEqual(self.publish()["url"], receipt["url"])
        self.assertEqual((self.transport.creates, self.transport.inserts), (1, 1))
        self.assertGreater(self.transport.fetches, fetches)
        self.assertGreater(self.transport.downloads, 1)

    def test_create_timeout_stops_repeat_and_explicit_reconcile(self):
        self.transport.create_timeout = True
        with self.assertRaises(AmbiguousDelivery):
            self.publish()
        with self.assertRaises(AmbiguousDelivery):
            self.publish()
        self.assertEqual(self.transport.creates, 1)
        receipt = self.publish(reconcile_document="doc1")
        self.assertEqual(receipt["status"], "verified")
        self.assertTrue(receipt["create"]["reconciled"])
        self.assertEqual(self.transport.creates, 1)

    def test_inflight_create_after_crash_is_never_repeated(self):
        self.transport.create_timeout = True
        with self.assertRaises(AmbiguousDelivery):
            self.publish()
        path = next(self.receipt_dir.glob("feishu-*.json"))
        receipt = json.loads(path.read_text())
        receipt["create"]["status"] = "inflight"
        path.write_text(json.dumps(receipt))
        with self.assertRaises(AmbiguousDelivery):
            self.publish()
        self.assertEqual(self.transport.creates, 1)

    def test_image_timeout_reconciles_remote_bytes_without_duplicate(self):
        self.transport.image_timeout = True
        receipt = self.publish()
        self.assertTrue(receipt["images"]["f1"]["reconciled"])
        self.assertEqual(self.transport.inserts, 1)
        self.publish()
        self.assertEqual(self.transport.inserts, 1)

    def test_unknown_missing_image_stops_next_attempt(self):
        self.transport.image_failure = True
        with self.assertRaises(AmbiguousDelivery):
            self.publish()
        with self.assertRaises(AmbiguousDelivery):
            self.publish()
        self.assertEqual((self.transport.creates, self.transport.inserts), (1, 1))

    def test_verified_media_tampering_fails_readback(self):
        self.publish()
        self.transport.picture = b"wrong bytes"
        with self.assertRaises(DeliveryError):
            self.publish()
        self.assertEqual((self.transport.creates, self.transport.inserts), (1, 1))

    def test_missing_content_or_wrong_rich_counts_cannot_complete(self):
        self.transport.mutate_content = lambda xml: xml.replace("正文应当保留。", "被更改。")
        with self.assertRaises(DeliveryError):
            self.publish()
        self.assertEqual(self.transport.inserts, 0)
        self.transport.mutate_content = lambda xml: xml + "<table/>"
        with self.assertRaises(DeliveryError):
            self.publish()
        self.assertEqual(self.transport.creates, 1)

    def test_unrelated_reconciliation_document_rejected(self):
        self.transport.create_timeout = True
        with self.assertRaises(AmbiguousDelivery):
            self.publish()
        self.transport.documents["doc1"]["content"] = "<title>用户文档</title><p>不要修改</p>"
        with self.assertRaises(DeliveryError):
            self.publish(reconcile_document="doc1")
        self.assertEqual(self.transport.inserts, 0)

    def test_existing_document_targets_rejected_before_network(self):
        for target in ("doc1", "https://test.feishu.cn/docx/doc1", "folder:bad token"):
            with self.assertRaises(ValueError):
                self.publish(target=target)
        self.assertEqual(self.transport.creates, 0)

    def test_receipt_target_and_payload_mismatch_rejected(self):
        self.publish()
        path = next(self.receipt_dir.glob("feishu-*.json"))
        receipt = json.loads(path.read_text())
        receipt["target"] = "folder:other"
        path.write_text(json.dumps(receipt))
        with self.assertRaises(ValueError):
            self.publish()
        receipt["target"] = "new"
        path.write_text(json.dumps(receipt))
        self.note["takeaway"] = "变动必须有新的笔记版本"
        with self.assertRaises(ValueError):
            self.publish()

    def test_unread_or_changed_assets_stop_before_remote(self):
        self.note["snapshot"]["frames"][0]["claimed_read"] = False
        with self.assertRaises(ValueError):
            self.publish()
        self.note["snapshot"]["frames"][0]["claimed_read"] = True
        self.asset.write_bytes(b"changed")
        with self.assertRaises(ValueError):
            self.publish()
        self.assertEqual(self.transport.creates, 0)

    def test_layout_upgrade_reuses_frozen_delivery_plan(self):
        receipt = self.publish()
        original = build_plan
        def changed(*args):
            plan = original(*args)
            plan["content"] += "<p>新布局</p>"
            plan["payload_hash"] = _digest({"content": plan["content"], "images": plan["images"]})
            return plan
        with patch("video_notes.delivery.feishu.build_plan", side_effect=changed):
            self.assertEqual(self.publish()["url"], receipt["url"])
        self.assertEqual((self.transport.creates, self.transport.inserts), (1, 1))

    def test_cli_transport_uses_argv_and_stdout_envelope(self):
        ok = subprocess.CompletedProcess([], 0, json.dumps({"ok": True, "data": {"document": {"document_id": "doc1"}}}), "")
        with patch("shutil.which", return_value="lark-cli"), patch("subprocess.run", return_value=ok) as invoke:
            transport = LarkCLI()
            self.assertEqual(transport.create("<title>Safe</title>")["document_id"], "doc1")
            args, opts = invoke.call_args
            self.assertEqual(opts["input"], "<title>Safe</title>")
            self.assertEqual(args[0][:3], ["lark-cli", "docs", "+create"])
            self.assertNotIn("shell", opts)
            self.assertIn("user", args[0])

    def test_public_cli_publishes_frozen_note_and_records_activity(self):
        from contextlib import redirect_stdout
        import io
        from video_notes.cli import main
        verified = {"status": "verified", "url": "https://test.feishu.cn/docx/doc1", "document_id": "doc1"}
        with patch("video_notes.delivery.feishu.publish_run", return_value=verified) as publish, redirect_stdout(io.StringIO()):
            self.assertEqual(main(["publish", "--run", str(self.directory), "--destination", "feishu"]), 0)
        publish.assert_called_once_with(str(self.directory), revision=None, target="new", reconcile_document=None)
        row = json.loads((self.directory / "activities.jsonl").read_text())
        self.assertEqual((row["command"], row["status"]), ("publish", "completed"))
        self.assertIsNone(row["native_tokens"])

    def test_xml_runtime_failure_stops_before_external_mutation(self):
        import builtins
        original = builtins.__import__
        def unavailable(name, *args, **kwargs):
            if name == "xml.etree.ElementTree":
                raise ImportError("broken parser runtime")
            return original(name, *args, **kwargs)
        with patch("builtins.__import__", side_effect=unavailable):
            with self.assertRaisesRegex(DeliveryError, "working Python"):
                self.publish()
        self.assertEqual((self.transport.creates, self.transport.inserts), (0, 0))

    def test_cli_missing_url_search_matches_document_token(self):
        response = subprocess.CompletedProcess([], 0, json.dumps({"ok": True, "data": {"results": [
            {"result_meta": {"token": "wrong", "url": "https://test.feishu.cn/docx/wrong"}},
            {"result_meta": {"token": "doc1", "url": "https://test.feishu.cn/docx/doc1"}}], "has_more": False}}), "")
        with patch("shutil.which", return_value="lark-cli"), patch("subprocess.run", return_value=response):
            self.assertEqual(LarkCLI().document_url("doc1", "title"), "https://test.feishu.cn/docx/doc1")

    def test_real_cli_caption_newline_normalization(self):
        self.publish()
        content = self.transport.documents["doc1"]["content"]
        self.transport.documents["doc1"]["content"] = content.replace('token="token1"', 'src="token1"').replace('caption="图 1：自制色块。仅用于验收。"', 'caption="图 1：自制色块。仅用于验收。&#xA;"')
        self.assertEqual(self.publish()["status"], "verified")
        self.assertEqual(self.transport.inserts, 1)

    def test_cli_errors_do_not_expose_auth_secrets(self):
        bad = subprocess.CompletedProcess([], 1, "", json.dumps({"ok": False, "error": {"type": "authorization", "subtype": "missing_scope", "message": "secret=DO_NOT_PRINT"}}))
        with patch("shutil.which", return_value="lark-cli"), patch("subprocess.run", return_value=bad):
            with self.assertRaises(DeliveryError) as result:
                LarkCLI().fetch("doc1")
            self.assertNotIn("DO_NOT_PRINT", str(result.exception))


if __name__ == "__main__":
    unittest.main()
