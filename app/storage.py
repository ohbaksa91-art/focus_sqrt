"""Offline SQLite persistence. Videos and raw images are never written here."""
import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean


def utcnow():return datetime.now(timezone.utc).isoformat()
def dump(value):return json.dumps(value,ensure_ascii=False,allow_nan=False)
def condition_key(conditions):return hashlib.sha256(json.dumps(conditions,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def average(values):
    present=[x for x in values if x is not None]
    return mean(present) if present else None


class Store:
    def __init__(self,path):
        Path(path).parent.mkdir(parents=True,exist_ok=True)
        self.lock=threading.RLock()
        self.db=sqlite3.connect(str(path),check_same_thread=False)
        self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS participants(id TEXT PRIMARY KEY, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, participant TEXT NOT NULL REFERENCES participants(id),
          started_at TEXT NOT NULL, ended_at TEXT, status TEXT NOT NULL, elapsed REAL NOT NULL DEFAULT 0,
          condition_key TEXT NOT NULL, conditions TEXT NOT NULL, rules TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS baselines(id TEXT PRIMARY KEY, participant TEXT NOT NULL REFERENCES participants(id),
          condition_key TEXT NOT NULL, version INTEGER NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL,
          UNIQUE(participant,condition_key,version));
        CREATE TABLE IF NOT EXISTS repetitions(id TEXT PRIMARY KEY, session TEXT NOT NULL REFERENCES sessions(id),
          ordinal INTEGER NOT NULL, payload TEXT NOT NULL, baseline_id TEXT REFERENCES baselines(id));
        CREATE TABLE IF NOT EXISTS samples(id INTEGER PRIMARY KEY, session TEXT NOT NULL REFERENCES sessions(id),
          channel TEXT NOT NULL, t REAL NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, session TEXT NOT NULL REFERENCES sessions(id),
          t REAL NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS samples_session ON samples(session,t);
        CREATE INDEX IF NOT EXISTS sessions_person ON sessions(participant,started_at);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        PRAGMA user_version=1;
        ''')
        self.db.commit()

    def recover(self):
        with self.lock,self.db:
            rows=self.db.execute("SELECT id,elapsed FROM sessions WHERE status='measuring'").fetchall()
            for row in rows:
                latest=self.db.execute('SELECT MAX(t) FROM samples WHERE session=?',(row['id'],)).fetchone()[0]
                elapsed=max(row['elapsed'],latest or 0)
                self.db.execute("UPDATE sessions SET status='interrupted', ended_at=?, elapsed=? WHERE id=?",(utcnow(),elapsed,row['id']))
                self.db.execute('INSERT INTO events(session,t,kind,payload) VALUES(?,?,?,?)',(row['id'],elapsed,'interrupted',dump({'reason':'서버 재시작 또는 예기치 않은 종료'})))

    def participant(self,identifier):
        with self.lock,self.db:self.db.execute('INSERT OR IGNORE INTO participants VALUES(?,?)',(identifier,utcnow()))

    def settings(self,default):
        with self.lock:
            r=self.db.execute("SELECT value FROM settings WHERE key='capture'").fetchone()
            return {**default,**json.loads(r[0])} if r else dict(default)

    def set_settings(self,value):
        with self.lock,self.db:self.db.execute("INSERT OR REPLACE INTO settings VALUES('capture',?)",(dump(value),))

    def start(self,participant,conditions,rules):
        sid=str(uuid.uuid4());self.participant(participant)
        with self.lock,self.db:
            self.db.execute('INSERT INTO sessions(id,participant,started_at,status,condition_key,conditions,rules) VALUES(?,?,?,?,?,?,?)',
                            (sid,participant,utcnow(),'measuring',condition_key(conditions),dump(conditions),dump(rules)))
        return sid

    def sample(self,sid,channel,t,payload):
        with self.lock,self.db:
            self.db.execute('INSERT INTO samples(session,channel,t,payload) VALUES(?,?,?,?)',(sid,channel,t,dump(payload)))
            self.db.execute('UPDATE sessions SET elapsed=MAX(elapsed,?) WHERE id=?',(t,sid))

    def event(self,sid,t,kind,payload):
        with self.lock,self.db:self.db.execute('INSERT INTO events(session,t,kind,payload) VALUES(?,?,?,?)',(sid,t,kind,dump(payload)))

    def repetition(self,sid,payload):
        with self.lock,self.db:
            ordinal=self.db.execute('SELECT COUNT(*)+1 FROM repetitions WHERE session=?',(sid,)).fetchone()[0]
            payload={**payload,'id':str(uuid.uuid4()),'ordinal':ordinal}
            self.db.execute('INSERT INTO repetitions VALUES(?,?,?,?,?)',(payload['id'],sid,ordinal,dump(payload),payload['baseline_id']))
            return payload

    def finish(self,sid,elapsed,status='completed'):
        with self.lock,self.db:self.db.execute('UPDATE sessions SET status=?,ended_at=?,elapsed=? WHERE id=?',(status,utcnow(),elapsed,sid))

    def latest_baseline(self,participant,conditions):
        with self.lock:
            row=self.db.execute('SELECT payload FROM baselines WHERE participant=? AND condition_key=? ORDER BY version DESC LIMIT 1',(participant,condition_key(conditions))).fetchone()
            return json.loads(row[0]) if row else None

    def baseline(self,participant,conditions,values,sid,t,rep_ids):
        with self.lock,self.db:
            version=self.db.execute('SELECT COALESCE(MAX(version),0)+1 FROM baselines WHERE participant=? AND condition_key=?',(participant,condition_key(conditions))).fetchone()[0]
            value={**values,'id':str(uuid.uuid4()),'version':version,'created_at':utcnow(),'source_session':sid,'source_repetitions':rep_ids,'conditions':conditions}
            self.db.execute('INSERT INTO baselines VALUES(?,?,?,?,?,?)',(value['id'],participant,condition_key(conditions),version,value['created_at'],dump(value)))
            self.db.execute('INSERT INTO events(session,t,kind,payload) VALUES(?,?,?,?)',(sid,t,'baseline_saved',dump({'baseline_id':value['id'],'version':version})))
            return value

    def get(self,sid):
        with self.lock:
            row=self.db.execute('SELECT * FROM sessions WHERE id=?',(sid,)).fetchone()
            if not row:raise KeyError(sid)
            result=dict(row)
            result['conditions']=json.loads(result['conditions']);result['rules']=json.loads(result['rules'])
            result['repetitions']=[json.loads(x[0]) for x in self.db.execute('SELECT payload FROM repetitions WHERE session=? ORDER BY ordinal',(sid,))]
            result['samples']=[{'channel':x['channel'],'t':x['t'],**json.loads(x['payload'])} for x in self.db.execute('SELECT channel,t,payload FROM samples WHERE session=? ORDER BY t,id',(sid,))]
            result['events']=[{'t':x['t'],'kind':x['kind'],**json.loads(x['payload'])} for x in self.db.execute('SELECT t,kind,payload FROM events WHERE session=? ORDER BY t,id',(sid,))]
            reps=result['repetitions'];scores=[x['score'] for x in reps if x['score'] is not None]
            result['summary']={'count':len(reps),'mean_rom':average([x['rom'] for x in reps]),'mean_duration':average([x['duration'] for x in reps]),
                               'mean_score':average(scores),'scored_repetitions':len(scores)}
            result['quality']={channel:{'held_frames':sum(1 for x in result['samples'] if x['channel']==channel and not x['valid'] and not x.get('missing')),
                                       'missing_markers':sum(1 for x in result['samples'] if x['channel']==channel and x.get('missing'))}
                               for channel in ('pi','phone')}
            result['baseline_versions']=sorted({x['baseline_id'] for x in reps if x['baseline_id']})
            referenced=set(result['baseline_versions']) | {x['baseline_id'] for x in result['events'] if x.get('baseline_id')}
            result['baselines']=[json.loads(row[0]) for identifier in sorted(referenced)
                                 for row in self.db.execute('SELECT payload FROM baselines WHERE id=?',(identifier,))]
            return result

    def history(self,participant):
        with self.lock:
            rows=self.db.execute('SELECT id FROM sessions WHERE participant=? ORDER BY started_at DESC LIMIT 200',(participant,)).fetchall()
            result=[]
            for row in rows:
                s=self.get(row['id'])
                result.append({k:s[k] for k in ('id','participant','started_at','status','elapsed','condition_key','conditions','summary','baseline_versions')})
            return result

    def close(self):self.db.close()
