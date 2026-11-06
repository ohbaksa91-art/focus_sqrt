"""Research-only 2D projected squat measurements. No clinical posture classifier."""
import math
from copy import deepcopy
from statistics import median

METHOD = 'pixel-2d-squat-v1'
DEFAULT_RULES = {'standing':160., 'descent':150., 'depth':110., 'min_rom':30.,
                 'min_duration':1., 'max_duration':20., 'max_gap':1.25, 'stable':.25}


def validate_rules(rules):
    values = {**DEFAULT_RULES, **rules}
    if set(values) != set(DEFAULT_RULES) or not all(isinstance(x,(int,float)) and math.isfinite(x) for x in values.values()):
        raise ValueError('반복 검출 설정을 확인하세요.')
    if not (30 <= values['depth'] < values['descent'] < values['standing'] <= 178):
        raise ValueError('깊이 < 하강 < 서기 임계값이어야 합니다.')
    if not (5 <= values['min_rom'] <= 120 and .5 <= values['min_duration'] <= 5 and
            5 <= values['max_duration'] <= 60 and .5 <= values['max_gap'] <= 2 and
            .15 <= values['stable'] <= .6):
        raise ValueError('반복 검출 설정 범위를 확인하세요.')
    return values


def reproduction_score(rom, duration, baseline):
    if not baseline or not all(isinstance(v,(int,float)) and math.isfinite(v) and v>0
                               for v in (rom,duration,baseline['rom'],baseline['duration'])):
        return None
    deviation=.7*abs(rom-baseline['rom'])/baseline['rom']+.3*abs(duration-baseline['duration'])/baseline['duration']
    return round(max(0.,100*(1-deviation)),2)


class SquatDetector:
    def __init__(self, rules=None):
        self.rules=validate_rules(rules or {})
        self.reset()

    def reset(self):
        self.state='waiting_standing'
        self.standing_since=None
        self.return_since=None
        self.start=None
        self.last_t=None
        self.minimum=None
        self.maximum=None
        self.baseline=None
        self.reason='선 자세를 먼저 확인해 주세요.'

    def invalidate(self, reason='인식 보류 후 선 자세부터 다시 준비해 주세요.'):
        self.reset()
        self.reason=reason

    def update(self,t,knee,valid,baseline=None):
        r=self.rules
        if not valid or knee is None or not math.isfinite(knee):
            self.invalidate()
            return None
        if self.last_t is not None and (t-self.last_t>r['max_gap'] or t<=self.last_t):
            self.invalidate('데이터 공백이 있어 선 자세부터 다시 준비합니다.')
        self.last_t=t
        if self.state=='waiting_standing':
            if knee>=r['standing']:
                if self.standing_since is None:self.standing_since=t
                if t-self.standing_since>=r['stable']:
                    self.state='standing';self.reason='준비 완료 · 천천히 하강하세요.'
            else:self.standing_since=None
            return None
        if self.state=='standing':
            if knee<r['descent']:
                self.state='descending';self.start=t;self.minimum=knee;self.maximum=knee
                self.baseline=deepcopy(baseline);self.reason='하강 중'
            return None
        if t-self.start>r['max_duration']:
            self.invalidate('반복시간 제한을 넘어 선 자세부터 다시 준비합니다.')
            return None
        # ROM and duration use the SAME interval: first descent sample to first
        # returning standing sample. Confirmation frames after that are excluded.
        if self.return_since is None:
            self.minimum=min(self.minimum,knee);self.maximum=max(self.maximum,knee)
        if self.state=='descending' and knee<=r['depth']:
            self.state='depth_reached';self.reason='설정 깊이 도달 · 선 자세로 돌아오세요.'
        if knee>=r['standing']:
            if self.return_since is None:
                self.return_since=t
                self.frozen_rom=self.maximum-self.minimum
                self.frozen_duration=t-self.start
            if t-self.return_since>=r['stable']:
                complete=self.state=='depth_reached' and self.frozen_rom>=r['min_rom'] and self.frozen_duration>=r['min_duration']
                result=None
                if complete:
                    result={'start':self.start,'end':self.return_since,'rom':round(self.frozen_rom,3),
                            'duration':round(self.frozen_duration,3),'min_knee':round(self.minimum,3),
                            'max_knee':round(self.maximum,3),
                            'baseline_id':self.baseline['id'] if self.baseline else None,
                            'baseline_version':self.baseline['version'] if self.baseline else None,
                            'score':reproduction_score(self.frozen_rom,self.frozen_duration,self.baseline)}
                self.state='standing';self.start=None;self.return_since=None
                self.reason='완료 · 다음 반복 준비' if complete else '부분·짧은 반복 제외 · 다시 준비하세요.'
                return result
        elif self.return_since is not None:
            # A brief bounce across the standing threshold is not completion.
            self.return_since=None
            self.minimum=min(self.minimum,knee);self.maximum=max(self.maximum,knee)
        return None


def baseline_values(repetitions):
    if len(repetitions)<3:raise ValueError('완료된 반복 3회가 필요합니다.')
    last=repetitions[-3:]
    return {'rom':median(x['rom'] for x in last),'duration':median(x['duration'] for x in last)}
