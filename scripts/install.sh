#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
if [[ "$(uname -s)" != Linux || "$(uname -m)" != aarch64 ]]; then
  echo 'Raspberry Pi OS 64-bit(aarch64)에서 실행하세요.' >&2; exit 1
fi
if [[ "$EUID" == 0 ]]; then echo 'sudo 없이 Pi 사용자 계정으로 실행하세요.' >&2; exit 1; fi
sudo apt-get update
sudo apt-get install -y python3-venv python3-pip libgl1 libglib2.0-0 libportaudio2 rpicam-apps curl openssl
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install --only-binary=:all: -r requirements.txt
mkdir -p models
if [[ ! -s models/pose_landmarker_lite.task ]]; then
 curl --fail --location --retry 3 'https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/1/pose_landmarker_lite.task' -o models/model.download
 mv models/model.download models/pose_landmarker_lite.task
fi
.venv/bin/python - <<'PY'
import mediapipe as mp
with mp.tasks.vision.PoseLandmarker.create_from_options(mp.tasks.vision.PoseLandmarkerOptions(base_options=mp.tasks.BaseOptions(model_asset_path='models/pose_landmarker_lite.task'))) as model:
    print('Pose model initialized')
PY
sudo usermod -aG video "$(id -un)"
if [[ ! -e /etc/focus.env ]]; then
 printf '%s\n' 'FOCUS_CAMERA=0' 'FOCUS_CAMERA_BACKEND=csi' | sudo tee /etc/focus.env >/dev/null
fi
# systemd quotes paths to support directory names containing spaces.
sudo tee /etc/systemd/system/focus.service >/dev/null <<UNIT
[Unit]
Description=Focus posture camera web application
After=network-online.target
Wants=network-online.target
[Service]
User=$(id -un)
Group=$(id -gn)
SupplementaryGroups=video
WorkingDirectory=$ROOT
EnvironmentFile=/etc/focus.env
ExecStart="$ROOT/.venv/bin/python" -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 --timeout-graceful-shutdown 5
Restart=on-failure
RestartSec=5
TimeoutStopSec=15
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable focus
sudo systemctl restart focus
sleep 3
curl --fail http://127.0.0.1:8000/api/status
printf '\n접속: http://focus-pi.local:8000 (또는 Pi IP:8000)\n'
