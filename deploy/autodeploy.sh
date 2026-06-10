#!/usr/bin/env bash
# TRMT auto-deploy: GitHub main 에 새 커밋이 있으면 자동 반영.
# systemd timer 가 주기적으로 호출함. 변경 없으면 즉시 종료.
set -euo pipefail
APP_DIR="${TRMT_APP_DIR:-$HOME/app}"
BRANCH="main"
cd "$APP_DIR"

git fetch --quiet origin "$BRANCH"
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse "origin/$BRANCH")
[ "$LOCAL" = "$REMOTE" ] && exit 0   # 변경 없음 → 종료

echo "$(date '+%F %T') new commit ${REMOTE:0:7}, deploying..."
git reset --hard "origin/$BRANCH"    # tracked 파일만 갱신, instance/ static/uploads(untracked) 보존

# venv 의존성 갱신(있으면) + DB 마이그레이션(drop 안 함)
source venv/bin/activate
pip install -q -r requirements.txt || true
python3 -c "import app; app.init_db(drop=False)"

sudo systemctl restart trmt
echo "$(date '+%F %T') deploy done -> ${REMOTE:0:7}"
