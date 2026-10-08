"""Frozen evidence, versioning and equivalent offline deliveries."""
import copy
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from video_notes import notes
from video_notes.delivery import html, markdown
from video_notes.run import save


class StudyNotes(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'run'; self.root.mkdir()
        (self.root / 'frames').mkdir()
        for fid in ('f0001', 'f0002'):
            shutil.copyfile(Path(__file__).with_name('fixture.jpg'), self.root / 'frames' / f'{fid}.jpg')
        self.run = {'run_id': 'note-test', 'status': 'finished', 'processed_range': [100, 180],
                    'requested_range': [100, 240], 'remaining_range': [180, 240], 'video_duration_seconds': 300,
                    'preset': 'economy', 'content_source': 'provided', 'transcript_status': 'ready',
                    'packs': [{'id': 'p001', 'kind': 'overview', 'frame_ids': ['f0001'],
                               'segment_ids': ['s0001'], 'claimed_read': True, 'text_chars': 40, 'images': 1},
                              {'id': 'p002', 'kind': 'detail', 'frame_ids': ['f0002'],
                               'segment_ids': ['s0002'], 'claimed_read': False, 'text_chars': 40, 'images': 1}],
                    'frames': [{'id': 'f0001', 'path': str(self.root / 'frames/f0001.jpg'), 'timestamp': 120, 'kind': 'overview'},
                               {'id': 'f0002', 'path': str(self.root / 'frames/f0002.jpg'), 'timestamp': 160, 'kind': 'detail'}]}
        self.usage = {'run_id': 'note-test', 'result_status': 'partial', 'elapsed_seconds': 10,
                      'content_source': 'provided', 'video': {'url': 'https://www.youtube.com/watch?v=test1234567', 'processed_minutes': 1.333},
                      'materials': {'overview_frames_extracted': 1, 'detail_frames_extracted': 1},
                      'tokens': {'native_actual': None, 'material_text_estimate': 200},
                      'cost': {'api_usd': None}, 'api_calls': [], 'run_directory': '/secret/path',
                      'session_log': '/secret/log'}
        source = {'source': {'platform': 'youtube', 'canonical_url': self.usage['video']['url'], 'signed_url': 'private'},
                  'metadata': {'media_id': 'test1234567', 'title': 'Synthetic course'}, 'selection': {'part_id': 'test1234567'}}
        save(self.root / 'source.json', source)
        save(self.root / 'segments.json', [{'id': 's0001', 'start': 100, 'end': 140, 'text': 'First concept'},
                                          {'id': 's0002', 'start': 140, 'end': 180, 'text': 'Unseen concept'}])
        self.save_run()
        self.data = {'schema_version': 2, 'title': 'Synthetic course', 'takeaway': 'One supported concept.',
                     'takeaway_evidence_refs': ['segment:s0001'],
                     'sections': [{'id': 'concept', 'title': 'Concept', 'evidence_refs': ['segment:s0001'], 'blocks': [
                         {'type': 'paragraph', 'text': 'Speaker content <script>evil()</script>', 'evidence_refs': ['segment:s0001']},
                         {'type': 'list', 'items': ['First', 'Second'], 'ordered': True, 'evidence_refs': ['segment:s0001']},
                         {'type': 'code', 'text': 'print("<tag>")\n```', 'language': 'python', 'evidence_refs': [], 'attribution': 'agent'},
                         {'type': 'formula', 'text': 'x = a + b', 'evidence_refs': ['segment:s0001']},
                         {'type': 'table', 'headers': ['Name', 'Value'], 'rows': [['<script>', 'a|b']], 'evidence_refs': ['segment:s0001']},
                         {'type': 'image', 'frame_id': 'f0001', 'title': 'Diagram', 'caption': 'A synthetic image', 'evidence_refs': ['frame:f0001']}
                     ]}],
                     'timeline': [{'start': 100, 'end': 140, 'title': 'Concept', 'text': 'Explanation', 'section_id': 'concept',
                                   'evidence_refs': ['segment:s0001']}],
                     'figures': [{'frame_id': 'f0001', 'title': 'Figure', 'caption': 'Visible sample', 'evidence_refs': ['frame:f0001']}],
                     'caveats': ['Only the first interval was covered.'],
                     'sources': [{'label': 'Reference', 'url': 'https://example.org/paper'}]}

    def save_run(self):
        save(self.root / 'run.json', self.run); save(self.root / 'usage.json', self.usage)

    def test_versions_and_identical_retry_are_immutable(self):
        first = notes.save_note(self.root, self.data)
        original = (self.root / 'notes/note-v001.json').read_bytes()
        retry = notes.save_note(self.root, copy.deepcopy(self.data))
        self.assertEqual(first, retry)
        self.data['takeaway'] = 'Revised concept.'
        second = notes.save_note(self.root, self.data)
        self.assertEqual(second['revision'], 2)
        self.assertEqual((self.root / 'notes/note-v001.json').read_bytes(), original)
        self.assertEqual(notes.load_note(self.root, 1)['takeaway'], 'One supported concept.')

    def test_snapshot_is_sanitized_and_read_declarations_are_explicit(self):
        note = notes.save_note(self.root, self.data)
        self.assertEqual([f['id'] for f in note['snapshot']['frames']], ['f0001'])
        self.assertEqual(note['snapshot']['reading'][0]['segment_ids'], ['s0001'])
        encoded = json.dumps(note)
        self.assertNotIn('/secret', encoded); self.assertNotIn('signed_url', encoded)
        self.assertNotIn(str(self.root), encoded)
        frame = note['snapshot']['frames'][0]
        self.assertEqual(hashlib.sha256(notes.frame_asset(self.root, note, frame['id']).read_bytes()).hexdigest(), frame['sha256'])

    def test_unknown_and_unread_evidence_rejected(self):
        for ref in ['frame:f0002', 'segment:s0002', 'segment:missing']:
            with self.subTest(ref=ref):
                data = copy.deepcopy(self.data); data['takeaway_evidence_refs'] = [ref]
                with self.assertRaisesRegex(ValueError, 'unavailable|read'):
                    notes.save_note(self.root, data)

    def test_outside_partial_range_and_unknown_section_rejected(self):
        data = copy.deepcopy(self.data); data['timeline'][0]['end'] = 181
        with self.assertRaisesRegex(ValueError, 'interval'):
            notes.save_note(self.root, data)
        data = copy.deepcopy(self.data); data['timeline'][0]['section_id'] = 'missing'
        with self.assertRaisesRegex(ValueError, 'section'):
            notes.save_note(self.root, data)
        self.run['frames'][0]['timestamp'] = 200; self.save_run()
        with self.assertRaisesRegex(ValueError, 'interval'):
            notes.save_note(self.root, self.data)

    def test_exports_are_independent_of_mutable_source_run_and_models(self):
        note = notes.save_note(self.root, self.data)
        for filename in ['run.json', 'source.json', 'segments.json', 'usage.json']:
            (self.root / filename).unlink()
        shutil.rmtree(self.root / 'frames')
        with patch('video_notes.sources.media_input', side_effect=AssertionError('Media must not be fetched')), \
             patch('subprocess.run', side_effect=AssertionError('No process/model calls')):
            first = notes.export_note(self.root, ('html', 'md'))
            html_bytes = Path(first['outputs']['html']).read_bytes()
            md_bytes = Path(first['outputs']['md']).read_bytes()
            second = notes.export_note(self.root, ('html', 'md'))
        self.assertEqual(html_bytes, Path(second['outputs']['html']).read_bytes())
        self.assertEqual(md_bytes, Path(second['outputs']['md']).read_bytes())
        self.assertEqual(first['model_calls'], 0)
        self.assertEqual(notes.load_note(self.root)['content_hash'], note['content_hash'])
        self.assertIn('尚有未处理区间', html_bytes.decode())
        self.assertNotIn('讲解全段', html_bytes.decode())
        self.assertIn('不可得', html_bytes.decode())
        self.assertIn('文字材料粗估', md_bytes.decode())

    def test_snapshot_and_asset_tampering_rejected(self):
        note = notes.save_note(self.root, self.data)
        frame = notes.frame_asset(self.root, note, 'f0001')
        original = frame.read_bytes(); frame.write_bytes(original + b'x')
        with self.assertRaisesRegex(ValueError, 'hash'):
            notes.load_note(self.root)
        frame.write_bytes(original)
        path = self.root / 'notes/note-v001.json'
        changed = json.loads(path.read_text()); changed['snapshot']['run']['processed_range'][1] = 240
        save(path, changed)
        with self.assertRaisesRegex(ValueError, 'modified'):
            notes.load_note(self.root)

    def test_markdown_assets_survive_directory_move(self):
        notes.save_note(self.root, self.data)
        result = notes.export_note(self.root, ('md',))
        output = Path(result['outputs']['md'])
        asset_folder = output.parent / (output.stem + '-assets')
        self.assertEqual(len(list(asset_folder.glob('*.jpg'))), 1)
        moved = Path(self.temp.name) / 'moved'
        shutil.copytree(output.parent, moved)
        markdown_text = (moved / output.name).read_text()
        self.assertIn(f']({output.stem}-assets/', markdown_text)
        self.assertTrue(next((moved / asset_folder.name).glob('*.jpg')).is_file())
        self.assertNotIn(str(self.root), markdown_text)

    def test_text_only_keeps_caption_without_new_assets(self):
        note = notes.save_note(self.root, self.data)
        output = Path(self.temp.name) / 'plain.md'
        result = markdown.render_note(self.root, note, output, text_only=True)
        self.assertIn('Visible sample', result); self.assertIn('A synthetic image', result)
        self.assertNotIn('![', result); self.assertFalse(output.with_name('plain-assets').exists())

    def test_html_and_markdown_support_every_block_without_unsafe_markup(self):
        note = notes.save_note(self.root, self.data)
        rendered_html = html.render_note(self.root, note)
        rendered_md = markdown.render_note(self.root, note, Path(self.temp.name) / 'test.md')
        for tag in ['<pre>', '<table>', '<ol>', 'data:image/jpeg;base64,']:
            self.assertIn(tag, rendered_html)
        self.assertNotIn('<script>', rendered_html); self.assertNotIn('<script>', rendered_md)
        self.assertIn('````python', rendered_md)
        self.assertIn('```math', rendered_md)
        self.assertIn('a\\|b', rendered_md)
        self.assertIn('?v=test1234567&amp;t=100', rendered_html)
        self.assertIn('?v=test1234567&t=100', rendered_md)

    def test_urls_and_paths_are_checked(self):
        for url in ['javascript:alert(1)', 'https://u:secret@example.org', 'https://example.org\nunsafe']:
            data = copy.deepcopy(self.data); data['sources'][0]['url'] = url
            with self.assertRaises(ValueError):
                notes.save_note(self.root, data)
        self.run['frames'][0]['path'] = str(Path(__file__).with_name('fixture.jpg')); self.save_run()
        with self.assertRaisesRegex(ValueError, 'frames folder'):
            notes.save_note(self.root, self.data)

    def test_frame_folder_symlink_cannot_escape_run(self):
        outside = Path(self.temp.name) / 'outside'; shutil.move(self.root / 'frames', outside)
        (self.root / 'frames').symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'frames folder'):
            notes.save_note(self.root, self.data)

    def test_explicit_legacy_import_preserves_historical_files(self):
        legacy = {'title': 'Historical', 'takeaway': 'Old summary',
                  'sections': [{'title': 'Old chapter', 'paragraphs': ['One'], 'bullets': ['Two']}],
                  'timeline': [{'start': 100, 'end': 140, 'title': 'Old', 'text': 'Existing'}],
                  'visuals': [{'frame_id': 'f0001', 'title': 'Picture', 'caption': 'Old caption'}]}
        save(self.root / 'summary.json', legacy)
        historical = {filename: (self.root / filename).read_bytes() for filename in ['summary.json', 'run.json', 'usage.json']}
        imported = notes.save_note(self.root, legacy, legacy=True)
        self.assertEqual(imported['provenance']['kind'], 'legacy_summary_import')
        self.assertIn('semantic review', imported['provenance']['reference_policy'])
        self.assertEqual(imported['timeline'][0]['evidence_refs'], ['segment:s0001'])
        self.assertEqual(imported['figures'][0]['evidence_refs'], ['frame:f0001'])
        for filename, value in historical.items():
            self.assertEqual((self.root / filename).read_bytes(), value)
        self.assertEqual(notes.export_note(self.root, ('html', 'md'))['revision'], 1)

    def test_block_refs_are_explicit_even_for_agent_commentary(self):
        data = copy.deepcopy(self.data); del data['sections'][0]['blocks'][0]['evidence_refs']
        with self.assertRaisesRegex(ValueError, 'explicit evidence_refs'):
            notes.save_note(self.root, data)

    def test_malformed_nodes_and_nonfinite_timeline_rejected(self):
        candidates = []
        for key, value in [('sections', {}), ('timeline', ['bad']), ('figures', 'bad'), ('caveats', 'bad')]:
            data = copy.deepcopy(self.data); data[key] = value; candidates.append(data)
        data = copy.deepcopy(self.data); data['sections'][0]['blocks'][1]['ordered'] = 'false'; candidates.append(data)
        data = copy.deepcopy(self.data); data['sections'][0]['blocks'][4]['rows'] = [['wrong width']]; candidates.append(data)
        for value in [float('nan'), float('inf'), True]:
            data = copy.deepcopy(self.data); data['timeline'][0]['start'] = value; candidates.append(data)
        for data in candidates:
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    notes.save_note(self.root, data)

    def test_entire_snapshot_can_move_without_absolute_paths(self):
        notes.save_note(self.root, self.data)
        destination = Path(self.temp.name) / 'moved-note'
        destination.mkdir()
        shutil.copytree(self.root / 'notes', destination / 'notes')
        shutil.rmtree(self.root)
        moved_note = notes.load_note(destination)
        result = notes.export_note(destination, ('html', 'md'))
        self.assertEqual(moved_note['revision'], 1)
        self.assertTrue(Path(result['outputs']['html']).is_file())
        self.assertTrue(Path(result['outputs']['md']).is_file())

    def test_note_and_export_cli(self):
        path = Path(self.temp.name) / 'input.json'; save(path, self.data)
        self.assertEqual(notes.main(['note', '--run', str(self.root), '--input', str(path)]), 0)
        self.assertEqual(notes.main(['export', '--run', str(self.root), '--format', 'html,md']), 0)
        self.assertEqual(notes.main(['export', '--run', str(self.root), '--format', 'xml']), 1)


if __name__ == '__main__':
    unittest.main()
