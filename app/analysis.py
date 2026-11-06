import math


def angle(a,b,c):
    u=[a[i]-b[i] for i in range(len(a))];v=[c[i]-b[i] for i in range(len(a))]
    denom=math.sqrt(sum(x*x for x in u)*sum(x*x for x in v))
    if denom<1e-9:return None
    return round(math.degrees(math.acos(max(-1,min(1,sum(x*y for x,y in zip(u,v))/denom)))),2)


def visible(p):
    return (p.visibility>=.55 and (getattr(p,'presence',None) is None or p.presence>=.55)
            and math.isfinite(p.x) and math.isfinite(p.y) and 0<=p.x<=1 and 0<=p.y<=1)


def projected_metrics(points,side='left',width=640,height=480):
    ids=(11,23,25,27) if side=='left' else (12,24,26,28)
    if not all(visible(points[i]) for i in ids):
        return {'valid':False,'knee':None,'hip':None,'trunk':None,'reason':'occluded'}
    shoulder,hip,knee,ankle=[(points[i].x*width,points[i].y*height) for i in ids]
    knee_angle=angle(hip,knee,ankle);hip_angle=angle(shoulder,hip,knee)
    dx,dy=shoulder[0]-hip[0],shoulder[1]-hip[1]
    trunk=round(math.degrees(math.atan2(abs(dx),-dy)),2) if math.hypot(dx,dy)>1e-6 else None
    valid=all(x is not None and math.isfinite(x) for x in (knee_angle,hip_angle,trunk))
    return {'valid':valid,'knee':knee_angle if valid else None,'hip':hip_angle if valid else None,
            'trunk':trunk if valid else None,'reason':'ready' if valid else 'degenerate'}


def metrics(points):
    result={}
    for side,label in [('left','왼쪽'),('right','오른쪽')]:
        data=projected_metrics(points,side)
        if data['valid']:
            result[label+' 무릎']=data['knee'];result[label+' 고관절']=data['hip'];result[label+' 몸통 기울기']=data['trunk']
    return result


def framing_guide(points,view='front'):
    if view in ('left','right'):
        chain=(11,23,25,27) if view=='left' else (12,24,26,28)
        head=max((points[i] for i in (0,7,8)),key=lambda p:p.visibility)
        needed=[head]+[points[i] for i in chain]
        valid=all(visible(p) for p in needed)
    else:
        needed=[points[i] for i in (0,11,12,23,24,25,26,27,28)]
        valid=sum(visible(p) for p in needed)>=7 and visible(points[27]) and visible(points[28])
    # Inferred out-of-frame coordinates take precedence over occlusion hints.
    if any(p.visibility>.3 and (p.x<.025 or p.x>.975 or p.y<.025 or p.y>.975) for p in needed):
        return {'ready':False,'reason':'edge','message':'머리·발 등이 잘려요. 카메라에서 조금 멀어져 전신이 보이게 하세요.'}
    if not valid:
        return {'ready':False,'reason':'occluded','message':'필요한 관절이 가려져요. 선택한 촬영 방향과 팔·다리·가구의 가림을 확인하세요.'}
    shown=[p for p in needed if visible(p)]
    center=sum(p.x for p in shown[1:4])/len(shown[1:4])
    if abs(center-.5)>.16:
        return {'ready':False,'reason':'off_center','message':'화면 중앙 가이드 쪽으로 이동하세요.'}
    if max(p.y for p in shown)-min(p.y for p in shown)<.45:
        return {'ready':False,'reason':'too_small','message':'사람이 작게 보여요. 카메라에 조금 가까이 오세요.'}
    return {'ready':True,'reason':'ready','message':'촬영 구도가 확인됐어요. 선 자세로 시작을 준비하세요.'}
