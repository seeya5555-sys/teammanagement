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

# ── 삭제 전파 ────────────────────────────────────────────────────────────────
# cp -rf 는 "repo 에서 지워진 파일"을 서버에서 지우지 않는다. 그래서 파일을 분할하거나
# 이름을 바꾸면 옛 .py 가 서버에 영구 잔존하고, repo 와 서버가 조용히 갈라진다.
# (모듈 추출 리팩터링에서 특히 위험: 옛 모듈이 import 가능한 채로 남아 어느 쪽이 도는지 모호해짐)
#
# 🔴 기준은 반드시 "직전 릴리스 zip ∖ 새 릴리스 zip" 이다. "서버 실파일 ∖ zip" 으로 잡으면
#    zip 에 애초에 없는 운영 자산 — wsgi.py·drydock_integration.py(서버 수동 배치),
#    static/uploads/*(사용자 업로드), instance/(DB) — 이 전부 삭제 대상이 되어 서비스가 죽는다.
#    zip 두 개의 차집합만 보면 그런 파일은 애초에 후보에 오르지 않는다.
PREV_ZIP="$REL_DIR/${LOCAL}.zip"
PEND_FILE="$APP_DIR/.deploy_deletions_pending"   # 이번에 못 지운 것 — 다음 배포에서 재시도
APP_REAL=$(cd "$APP_DIR" && pwd -P)

_sha_ok() { printf '%s' "${1:-}" | grep -qE '^[0-9a-f]{40}$'; }

# zip 의 파일 목록을 root prefix 를 벗겨 $3 에 쓴다.
# 🔴 하나라도 예상 root(teammanagement-<sha>/) 밖에 있으면 실패시킨다. prefix 가 안 벗겨지면
#    prev.list 는 "teammanagement-X/app.py", new.list 는 "app.py" 가 되어 차집합이 통째로
#    부풀고, 살아있는 파일까지 삭제 후보가 된다(GitHub 이 archive 구조를 바꾸면 실제로 발생).
_zip_list() {
  local z="$1" root="teammanagement-$2/" out="$3" all
  all=$(unzip -Z1 "$z") || return 1
  printf '%s\n' "$all" | grep -v "^${root}" | grep -q . && return 2
  printf '%s\n' "$all" | sed "s|^${root}||" | grep -v '/$' | grep -v '^$' | sort -u > "$out"
}

# 삭제 후보 = (직전 릴리스 ∪ 지난번에 못 지운 pending) ∖ 새 릴리스.
# ⚠️ 파이프로 엮지 말 것: pipefail 하에서 PEND_FILE 부재 시 cat 이 실패하면 파이프라인 전체가
#    실패로 판정되어 삭제가 통째로 생략된다(조용한 무동작).
_build_cand() {
  cp "$_d/prev.list" "$_d/cand.raw" || return 1
  if [ -s "$PEND_FILE" ]; then cat "$PEND_FILE" >> "$_d/cand.raw" || return 1; fi
  sort -u "$_d/cand.raw" > "$_d/cand.list" || return 1
  comm -23 "$_d/cand.list" "$_d/new.list" > "$_d/gone.list" || return 1
}

del_ok=1
_d=$(mktemp -d /tmp/trmt_del.XXXXXX)
if [ "$LOCAL" = "none" ] || ! _sha_ok "$LOCAL" || ! _sha_ok "$REMOTE"; then
  echo "$(date '+%F %T') WARN: SHA 형식 이상(.deployed_sha 손상 의심) — 삭제 전파 생략"; del_ok=0
elif [ ! -f "$PREV_ZIP" ]; then
  echo "$(date '+%F %T') WARN: 직전 릴리스 zip 없음 — 삭제 전파 생략(사라진 파일이 서버에 남을 수 있음)"; del_ok=0
elif ! _zip_list "$PREV_ZIP" "$LOCAL" "$_d/prev.list" || ! _zip_list "${NEW_ZIP}.part" "$REMOTE" "$_d/new.list"; then
  echo "$(date '+%F %T') WARN: zip 판독 실패 또는 archive root 규칙 불일치 — 삭제 전파 생략"; del_ok=0
elif ! _build_cand; then
  # `|| true` 로 삼키면 부분 목록으로 삭제할 수 있다 — 실패는 실패로 다룬다.
  echo "$(date '+%F %T') WARN: 삭제 목록 계산 실패 — 삭제 전파 생략"; del_ok=0
fi

if [ "$del_ok" = 1 ]; then
  MAX_DEL="${TRMT_MAX_DELETE:-50}"
  printf '%s' "$MAX_DEL" | grep -qE '^[0-9]+$' || MAX_DEL=50   # 빈값·비수치면 상한 우회됨
  gone_n=$(grep -cv '^$' "$_d/gone.list" || true)
  : > "$_d/pend.new"
  if [ "${gone_n:-0}" -eq 0 ]; then
    :
  elif [ "$gone_n" -gt "$MAX_DEL" ]; then
    # 급증은 zip 손상·경로 규칙 변경 같은 사고일 가능성이 높다. 지우지 말고 크게 알린다.
    # 다만 그냥 넘기면 누락이 영구화되므로(다음 회차는 LOCAL=REMOTE 로 조기종료) pending 에 남긴다.
    echo "$(date '+%F %T') WARN: 삭제 대상 ${gone_n}건 > 상한 ${MAX_DEL} — 이번 삭제 생략(수동 확인 필요, 다음 배포에서 재시도)"
    head -20 "$_d/gone.list" | sed 's/^/    /'
    cp "$_d/gone.list" "$_d/pend.new"
  else
    echo "$(date '+%F %T') repo 에서 사라진 파일 ${gone_n}건 제거"
    while IFS= read -r f; do
      [ -n "$f" ] || continue
      case "$f" in
        /*|../*|*/../*|*/..|..) echo "$(date '+%F %T') WARN: $f — 경로 탈출 의심, 건너뜀"; continue ;;
        instance/*|venv/*|uploads/*|static/uploads/*) continue ;;
      esac
      # 정상 문자만 자동 삭제한다. 개행·따옴표·역슬래시가 든 이름은 라인 기반 처리로 안전하지
      # 않으므로 사람이 보게 남긴다(우리 repo 엔 없다).
      printf '%s' "$f" | grep -qE '^[A-Za-z0-9._/-]+$' \
        || { echo "$(date '+%F %T') WARN: 비정상 문자 파일명 — 건너뜀"; continue; }
      t="$APP_DIR/$f"
      [ -e "$t" ] || [ -L "$t" ] || continue          # 이미 없음
      [ -L "$t" ] && { echo "$(date '+%F %T') WARN: $f 가 심볼릭 링크 — 건너뜀"; printf '%s\n' "$f" >> "$_d/pend.new"; continue; }
      [ -d "$t" ] && { echo "$(date '+%F %T') WARN: $f 가 디렉토리 — 건너뜀"; printf '%s\n' "$f" >> "$_d/pend.new"; continue; }
      # 중간 경로가 심볼릭 링크면 $APP_DIR 밖 파일을 지울 수 있다 — 실경로로 확인한다.
      p=$(cd "$(dirname "$t")" 2>/dev/null && pwd -P) || { printf '%s\n' "$f" >> "$_d/pend.new"; continue; }
      case "$p/" in "$APP_REAL"/*) : ;; *) echo "$(date '+%F %T') WARN: $f 가 앱 디렉토리 밖 — 건너뜀"; continue ;; esac
      rm -f "$t" && echo "    - $f" || printf '%s\n' "$f" >> "$_d/pend.new"
    done < "$_d/gone.list"
    # 지워진 .py 의 컴파일 캐시가 남아 import 가 계속 성공하는 걸 막는다.
    # 운영 자산(venv·instance·업로드) 내부는 건드리지 않는다.
    find "$APP_DIR" \( -path "$APP_DIR/venv" -o -path "$APP_DIR/instance" -o -path "$APP_DIR/static/uploads" \) -prune \
      -o -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
  fi
  if [ -s "$_d/pend.new" ]; then sort -u "$_d/pend.new" > "$PEND_FILE"; else rm -f "$PEND_FILE"; fi
fi
rm -rf "$_d"

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
