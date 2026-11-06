#!/usr/bin/env bash
set -euo pipefail
if [[ $# -lt 2 || $# -gt 3 ]]; then echo '사용법: bash scripts/deploy.sh 사용자@Pi주소 SSH키파일 [known_hosts파일]'; exit 1; fi
TARGET="$1"
[[ "$TARGET" =~ ^[a-zA-Z_][a-zA-Z0-9_-]*@[a-zA-Z0-9._:-]+$ ]] || { echo '접속 주소 형식을 확인하세요.'; exit 1; }
KEYFILE="$(cd "$(dirname "$2")" && pwd)/$(basename "$2")"
OPTIONS=(-i "$KEYFILE" -o IdentitiesOnly=yes)
if [[ $# == 3 ]]; then
 HOSTFILE="$(cd "$(dirname "$3")" && pwd)/$(basename "$3")"
 OPTIONS+=(-o "UserKnownHostsFile=$HOSTFILE")
fi
cd "$(dirname "$0")/.."
ssh "${OPTIONS[@]}" "$TARGET" 'set -eu; mkdir -p "$HOME/focus" "$HOME/focus-backup"; if [ -d "$HOME/focus/app" ]; then tar --exclude=__pycache__ --exclude="._*" -czf "$HOME/focus-backup/app-$(date +%Y%m%d-%H%M%S).tgz" -C "$HOME/focus" app; fi'
COPYFILE_DISABLE=1 tar --no-xattrs --exclude='__pycache__' --exclude='*.pyc' --exclude='._*' -czf - app scripts tests requirements.txt requirements-dev.txt README.md | ssh "${OPTIONS[@]}" "$TARGET" 'tar -xzf - -C "$HOME/focus"'
ssh "${OPTIONS[@]}" "$TARGET" 'set -eu; cd "$HOME/focus"; mkdir -p data; if [ -x .venv/bin/python ] && systemctl cat focus >/dev/null 2>&1; then .venv/bin/python -m compileall -q -x "/\\._" app; sudo systemctl restart focus; else bash scripts/install.sh; fi'
