#!/usr/bin/env bash
# 편한 견적서 실행 스크립트
# 사용법: bash run.sh   →   http://localhost:5010 (충돌 시 자동 전환)
set -euo pipefail

cd "$(dirname "$0")"

if ! python3 -c "import flask" >/dev/null 2>&1; then
  echo "Flask를 설치합니다..."
  pip3 install --quiet flask
fi

# app.py가 시작할 때 DB 생성 + 기본 단가표를 자동으로 넣습니다.
exec python3 app.py
