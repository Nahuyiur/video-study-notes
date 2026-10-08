"""Portable HTML checks: scope, escaping, viewed frames and offline images."""
import copy
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from html.parser import HTMLParser

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from video_notes.delivery import html as render_summary
from video_notes.notes import save_note


class Tags(HTMLParser):
    def __init__(self):
        super().__init__(); self.tags=[]; self.attrs=[]
    def handle_starttag(self, tag, attrs):
        self.tags.append(tag); self.attrs.extend(attrs)


class Summary(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.directory=Path(self.temp.name)
        folder=self.directory/'frames';folder.mkdir()
        frame=folder/'f0001.jpg'
        shutil.copyfile(Path(__file__).with_name('fixture.jpg'),frame)
        self.run={'run_id':'fixture','status':'finished','processed_range':[0,328],
                  'remaining_range':None,'video_duration_seconds':328,
                  'packs':[{'claimed_read':True,'frame_ids':['f0001']}],
                  'frames':[{'id':'f0001','path':str(frame),'timestamp':116}]}
        self.usage={'run_id':'fixture','result_status':'complete','content_source':'provided',
                    'elapsed_seconds':1,'api_calls':[],
                    'video':{'url':'https://www.bilibili.com/video/BV1mDHJ6xEBP/','processed_minutes':5.467},
                    'materials':{'overview_frames_extracted':1,'detail_frames_extracted':0},
                    'tokens':{'native_actual':None,'material_text_estimate':100},'cost':{'api_usd':None}}
        self.save()
        self.data={'title':'Portable video fixture','takeaway':'This is synthetic test content.',
                   'timeline':[{'start':0,'end':65,'title':'Intro','text':'Scope'},
                               {'start':114,'end':136,'title':'Concept','text':'Explanation'}],
                   'visuals':[{'frame_id':'f0001','title':'Example','caption':'Solid-color test image'}]}

    def save(self):
        for name,data in [('run.json',self.run),('usage.json',self.usage)]:
            (self.directory/name).write_text(json.dumps(data))

    def render(self, data=None):
        # Historical fields are explicitly imported before entering the sole renderer.
        note = save_note(self.directory, data if data is not None else self.data, legacy=True)
        return render_summary.render_note(self.directory, note)

    def test_portable_images_and_no_external_runtime(self):
        result=self.render()
        parser=Tags();parser.feed(result)
        images=[v for k,v in parser.attrs if k=='src']
        self.assertEqual(len(images),1)
        self.assertTrue(all(v.startswith('data:image/jpeg;base64,') for v in images))
        self.assertNotIn('script',parser.tags);self.assertNotIn('link',parser.tags)

    def test_source_markup_never_executes(self):
        self.data['takeaway']='<script>alert(1)</script><img src=x onerror=alert(2)>'
        result=self.render()
        parser=Tags();parser.feed(result)
        self.assertNotIn('script',parser.tags)
        self.assertFalse(any(k.startswith('on') for k,v in parser.attrs))
        self.assertIn('&lt;script&gt;',result)

    def test_unsafe_source_link_rejected(self):
        self.data['sources']=[{'label':'x','url':'javascript:alert(1)'}]
        with self.assertRaises(ValueError):self.render()

    def test_unread_frame_rejected(self):
        self.data['visuals'][0]['frame_id']='unread'
        with self.assertRaises(ValueError):self.render()

    def test_timeline_cannot_claim_outside_range(self):
        self.data['timeline'][0]['end']=1000
        with self.assertRaises(ValueError):self.render()

    def test_unknown_tokens_stay_unknown_and_jump_uses_seconds(self):
        result=self.render()
        self.assertIn('不可得',result);self.assertIn('文字材料粗估',result)
        self.assertIn('?t=114',result)

    def test_partial_scope_remains_visible(self):
        self.run['processed_range']=[0,65];self.run['remaining_range']=[65,328]
        self.usage['result_status']='partial';self.run['frames']=[];self.run['packs']=[];self.save()
        source=copy.deepcopy(self.data);source['timeline']=source['timeline'][:1];source['visuals']=[]
        result=self.render(source)
        self.assertIn('尚有未处理区间',result);self.assertNotIn('讲解全段',result)


if __name__=='__main__':unittest.main(verbosity=2)
