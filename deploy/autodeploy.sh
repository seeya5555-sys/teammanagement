#!/usr/bin/env bash
# TRMT auto-deploy (git 불필요). GitHub main 에 새 커밋이 있으면 curl/unzip 으로 반영.
# systemd timer 가 주기적으로 호출. 변경 없으면 즉시 종료(메모리 거의 안 씀).
set -euo pipefail

# ⚠️ 자기 자신을 덮어쓰는 문제 회피 (self-relocate).
# 이 스크립트는 $APP_DIR/deploy/ 안에 있는데 아래 27행 cp -rf 가 $APP_DIR 를 통째로 덮는다.
# bash 는 스크립트를 통째로 읽지 않고 파일 오프셋을 들고 이어 읽으므로, 실행 중에 자기 파일이
# 바뀌면 남은 부분을 엉뚱한 위치부터 읽어 구문오류나 라인 절단이 난다(길이가 바뀌면 확정적).
# 그래서 진짜 작업 전에 자기 사본을 /tmp 로 옮겨 거기서 다시 실행한다.
if [ -z "${TRMT_RELOCATED:-}" ]; then
  _self="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
  _run="$(mktemp /tmp/trmt_autodeploy.XXXXXX.sh)"
  cp "$_self" "$_run"
  chmod +x "$_run"
  # 사본은 exec 로 대체 실행하고, 사본 자신이 끝날 때 스스로를 지운다.
  TRMT_RELOCATED=1 TRMT_RELOCATED_PATH="$_run" exec /bin/bash "$_run" "$@"
fi
[ -n "${TRMT_RELOCATED_PATH:-}" ] && trap 'rm -f "$TRMT_RELOCATED_PATH"' EXIT

APP_DIR="${TRMT_APP_DIR:-$HOME/app}"
REL_DIR="${TRMT_RELEASES_DIR:-$HOME/releases}"
REPO="seeya5555-sys/teammanagement"
BRANCH="main"
SHA_FILE="$APP_DIR/.deployed_sha"
HOLD_FILE="$APP_DIR/.deploy_hold"
KEEP_RELEASES="${TRMT_KEEP_RELEASES:-2}"   # 현재 + 직전
LOCK_FILE="${TRMT_LOCK_FILE:-$APP_DIR/.deploy.lock}"
mkdir -p "$REL_DIR"
cd "$APP_DIR"

# 🔒 rollback.sh 와 상호배제. 60초 타이머가 롤백 도중에 깨어나 같은 디렉토리를 덮으면
# 반쯤 섞인 배포본이 된다. 락을 못 잡으면 이번 회차는 조용히 넘긴다(다음 타이머가 재시도).
exec 9>"$LOCK_FILE"
flock -n 9 || { echo "$(date '+%F %T') 다른 배포/롤백 진행 중 — skip"; exit 0; }

# 원격 main 최신 커밋 SHA (git ls-remote 와 동일, REST API 아님 → rate limit 무관)
# `|| true` 필수: set -euo pipefail 하에서 네트워크 실패나 grep 미매치가 나면 아래 -z 방어까지
# 가지도 못하고 스크립트가 통째로 죽는다(= 조용한 배포 중단).
REMOTE=$(curl -s "https://github.com/${REPO}.git/info/refs?service=git-upload-pack" \
  | tr -d '\0' | grep -oE "[0-9a-f]{40} refs/heads/${BRANCH}" | head -1 | cut -d' ' -f1 || true)
[ -z "$REMOTE" ] && { echo "$(date '+%F %T') cannot fetch remote sha"; exit 0; }

LOCAL=$(cat "$SHA_FILE" 2>/dev/null || echo none)

# 롤백으로 차단된 SHA 는 다시 배포하지 않는다(deploy/rollback.sh 가 기록).
# 이게 없으면 롤백 60초 뒤 타이머가 방금 되돌린 나쁜 커밋을 그대로 재배포한다.
# main 에 새 커밋이 올라오면 REMOTE 가 달라지므로 hold 는 자동으로 풀린다.
if [ -s "$HOLD_FILE" ] && [ "$REMOTE" = "$(cat "$HOLD_FILE")" ]; then
  echo "$(date '+%F %T') hold: ${REMOTE:0:7} 은 롤백으로 차단됨 — 배포 건너뜀"
  exit 0
fi
[ -s "$HOLD_FILE" ] && { echo "$(date '+%F %T') 새 커밋 감지 — hold 해제"; rm -f "$HOLD_FILE"; }

# 현재 배포본의 zip 이 보관돼 있지 않으면 먼저 확보한다(롤백 대상이 없으면 롤백을 못 한다).
# 최초 도입/수동 배포 직후에만 걸리고, 실패해도 배포 자체는 계속 진행한다.
if [ "$LOCAL" != "none" ] && [ ! -f "$REL_DIR/${LOCAL}.zip" ]; then
  echo "$(date '+%F %T') 현재 릴리스 ${LOCAL:0:7} 보관본 없음 — 확보 시도"
  curl -fsSL "https://github.com/${REPO}/archive/${LOCAL}.zip" -o "$REL_DIR/${LOCAL}.zip.part" \
    && mv "$REL_DIR/${LOCAL}.zip.part" "$REL_DIR/${LOCAL}.zip" \
    || { rm -f "$REL_DIR/${LOCAL}.zip.part"; echo "$(date '+%F %T') WARN: 보관 실패 — 이 배포는 롤백 대상이 없다"; }
fi

[ "$LOCAL" = "$REMOTE" ] && exit 0   # 변경 없음 → 종료

echo "$(date '+%F %T') new commit ${REMOTE:0:7}, deploying..."
# ⚠️ 반드시 커밋 SHA 고정 URL. archive/refs/heads/main.zip 은 codeload 캐시 alias 라서
#    직전 커밋의 zip 이 돌아올 수 있음. 그러면 내용은 구버전인데 .deployed_sha 는 최신으로
#    기록되고, 17행 조기종료 때문에 그 상태가 영구 고정됨(2026-07-29 build 101 OTA 사고:
#    web SHA 는 380ccf1 인데 IPA·manifest 는 build 100 이 계속 서빙됨).
#    SHA 고정 archive 는 immutable 이라 캐시 히트도 항상 정확함.
# 받은 zip 은 롤백용으로 보관한다(.part → 배포 성공 시 승격). 이전 실행이 남긴 찌꺼기도 정리.
rm -f "$REL_DIR"/*.zip.part
NEW_ZIP="$REL_DIR/${REMOTE}.zip"
curl -fsSL "https://github.com/${REPO}/archive/${REMOTE}.zip" -o "${NEW_ZIP}.part"
cd /tmp && rm -rf "teammanagement-${REMOTE}" && unzip -oq "${NEW_ZIP}.part"
cp -rf "teammanagement-${REMOTE}/." "$APP_DIR/"     # 코드 갱신, instance/ 와 기존 uploads 는 보존
rm -rf "teammanagement-${REMOTE}"
cd "$APP_DIR"

source venv/bin/activate
# gunicorn 없으면 설치(venv 재구축/신규 서버 대비). 있으면 즉시 skip.
python3 -c "import gunicorn" 2>/dev/null || pip install "gunicorn>=21,<24"
# Outlook .msg 미리보기 파서: 필요한 새 의존성만 배포 시 설치(기존 배포는 즉시 skip).
python3 -c "import extract_msg" 2>/dev/null || pip install "extract-msg>=0.55,<0.56"
# Werkzeug 3.1+ 보장: per-request request.max_content_length setter 필요
# (STT 200MB 허용하되 그 외 라우트를 스트리밍 단계서 20MB로 조임 — 3.0은 setter 없어 fail-open).
# 3.1+ 이면 즉시 skip, 미만일 때만 업그레이드(배포 시에만·미달 시에만 실행).
python3 -c "import sys; from importlib.metadata import version; v=tuple(int(x) for x in version('werkzeug').split('.')[:2]); sys.exit(0 if v>=(3,1) else 1)" 2>/dev/null \
  || pip install -U "Werkzeug>=3.1,<4.0"
# 마이그레이션(gunicorn은 __main__을 안 타므로 init_db 자체migrate + _auto_migrate 둘 다).
python3 -c "import app; app.init_db(drop=False); app._auto_migrate()"
sudo systemctl restart trmt
echo "$REMOTE" > "$SHA_FILE"

# 배포 성공 확정 후에만 릴리스로 승격하고, 오래된 보관본을 정리한다(현재 + 직전 = 2개).
mv -f "${NEW_ZIP}.part" "$NEW_ZIP"
ls -1t "$REL_DIR"/*.zip 2>/dev/null | tail -n +$((KEEP_RELEASES + 1)) | xargs -r rm -f

echo "$(date '+%F %T') deploy done -> ${REMOTE:0:7} (롤백: deploy/rollback.sh)"
