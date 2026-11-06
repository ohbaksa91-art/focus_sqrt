import csv
import io
import os
import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path

_tmp=tempfile.TemporaryDirectory()
os.environ['FOCUS_DB_PATH']=str(Path(_tmp.name)/'api.sqlite3')
import cv2
from fastapi.testclient import TestClient
from app.main import app,engine,engines,runtime,store
from app.storage import condition_key


class ApiTests(unittest.TestCase):
    def test_api_models_save_reopen_csv_and_compare(self):
        with patch.object(engine,'worker',lambda:None),TestClient(app) as c:
            self.assertTrue(c.get('/api/status').json()['model_ready'])
            self.assertEqual(c.post('/api/start',json={}).status_code,400)
            self.assertEqual(c.post('/api/participants',json={'participant':'API-A'}).status_code,200)
            self.assertEqual(c.post('/api/settings',json={'pi_lens':'standard','pi_side':'left'}).status_code,200)
            self.assertEqual(c.post('/api/start',json={'participant':'API-A','delay':0}).status_code,200)
            self.assertEqual(c.post('/api/settings',json={'pi_side':'right'}).status_code,400)
            self.assertEqual(c.post('/api/stop',headers={'Origin':'https://bad.example'}).status_code,403)
            self.assertEqual(c.post('/api/frame',content=b'x',headers={'Content-Type':'image/jpeg'}).status_code,400)
            self.assertEqual(c.post('/api/frame',content=b'x'*1_000_001,headers={'Content-Type':'image/jpeg'}).status_code,413)
            image=os.environ.get('FOCUS_TEST_IMAGE')
            if image:
                frame=cv2.imread(image);ok,jpg=cv2.imencode('.jpg',frame);self.assertTrue(ok)
                thread=threading.Thread(target=lambda:engine.process(frame.copy()));thread.start()
                r=c.post('/api/frame',content=jpg.tobytes(),headers={'Content-Type':'image/jpeg','X-Focus-Client':'api-test','X-Focus-Facing':'user'})
                self.assertEqual(r.status_code,200,r.text);thread.join()
                self.assertTrue(c.get('/api/status').json()['both_connected'])
            first=c.post('/api/stop').json()['session'];sid=first['id']
            result=c.get('/api/sessions/'+sid).json();self.assertEqual(result['participant'],'API-A')
            self.assertIsNone(result['summary']['mean_score'])
            exported=c.get('/api/sessions/'+sid+'/csv');self.assertEqual(exported.status_code,200)
            rows=list(csv.DictReader(io.StringIO(exported.text.lstrip('\ufeff'))))
            self.assertEqual({r['pi_side'] for r in rows},{'left'})
            self.assertTrue(all('same interval' in r['time_definition'] for r in rows))
            self.assertEqual(c.get('/api/sessions/'+sid+'/json').json()['id'],sid)
            c.post('/api/settings',json={'pi_side':'right','pi_lens':'wide'})
            c.post('/api/start',json={'participant':'API-A','delay':0});second=c.post('/api/stop').json()['session']
            comparison=c.get('/api/compare',params={'first':sid,'second':second['id']}).json()
            self.assertFalse(comparison['comparable'])
            self.assertEqual(c.get('/api/sessions/'+sid).json()['conditions']['pi_lens'],'standard')
            history=c.get('/api/sessions',params={'participant':'API-A'}).json()['sessions'];self.assertEqual(len(history),2)
            self.assertEqual(c.get('/setup/certificate').status_code,200)
            self.assertEqual(c.post('/api/start',json={'participant':'API-B','delay':10}).status_code,200)
            self.assertEqual(c.post('/api/cancel').status_code,200)

if __name__=='__main__':unittest.main()
