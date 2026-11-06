import asyncio
import csv
import io
import json
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import cv2
import mediapipe as mp
import numpy as np
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from .analysis import metrics, framing_guide, projected_metrics
from .storage import Store
from .runtime import Runtime
from .camera import CSICamera, open_camera

ROOT = Path(__file__).resolve().parent.parent
store = Store(os.environ.get('FOCUS_DB_PATH', str(ROOT/'data/focus.sqlite3')))
runtime = Runtime(store)
EDGES = [(11,12),(11,13),(13,15),(12,14),(14,16),(11,23),(12,24),(23,24),(23,25),(25,27),(24,26),(26,28)]


class Engine:
    def __init__(self, name):
        self.name = name
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.thread = None
        self.detector = None
        self.jpeg = None
        self.csi_preview = None
        self.reset()
        self.error = None
        self.model_error = None
        self.last_frame = 0

    def reset(self):
        self.running = False
        self.started = None
        self.elapsed = 0
        self.samples = 0
        self.sums = {}
        self.counts = {}
        self.angles = {}
        self.detected = False
        self.measurement = {'valid': False, 'knee': None, 'hip': None, 'trunk': None, 'reason': 'not_detected'}
        self.facing = 'user'
        self.guide = {"ready": False, "message": "카메라에 전신이 보이도록 서 주세요."}

    def process(self, frame):
        with self.lock:
            height, width = frame.shape[:2]
            scale = min(640/width, 480/height)
            frame = cv2.resize(frame, (round(width*scale), round(height*scale)))
            dh, dw = 480-frame.shape[0], 640-frame.shape[1]
            frame = cv2.copyMakeBorder(frame, dh//2, dh-dh//2, dw//2, dw-dw//2, cv2.BORDER_CONSTANT)
            self.guide = {'ready': False, 'reason': 'not_detected', 'message': '사람을 인식하지 못했어요. 전신이 보이는지, 조명이 충분한지, 복장이 배경과 구분되는지 확인해 주세요.'}
            self.running = runtime.phase == 'measuring'
            self.measurement = {'valid': False, 'knee': None, 'hip': None, 'trunk': None, 'reason': 'not_detected'}
            self.angles = {}
            self.detected = False
            if self.detector:
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                result = self.detector.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb))
                if result.pose_landmarks:
                    self.detected = True
                    points = result.pose_landmarks[0]
                    view = runtime.capture['pi_side'] if self.name == 'pi' else 'front'
                    self.guide = framing_guide(points,view)
                    self.angles = metrics(points)
                    self.measurement = projected_metrics(points,view) if self.name == 'pi' else projected_metrics(points,'left')
                    if self.name == 'phone':
                        other = projected_metrics(points,'right')
                        if self.measurement['valid'] and other['valid']:
                            self.measurement = {**self.measurement,**{k:round((self.measurement[k]+other[k])/2,2) for k in ('knee','hip','trunk')}}
                        else:
                            self.measurement = {'valid':False,'knee':None,'hip':None,'trunk':None,'reason':'occluded'}
                    if self.running:
                        self.samples += 1
                        for key, value in self.angles.items():
                            self.sums[key] = self.sums.get(key, 0) + value
                            self.counts[key] = self.counts.get(key, 0) + 1
                    for a, b in EDGES:
                        if self.csi_preview is None and points[a].visibility > .6 and points[b].visibility > .6:
                            xy = lambda p: (int(p.x*640), int(p.y*480))
                            cv2.line(frame, xy(points[a]), xy(points[b]), (90,240,100), 2)
                            cv2.circle(frame, xy(points[a]), 4, (90,240,100), -1)
            runtime.ingest(self.name,self.measurement,self.guide,self.facing if self.name=='phone' else None)
            if self.csi_preview is None:
                ok, data = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
                if not ok:
                    return
                self.jpeg = data.tobytes()
            self.last_frame = time.time()
            self.error = None

    def worker(self):
        cap = None
        try:
            while not self.stop.is_set():
                if cap is None:
                    try:
                        cap = open_camera()
                        if isinstance(cap, CSICamera):
                            self.csi_preview = cap
                    except Exception:
                        logging.exception('Camera initialization failed')
                        with self.lock:
                            self.error = 'Pi 카메라 연결을 확인해 주세요. rpicam-hello --list-cameras로 점검할 수 있습니다.'
                        self.stop.wait(3)
                        continue
                ok, frame = cap.read()
                if not ok:
                    with self.lock:
                        self.error = 'Pi 카메라 모듈 연결을 확인해 주세요. 스마트폰 입력도 사용할 수 있습니다.'
                    self.csi_preview = None
                    cap.release()
                    cap = None
                    self.stop.wait(2)
                    continue
                try:
                    with self.lock:
                        self.process(frame)
                except Exception:
                    logging.exception('Frame processing failed')
                    with self.lock:
                        self.error = '분석 오류: 서버 로그를 확인해 주세요.'
                self.stop.wait(.20 if self.running else .55)
        finally:
            self.csi_preview = None
            if cap is not None:
                cap.release()

    def status(self):
        with self.lock:
            fresh = time.time() - self.last_frame < 4
            return dict(running=runtime.phase=='measuring', source=self.name, updated_at=self.last_frame,
                        measurement=self.measurement if fresh else {"valid":False,"knee":None,"hip":None,"trunk":None},facing=self.facing,
                        guide=self.guide if fresh else {"ready": False, "message": "카메라 연결을 기다립니다."},
                        camera=fresh, detected=self.detected if fresh else False,
                        error=self.error, model_error=self.model_error,
                        model_ready=self.detector is not None,
                        elapsed=runtime.status()['elapsed'],
                        samples=self.samples, angles=self.angles if fresh else {},
                        averages={k: round(v/self.counts[k],1) for k,v in self.sums.items()})

engines = {name: Engine(name) for name in ('pi', 'phone')}
engine = engines['pi']
phone_guard = threading.Lock()
phone_owner = None
phone_owner_seen = 0


def combined_status():
    channels = {name: item.status() for name, item in engines.items()}
    a, b = channels['pi'], channels['phone']
    return dict(**runtime.status(),
                model_ready=all(x['model_ready'] for x in channels.values()),
                channels=channels, both_connected=a['camera'] and b['camera'],
                frame_gap_ms=round(abs(a['updated_at']-b['updated_at'])*1000)
                    if a['camera'] and b['camera'] else None)



@asynccontextmanager
async def lifespan(app):
    for item in engines.values():
        try:
            item.detector = mp.tasks.vision.PoseLandmarker.create_from_options(
                mp.tasks.vision.PoseLandmarkerOptions(
                    base_options=mp.tasks.BaseOptions(model_asset_path=str(ROOT/'models/pose_landmarker_lite.task')),
                    running_mode=mp.tasks.vision.RunningMode.IMAGE, num_poses=1))
        except Exception:
            logging.exception('Model initialization failed: %s', item.name)
            item.model_error = '자세 모델 초기화 실패. 서버 로그를 확인하세요.'
    engine.stop.clear()
    engine.thread = threading.Thread(target=engine.worker, daemon=True)
    engine.thread.start()
    async def ticker():
        while True:
            await asyncio.to_thread(runtime.tick)
            await asyncio.sleep(.2)
    task = asyncio.create_task(ticker())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    runtime.stop(interrupted=True)
    engine.stop.set()
    engine.thread.join(timeout=5)
    if not engine.thread.is_alive():
        for item in engines.values():
            with item.lock:
                if item.detector:
                    item.detector.close()

app = FastAPI(title='포커스', lifespan=lifespan)


@app.middleware('http')
async def local_origin(request: Request, call_next):
    # Block browser cross-origin commands; LAN clients intentionally share one session.
    origin = request.headers.get('origin')
    if request.method == 'POST' and origin and origin != str(request.base_url).rstrip('/'):
        return Response('다른 사이트에서의 요청은 허용하지 않습니다.', status_code=403)
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Content-Security-Policy'] = "frame-ancestors 'none'"
    return response


@app.get('/')
def home():
    return FileResponse(ROOT/'app/static/index.html')


@app.get('/api/status')
def status():
    return combined_status()


async def body_json(request):
    data=bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data)>4096:raise HTTPException(413,'요청이 너무 큽니다.')
    try:
        value=json.loads(data or b'{}')
        if not isinstance(value,dict):raise ValueError()
        return value
    except (ValueError,TypeError):raise HTTPException(400,'JSON 요청을 확인하세요.')


def checked(action):
    try:return action()
    except ValueError as exc:raise HTTPException(400,str(exc))


@app.post('/api/participants')
async def participant(request:Request):
    from .runtime import participant_id
    data=await body_json(request)
    identifier=checked(lambda:participant_id(data.get('participant')))
    store.participant(identifier)
    return {'participant':identifier}


@app.get('/api/settings')
def settings():return dict(runtime.capture)


@app.post('/api/settings')
async def configure(request:Request):
    data=await body_json(request)
    with engines['pi'].lock,engines['phone'].lock:
        return checked(lambda:runtime.configure(data))


@app.get('/api/baseline')
def current_baseline(participant:str):
    return {'baseline':checked(lambda:runtime.baseline_for(participant))}


@app.post('/api/start')
async def start(request:Request):
    data=await body_json(request)
    if not all(item.detector for item in engines.values()):raise HTTPException(503,'자세 모델을 준비하지 못했습니다.')
    with engines['pi'].lock,engines['phone'].lock:
        return checked(lambda:runtime.prepare(data.get('participant'),data.get('delay',3),data.get('rules',{})))


@app.post('/api/cancel')
def cancel():return checked(runtime.cancel)


@app.post('/api/stop')
def stop():
    with engines['pi'].lock,engines['phone'].lock:
        saved=runtime.stop()
    return {'session':saved,'status':runtime.status()}


@app.post('/api/baseline')
async def baseline(request:Request):
    data=await body_json(request)
    return {'baseline':checked(lambda:runtime.save_baseline(data.get('confirmed')))}


@app.post('/api/source/{source}')
def source(source: str):
    raise HTTPException(410, '동시 분석 버전입니다. 페이지를 새로고침해 주세요.')


@app.post('/api/frame')
async def phone_frame(request: Request):
    chunks = bytearray()
    async for chunk in request.stream():
        chunks.extend(chunk)
        if len(chunks) > 1_000_000:
            raise HTTPException(413, '사진은 1MB 이하로 보내 주세요.')
    # Check encoded dimensions before decoder allocation.
    if request.headers.get('content-type','').split(';')[0] != 'image/jpeg':
        raise HTTPException(415, 'JPEG 형식만 지원합니다.')
    def decode():
        from PIL import Image
        try:
            with Image.open(io.BytesIO(chunks)) as picture:
                if picture.format != 'JPEG' or picture.width*picture.height > 4_000_000:
                    raise ValueError()
            frame = cv2.imdecode(np.frombuffer(chunks, dtype=np.uint8), cv2.IMREAD_COLOR)
            if frame is None:
                raise ValueError()
        except Exception:
            raise HTTPException(400, '사진을 읽을 수 없습니다. 4MP 이하 JPEG를 사용하세요.')
        global phone_owner, phone_owner_seen
        client = request.headers.get('x-focus-client', '')[:128]
        if not client:
            raise HTTPException(400, '휴대폰 연결 ID가 없습니다. 페이지를 새로고침하세요.')
        if not phone_guard.acquire(blocking=False):
            raise HTTPException(429, '이전 프레임 처리 중')
        try:
            now = time.monotonic()
            if phone_owner and phone_owner != client and now-phone_owner_seen < 6:
                raise HTTPException(409, '다른 휴대폰에서 카메라를 전송하고 있습니다.')
            phone_owner = client
            phone_owner_seen = now
            facing=request.headers.get('x-focus-facing','user')
            if facing not in ('user','environment'):raise HTTPException(400,'카메라 방향을 확인하세요.')
            engines['phone'].facing=facing
            engines['phone'].process(frame)
        finally:
            phone_guard.release()
        return engines['phone'].status()
    return await asyncio.to_thread(decode)


@app.get('/video/{source}')
async def video(source: str):
    if source not in engines:
        raise HTTPException(404, '카메라 없음')
    item = engines[source]
    async def frames():
        previous = None
        while True:
            csi = item.csi_preview
            if csi is not None:
                stamp, jpeg = csi.preview
                if time.monotonic() - stamp > 2:
                    jpeg = None
            else:
                stamp, jpeg = item.last_frame, item.jpeg
                if time.time() - stamp > 4:
                    jpeg = None
            if jpeg and stamp != previous:
                previous = stamp
                header = f'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: {len(jpeg)}\r\n\r\n'
                yield header.encode() + jpeg + b'\r\n'
            await asyncio.sleep(.025 if csi is not None else .15)
    return StreamingResponse(frames(), media_type='multipart/x-mixed-replace; boundary=frame',
                             headers={'X-Accel-Buffering':'no', 'Cache-Control':'no-store'})


def saved_session(sid):
    try:return store.get(sid)
    except KeyError:raise HTTPException(404,'저장된 세션을 찾지 못했습니다.')


@app.get('/api/sessions')
def history(participant:str):
    from .runtime import participant_id
    checked(lambda:participant_id(participant))
    return {'sessions':store.history(participant)}


@app.get('/api/sessions/{sid}')
def session(sid:str):return saved_session(sid)


@app.get('/api/sessions/{sid}/json')
def session_json(sid:str):
    return Response(json.dumps(saved_session(sid),ensure_ascii=False,allow_nan=False),media_type='application/json',
                    headers={'Content-Disposition':f'attachment; filename="focus-{sid}.json"'})


def session_csv(s):
    out=io.StringIO()
    columns=['record_type','session_id','participant','started_at','session_status','pi_side','pi_model','pi_lens',
             'phone_model','phone_lens','phone_facing','method','rom_definition','time_definition','elapsed_s',
             'ordinal','channel','t','start','end','knee_2d','hip_2d','trunk_2d','valid','reason','missing',
             'rom_2d','duration_s','score','baseline_id','baseline_version','key','value']
    writer=csv.DictWriter(out,fieldnames=columns);writer.writeheader()
    base={'session_id':s['id'],'participant':s['participant'],'started_at':s['started_at'],'session_status':s['status'],
          **{k:s['conditions'].get(k) for k in ('pi_side','pi_model','pi_lens','phone_model','phone_lens','phone_facing','method')},
          'rom_definition':'2D projected knee max-min from descent start to first returned standing sample',
          'time_definition':'same interval as ROM; excludes standing confirmation after return', 'elapsed_s':s['elapsed']}
    def row(record):
        merged={**base,**record}
        # Prevent participant IDs or future free text becoming spreadsheet formulas.
        writer.writerow({k:("'"+v if isinstance(v,str) and v.startswith(('=','+','-','@')) else v) for k,v in merged.items()})
    for key,value in s['summary'].items():row({'record_type':'summary','key':key,'value':value})
    for key,value in s['rules'].items():row({'record_type':'detection_rule','key':key,'value':value})
    for channel,value in s['quality'].items():
        for key,count in value.items():row({'record_type':'quality','channel':channel,'key':key,'value':count})
    for rep in s['repetitions']:
        row({'record_type':'repetition','ordinal':rep['ordinal'],'start':rep['start'],'end':rep['end'],
             'rom_2d':rep['rom'],'duration_s':rep['duration'],'score':rep['score'],
             'baseline_id':rep['baseline_id'],'baseline_version':rep['baseline_version']})
    for sample in s['samples']:
        row({'record_type':'sample','channel':sample['channel'],'t':sample['t'],'knee_2d':sample['knee'],
             'hip_2d':sample['hip'],'trunk_2d':sample['trunk'],'valid':sample['valid'],
             'reason':sample['reason'],'missing':sample.get('missing',False)})
    for event in s['events']:row({'record_type':'event','t':event['t'],'key':event['kind'],'value':json.dumps(event,ensure_ascii=False)})
    return Response('\ufeff'+out.getvalue(),media_type='text/csv; charset=utf-8',
                    headers={'Content-Disposition':f'attachment; filename="focus-{s["id"]}.csv"'})


@app.get('/api/sessions/{sid}/csv')
def download_csv(sid:str):return session_csv(saved_session(sid))


@app.get('/api/results.csv')
def results():
    if not runtime.last_saved:raise HTTPException(409,'먼저 측정을 중지·저장하세요.')
    return session_csv(saved_session(runtime.last_saved))


@app.get('/api/compare')
def compare(first:str,second:str):
    a,b=saved_session(first),saved_session(second)
    comparable=a['participant']==b['participant'] and a['condition_key']==b['condition_key']
    warnings=[]
    if not comparable:warnings.append('참가자 또는 촬영·측정 조건이 달라 직접 비교할 수 없습니다.')
    if a['baseline_versions']!=b['baseline_versions']:warnings.append('개인 기준 버전이 달라 재현도 점수의 직접 비교에 주의하세요.')
    warnings.append('ROM·시간의 증감을 좋음·나쁨 또는 피로로 해석하지 마세요.')
    return {'comparable':comparable,'warnings':warnings,
            'first':{'id':a['id'],'summary':a['summary']},'second':{'id':b['id'],'summary':b['summary']}}


@app.get('/setup/certificate')
def certificate():
    cert = ROOT/'config/focus-local-ca.crt'
    if not cert.exists():
        raise HTTPException(404, 'HTTPS 인증서가 아직 준비되지 않았습니다.')
    return FileResponse(cert, media_type='application/x-x509-ca-cert', filename='focus-local-ca.crt')
