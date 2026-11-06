import copy
import math
import re
import threading
import time
from .measurement import METHOD, DEFAULT_RULES, SquatDetector, baseline_values, validate_rules

DEFAULT_CAPTURE={'pi_side':'left','pi_model':'Raspberry Pi Camera Module 3 (IMX708)',
                 'pi_lens':'unspecified','pi_resolution':'640x480-letterbox',
                 'phone_model':'브라우저 카메라 (기기 모델 미확인)','phone_lens':'front',
                 'phone_facing':'user','method':METHOD,'rules':dict(DEFAULT_RULES)}


def participant_id(value):
    if not isinstance(value,str) or not re.fullmatch(r'[\w가-힣-]{1,40}',value,re.UNICODE):
        raise ValueError('참가자 ID는 문자·숫자·밑줄·하이픈 1~40자로 입력하세요.')
    return value


class Runtime:
    def __init__(self,store,clock=time.monotonic):
        self.store=store;self.clock=clock;self.lock=threading.RLock()
        self.capture=store.settings(DEFAULT_CAPTURE)
        self.phase='idle';self.pending=None;self.session=None;self.last_saved=None
        self.baseline=None;self.reps=[];self.detector=SquatDetector()
        self.current={};self.last_seen={};self.last_missing={};self.started=None
        self.store.recover()

    def configure(self,value):
        with self.lock:
            if self.phase in ('countdown','measuring'):raise ValueError('대기·측정 중에는 촬영 조건을 바꿀 수 없습니다.')
            allowed={'pi_side','pi_lens','phone_facing','rules'}
            if not set(value)<=allowed:raise ValueError('지원하지 않는 촬영 설정입니다.')
            if 'pi_side' in value and value['pi_side'] not in ('left','right'):raise ValueError('촬영 방향을 확인하세요.')
            if 'pi_lens' in value and value['pi_lens'] not in ('standard','wide','unspecified'):raise ValueError('렌즈 프로필을 확인하세요.')
            if 'phone_facing' in value and value['phone_facing'] not in ('user','environment'):raise ValueError('휴대폰 방향을 확인하세요.')
            if 'rules' in value:value={**value,'rules':validate_rules(value['rules'])}
            self.capture.update(value)
            self.capture['phone_lens']='front' if self.capture['phone_facing']=='user' else 'rear-unspecified'
            self.store.set_settings(self.capture)
            return dict(self.capture)

    def prepare(self,participant,delay,rules=None):
        with self.lock:
            if self.phase in ('countdown','measuring'):raise ValueError('이미 대기 또는 측정 중입니다.')
            participant=participant_id(participant)
            if delay not in (0,3,5,10):raise ValueError('대기 시간은 0·3·5·10초 중 선택하세요.')
            if self.capture['pi_lens']=='unspecified':raise ValueError('Pi 카메라의 일반형/Wide 렌즈 프로필을 먼저 선택하세요.')
            self.store.participant(participant)
            rules=validate_rules(rules or self.capture['rules'])
            conditions=copy.deepcopy(self.capture);conditions['rules']=rules
            self.pending={'participant':participant,'conditions':conditions,
                          'rules':rules,'deadline':self.clock()+delay}
            self.phase='countdown'
            self.tick()
            return self.status()

    def cancel(self):
        with self.lock:
            if self.phase!='countdown':raise ValueError('취소할 시작 대기가 없습니다.')
            self.pending=None;self.phase='idle'
            return self.status()

    def tick(self):
        with self.lock:
            now=self.clock()
            if self.phase=='countdown' and now>=self.pending['deadline']:
                p=self.pending
                sid=self.store.start(p['participant'],p['conditions'],p['rules'])
                self.session={'id':sid,**copy.deepcopy(p)};self.pending=None;self.started=now
                self.reps=[];self.detector=SquatDetector(p['rules']);self.current={};self.last_seen={};self.last_missing={}
                self.baseline=self.store.latest_baseline(p['participant'],p['conditions'])
                self.phase='measuring'
                self.store.event(sid,0,'measurement_started',{'baseline_id':self.baseline['id'] if self.baseline else None})
            if self.phase=='measuring':
                elapsed=now-self.started
                for channel in ('pi','phone'):
                    seen=self.last_seen.get(channel,self.started)
                    if now-seen>self.detector.rules['max_gap'] and now-self.last_missing.get(channel,0)>1:
                        payload={'valid':False,'knee':None,'hip':None,'trunk':None,'reason':'input_gap','missing':True}
                        self.store.sample(self.session['id'],channel,elapsed,payload)
                        self.last_missing[channel]=now;self.current[channel]=payload
                        if channel=='pi':self.detector.invalidate('입력 공백 · 선 자세부터 다시 준비해 주세요.')

    def ingest(self,channel,metrics,guide,facing=None):
        with self.lock:
            self.tick()
            if self.phase!='measuring':return
            now=self.clock();t=now-self.started;self.last_seen[channel]=now
            valid=bool(metrics.get('valid')) and guide.get('ready',False)
            reason=metrics.get('reason') if not metrics.get('valid') else guide.get('reason')
            if channel=='phone' and facing and facing!=self.session['conditions']['phone_facing']:
                valid=False;reason='capture_condition_changed'
            payload={key:metrics.get(key) if valid else None for key in ('knee','hip','trunk')}
            payload.update(valid=valid,reason=reason or ('ready' if valid else 'not_detected'),missing=False)
            self.current[channel]=payload
            self.store.sample(self.session['id'],channel,t,payload)
            if channel=='pi':
                rep=self.detector.update(t,payload['knee'],valid,self.baseline)
                if rep:
                    rep=self.store.repetition(self.session['id'],rep);self.reps.append(rep)

    def save_baseline(self,confirmed):
        with self.lock:
            if self.phase!='measuring':raise ValueError('측정 중 완료한 최근 3회를 기준으로 저장하세요.')
            if confirmed is not True:raise ValueError('지도자와 최근 3회 동작을 확인한 뒤 체크해 주세요.')
            values=baseline_values(self.reps)
            self.baseline=self.store.baseline(self.session['participant'],self.session['conditions'],values,self.session['id'],
                                              self.clock()-self.started,[x['id'] for x in self.reps[-3:]])
            return self.baseline

    def stop(self,interrupted=False):
        with self.lock:
            if self.phase=='countdown':self.cancel();return None
            if self.phase!='measuring':return self.store.get(self.last_saved) if self.last_saved else None
            sid=self.session['id'];elapsed=self.clock()-self.started
            self.store.finish(sid,elapsed,'interrupted' if interrupted else 'completed')
            self.store.event(sid,elapsed,'measurement_stopped',{'unfinished_repetition_discarded':self.detector.state not in ('standing','waiting_standing')})
            self.last_saved=sid;self.phase='saved'
            return self.store.get(sid)

    def baseline_for(self,participant):
        participant=participant_id(participant)
        return self.store.latest_baseline(participant,self.capture)

    def status(self):
        with self.lock:
            running=self.phase=='measuring'
            return {'phase':self.phase,'running':running,
                    'elapsed':round(self.clock()-self.started,2) if running else 0,
                    'countdown':max(0,math.ceil(self.pending['deadline']-self.clock())) if self.pending else 0,
                    'participant':self.session['participant'] if running else (self.pending['participant'] if self.pending else None),
                    'session_id':self.session['id'] if running else None,'last_saved':self.last_saved,
                    'capture':dict(self.capture),'session_conditions':copy.deepcopy(self.session['conditions']) if running else None,
                    'count':len(self.reps) if running else 0,'repeat_state':self.detector.state,
                    'repeat_message':self.detector.reason,'current':copy.deepcopy(self.current) if running else {},
                    'last_repetition':copy.deepcopy(self.reps[-1]) if running and self.reps else None,
                    'baseline':copy.deepcopy(self.baseline) if running else None,
                    'elapsed_includes_holds':True,'rules':dict(self.detector.rules)}
