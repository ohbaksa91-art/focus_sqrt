import csv
import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from app.measurement import SquatDetector,reproduction_score
from app.storage import Store
from app.runtime import Runtime
from app.analysis import projected_metrics,framing_guide


class Clock:
    def __init__(self):self.value=0.
    def __call__(self):return self.value


class ResearchFlow(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'test.sqlite3'
        self.store=Store(self.path);self.clock=Clock();self.r=Runtime(self.store,self.clock)
        self.r.configure({'pi_side':'left','pi_lens':'standard'})
    def tearDown(self):self.store.close();self.temp.cleanup()
    def frame(self,dt,knee,valid=True):
        self.clock.value+=dt
        self.r.ingest('pi',{'valid':valid,'knee':knee if valid else None,'hip':120 if valid else None,'trunk':20 if valid else None,'reason':'ready' if valid else 'occluded'},
                      {'ready':valid,'reason':'ready' if valid else 'occluded'})
    def repetition(self):
        # Explicit first descent and returned-standing times; confirmation is later.
        for dt,value in [(.3,170),(.3,170),(.3,148),(.4,130),(.4,100),(.4,125),(.4,165),(.3,170)]:self.frame(dt,value)

    def test_required_end_to_end_flow_and_versions(self):
        self.r.prepare('A',0)
        for _ in range(3):self.repetition()
        self.assertEqual(len(self.r.reps),3)
        self.assertTrue(all(x['score'] is None for x in self.r.reps))
        with self.assertRaises(ValueError):self.r.save_baseline(False)
        b1=self.r.save_baseline(True)
        self.assertAlmostEqual(b1['rom'],65)
        self.assertAlmostEqual(b1['duration'],1.6)
        self.repetition();self.assertEqual(self.r.reps[-1]['score'],100)
        self.assertEqual(self.r.reps[-1]['baseline_id'],b1['id'])
        # Change the baseline DURING a rep. That rep keeps the descent-time version.
        self.frame(.3,148)
        b2=self.r.save_baseline(True)
        for dt,value in [(.4,100),(.4,120),(.4,165),(.3,170)]:self.frame(dt,value)
        self.assertEqual(self.r.reps[-1]['baseline_id'],b1['id'])
        self.repetition();self.assertEqual(self.r.reps[-1]['baseline_id'],b2['id'])
        sid=self.r.stop()['id'];s=self.store.get(sid)
        self.assertEqual(s['summary']['count'],6)
        self.assertEqual(s['summary']['scored_repetitions'],3)
        self.assertIsNone(s['repetitions'][0]['score'])
        self.assertEqual(len([e for e in s['events'] if e['kind']=='baseline_saved']),2)
        self.r.prepare('A',0);self.assertEqual(self.r.baseline['id'],b2['id'])
        self.r.stop()
        self.r.prepare('B',0);self.assertIsNone(self.r.baseline);self.r.stop()
        self.r.configure({'pi_side':'right'});self.r.prepare('A',0);self.assertIsNone(self.r.baseline)
        with self.assertRaises(ValueError):self.r.configure({'pi_side':'left'})
        right=self.r.stop();self.assertEqual(right['conditions']['pi_side'],'right')
        self.r.configure({'pi_side':'left','pi_lens':'wide'});self.r.prepare('A',0);self.assertIsNone(self.r.baseline);self.r.stop()
        # Past records stay left/standard despite current right/Wide changes.
        self.assertEqual(self.store.get(sid)['conditions']['pi_lens'],'standard')
        self.assertEqual(self.store.get(sid)['conditions']['pi_side'],'left')
        self.assertEqual(len(self.store.history('B')),1)
        from app.main import session_csv
        csv_text=session_csv(s).body.decode('utf-8-sig')
        rows=list(csv.DictReader(io.StringIO(csv_text)))
        repetitions=[x for x in rows if x['record_type']=='repetition']
        self.assertEqual(len(repetitions),6)
        self.assertEqual([x['score'] for x in repetitions[:3]],['','',''])
        self.assertEqual(repetitions[3]['baseline_id'],b1['id'])
        self.assertEqual(repetitions[-1]['baseline_id'],b2['id'])
        self.assertAlmostEqual(float(repetitions[0]['rom_2d']),65)
        self.assertAlmostEqual(float(repetitions[0]['duration_s']),1.6)
        self.assertEqual({x['pi_lens'] for x in rows},{'standard'})

    def test_bad_frames_gaps_partial_and_recovery(self):
        self.r.prepare('A',0)
        for dt,value in [(.3,170),(.3,170),(.3,145),(.4,100)]:self.frame(dt,value)
        self.frame(.2,None,False)
        for dt,value in [(.3,165),(.3,170)]:self.frame(dt,value)
        self.assertEqual(len(self.r.reps),0)
        # Partial movement never reaches configured depth.
        for dt,value in [(.3,148),(.4,125),(.4,165),(.3,170)]:self.frame(dt,value)
        self.assertEqual(len(self.r.reps),0)
        self.frame(.3,148);self.frame(.3,100)
        self.clock.value+=4;self.r.tick()
        self.frame(.1,165);self.frame(.3,170)
        self.assertEqual(len(self.r.reps),0)
        self.repetition();self.assertEqual(len(self.r.reps),1)
        result=self.r.stop();self.assertGreater(result['quality']['pi']['held_frames'],0)
        self.assertGreater(result['quality']['pi']['missing_markers'],0)
        self.assertTrue(any(not s['valid'] and s['knee'] is None for s in result['samples']))

    def test_countdown_zero_session_and_restart(self):
        self.r.prepare('A',3);self.assertEqual(self.r.phase,'countdown')
        self.assertEqual(len(self.store.history('A')),0)
        self.clock.value=2;self.r.tick();self.assertEqual(self.r.phase,'countdown')
        self.r.cancel();self.assertEqual(len(self.store.history('A')),0)
        self.r.prepare('A',5);self.clock.value=7;self.r.tick()
        empty=self.r.stop();self.assertEqual(empty['summary']['count'],0)
        self.assertIsNone(empty['summary']['mean_rom']);self.assertIsNone(empty['summary']['mean_score'])
        self.r.prepare('A',0);sid=self.r.session['id'];self.frame(.3,None,False)
        second=Store(self.path);new=Runtime(second,self.clock)
        interrupted=second.get(sid);self.assertEqual(interrupted['status'],'interrupted')
        self.assertAlmostEqual(interrupted['elapsed'],.3);second.close()

    def test_sides_projection_and_condition_rules(self):
        points=[SimpleNamespace(x=.5,y=.5,visibility=0,presence=1) for _ in range(33)]
        for i,x,y in [(0,.5,.1),(11,.5,.25),(23,.5,.5),(25,.5,.7),(27,.5,.9)]:
            points[i]=SimpleNamespace(x=x,y=y,visibility=.9,presence=1)
        self.assertTrue(framing_guide(points,'left')['ready'])
        self.assertFalse(framing_guide(points,'front')['ready'])
        self.assertFalse(framing_guide(points,'right')['ready'])
        m=projected_metrics(points,'left');self.assertEqual(m['knee'],180);self.assertEqual(m['trunk'],0)
        self.r.prepare('A',0)
        for _ in range(3):self.repetition()
        self.r.save_baseline(True);self.r.stop()
        self.r.configure({'rules':{'depth':100}});self.assertIsNone(self.r.baseline_for('A'))

    def test_front_input_never_adds_squat_count(self):
        self.r.prepare('A',0)
        for _ in range(3):
            for dt,knee in [(.3,170),(.3,170),(.3,148),(.4,100),(.4,165),(.3,170)]:
                self.clock.value+=dt
                self.r.ingest('phone',{'valid':True,'knee':knee,'hip':110,'trunk':20},{'ready':True,'reason':'ready'},'user')
        self.assertEqual(len(self.r.reps),0)
        self.repetition();self.assertEqual(len(self.r.reps),1)

    def test_short_jitter_and_score_nulls(self):
        d=SquatDetector()
        for t,knee in [(0,170),(.3,170),(.4,149),(.5,148),(.6,170),(.9,170)]:
            self.assertIsNone(d.update(t,knee,True))
        self.assertIsNone(reproduction_score(65,1.6,None))
        self.assertEqual(reproduction_score(65,1.6,{'rom':65,'duration':1.6}),100)
        self.assertIsNone(reproduction_score(None,1.6,{'rom':65,'duration':1.6}))

if __name__=='__main__':unittest.main()
