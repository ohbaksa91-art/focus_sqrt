#!/usr/bin/env bash
set -euo pipefail
# Optional direct TLS. Trust this certificate on the phone before getUserMedia use.
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
IP="${1:-}"
[[ "$IP" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || { echo '사용법: bash scripts/https.sh Pi의IPv4주소'; exit 1; }
sudo install -d -m 750 -o root -g "$(id -gn)" /etc/focus-tls
if [[ -e /etc/focus-tls/cert.pem ]]; then echo '기존 인증서가 있습니다. 덮어쓰지 않았습니다.'; exit 1; fi
sudo openssl req -x509 -newkey rsa:2048 -nodes -days 365 -keyout /etc/focus-tls/key.pem -out /etc/focus-tls/cert.pem -subj '/CN=focus-pi.local' -addext "subjectAltName=DNS:focus-pi.local,IP:$IP" -addext 'basicConstraints=critical,CA:TRUE'
sudo chown root:"$(id -gn)" /etc/focus-tls/*.pem
sudo chmod 640 /etc/focus-tls/key.pem
sudo mkdir -p /etc/systemd/system/focus.service.d
sudo tee /etc/systemd/system/focus.service.d/https.conf >/dev/null <<UNIT
[Service]
ExecStart=
ExecStart="$ROOT/.venv/bin/python" -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 --ssl-keyfile /etc/focus-tls/key.pem --ssl-certfile /etc/focus-tls/cert.pem
UNIT
sudo systemctl daemon-reload
sudo systemctl restart focus
echo "인증서를 스마트폰에 설치·신뢰한 뒤 https://$IP:8000 에 접속하세요."
