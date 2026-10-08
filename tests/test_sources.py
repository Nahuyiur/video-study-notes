import unittest
from video_notes.contracts import source_record
from video_notes.sources import normalize_source, resolve_source, timestamp_url


class Sources(unittest.TestCase):
    def test_provider_fields_stay_at_boundary(self):
        raw = {"source": {"platform": "bilibili", "canonical_url": "https://www.bilibili.com/video/BV123/?p=2"},
               "metadata": {"bvid": "BV123", "title": "课", "duration_seconds": 999,
                            "pages": [{"page": 2, "duration_seconds": 120}]},
               "selection": {"page": 2, "cid": 456}, "content": {"source_type": "none"}}
        result = normalize_source(raw)
        self.assertEqual(result["metadata"]["media_id"], "BV123")
        self.assertEqual(result["selection"]["part_id"], "456")
        self.assertEqual(result["metadata"]["duration_seconds"], 120)
        self.assertNotIn("cid", result["selection"])
        self.assertNotIn("bvid", result["metadata"])

    def test_links_share_original_seconds(self):
        self.assertIn("p=2&t=12", timestamp_url("https://www.bilibili.com/video/BV123/?p=2&t=9", 12.9))
        self.assertIn("v=abc&t=12", timestamp_url("https://www.youtube.com/watch?v=abc", 12.9))

    def test_credentials_and_nonfinite_duration_rejected(self):
        with self.assertRaises(ValueError): source_record("local", "x", "1", "x", float("nan"))
        with self.assertRaises(ValueError): timestamp_url("https://user:secret@example.com/", 1)
        with self.assertRaises(ValueError): resolve_source("https://example.com/video")
