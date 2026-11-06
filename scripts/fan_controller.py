#!/usr/bin/env python3
"""Restricted local fan controller. Web app supplies only an allowlisted mode."""
import fcntl
import json
import os
from pathlib import Path
import pwd
import signal
import time

RUN = Path('/run/focus-fan')
MODES = {'auto': 0, 'low': 1, 'medium': 2, 'high': 4}


def thermal_floor(temp):
    for threshold, state in ((70,4),(65,3),(60,2),(55,1)):
        if temp >= threshold:
            return state
    return 0


def desired_state(mode, temp):
    return max(MODES[mode], thermal_floor(temp))


def main():
    cooling = next(p for p in Path('/sys/class/thermal').glob('cooling_device*') if (p/'type').read_text().strip() == 'pwm-fan')
    hwmon = next(p for p in Path('/sys/class/hwmon').glob('hwmon*') if (p/'name').read_text().strip() == 'pwmfan')
    temp_path = next(p/'temp' for p in Path('/sys/class/thermal').glob('thermal_zone*') if (p/'type').read_text().strip() == 'cpu-thermal')
    cur = cooling/'cur_state'
    maximum = int((cooling/'max_state').read_text())
    RUN.mkdir(exist_ok=True)
    request = RUN/'request.json'
    if not request.exists():
        request.write_text('{}')
    user = pwd.getpwnam(os.environ.get('FOCUS_APP_USER','focus'))
    os.chown(request,user.pw_uid,user.pw_gid)
    request.chmod(0o600)
    mode = 'auto'; token = None; expires = 0; previous = 'auto'
    stopping = False
    def stop(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while not stopping:
            try:
                with request.open() as f:
                    fcntl.flock(f,fcntl.LOCK_SH)
                    command=json.load(f)
                if command.get('id') != token and command.get('mode') in MODES:
                    token=command['id'];mode=command['mode'];expires=time.monotonic()+600
            except (ValueError,OSError,KeyError):
                pass
            if mode != 'auto' and time.monotonic() >= expires:
                mode='auto'
            try:
                temp=float(temp_path.read_text())/1000
                floor=thermal_floor(temp)
                if mode != 'auto':
                    target=min(maximum,desired_state(mode,temp))
                    cur.write_text(str(target))
                elif previous != 'auto':
                    cur.write_text(str(min(maximum,floor)))
                status={'available':True,'mode':mode,'temperature':round(temp,1),
                        'rpm':int((hwmon/'fan1_input').read_text()),
                        'state':int(cur.read_text()),'max_state':maximum,
                        'thermal_override':mode!='auto' and floor>MODES[mode],
                        'remaining':max(0,round(expires-time.monotonic())) if mode!='auto' else 0,
                        'updated_at':time.time(),'request_id':token}
            except (OSError,ValueError) as exc:
                # Fail toward more cooling while retaining kernel thermal control.
                try:cur.write_text(str(maximum))
                except OSError:pass
                status={'available':False,'error':'팬 또는 온도 센서를 읽지 못했습니다.','updated_at':time.time()}
            temp_file=RUN/'status.tmp';temp_file.write_text(json.dumps(status));temp_file.chmod(0o644);temp_file.replace(RUN/'status.json')
            previous=mode
            time.sleep(.5)
    finally:
        # Return control to kernel governor with a temperature-appropriate starting state.
        try:cur.write_text(str(min(maximum,thermal_floor(float(temp_path.read_text())/1000))))
        except (OSError,ValueError):pass

if __name__=='__main__':
    main()
