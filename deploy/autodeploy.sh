#!/usr/bin/env bash
# TRMT auto-deploy (git 불필요). GitHub main 에 새 커밋이 있으면 curl/unzip 으로 반영.
# systemd timer 가 주기적으로 호출. 변경 없으면 즉시 종료(메모리 거의 안 씀).
set -euo pipefail
APP_DIR="${TRMT_APP_DIR:-$HOME/app}"
REPO="seeya5555-sys/teammanagement"
BRANCH="main"
SHA_FILE="$APP_DIR/.deployed_sha"
cd "$APP_DIR"

# 원격 main 최신 커밋 SHA (git ls-remote 와 동일, REST API 아님 → rate limit 무관)
REMOTE=$(curl -s "https://github.com/${REPO}.git/info/refs?service=git-upload-pack" \
  | tr -d '\0' | grep -oE "[0-9a-f]{40} refs/heads/${BRANCH}" | head -1 | cut -d' ' -f1)
[ -z "$REMOTE" ] && { echo "$(date '+%F %T') cannot fetch remote sha"; exit 0; }

LOCAL=$(cat "$SHA_FILE" 2>/dev/null || echo none)
[ "$LOCAL" = "$REMOTE" ] && exit 0   # 변경 없음 → 종료

echo "$(date '+%F %T') new commit ${REMOTE:0:7}, deploying..."
curl -sL "https://github.com/${REPO}/archive/refs/heads/${BRANCH}.zip" -o /tmp/trmt_u.zip
cd /tmp && rm -rf "teammanagement-${BRANCH}" && unzip -oq trmt_u.zip
cp -rf "teammanagement-${BRANCH}/." "$APP_DIR/"     # 코드 갱신, instance/ 와 기존 uploads 는 보존
rm -rf "teammanagement-${BRANCH}" trmt_u.zip
cd "$APP_DIR"

source venv/bin/activate
python3 -c "import app; app.init_db(drop=False)"
sudo systemctl restart trmt
echo "$REMOTE" > "$SHA_FILE"
echo "$(date '+%F %T') deploy done -> ${REMOTE:0:7}"
