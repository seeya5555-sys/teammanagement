#!/usr/bin/env bash
# TRMT 배포 롤백 — 보관된 직전 릴리스 zip 으로 되돌린다.
#
# 왜 zip 을 보관하나: archive/<sha>.zip 은 immutable 이라 "그 커밋의 정확한 내용"이 보장된다.
# 롤백 시점에 네트워크/GitHub 가 죽어 있어도 되돌릴 수 있어야 하므로 로컬에 들고 있는다.
# (브랜치 zip 은 codeload 캐시 alias 라 절대 쓰지 않는다 — 2026-07-29 build 101 사고 참조)
#
# 사용:
#   ./deploy/rollback.sh              # 직전 릴리스로
#   ./deploy/rollback.sh --to <sha>   # 보관된 특정 릴리스로
#   ./deploy/rollback.sh --list       # 보관 목록
#   ./deploy/rollback.sh --unhold     # hold 해제만(재배포 재개)
set -euo pipefail

APP_DIR="${TRMT_APP_DIR:-$HOME/app}"
REL_DIR="${TRMT_RELEASES_DIR:-$HOME/releases}"
SHA_FILE="$APP_DIR/.deployed_sha"
HOLD_FILE="$APP_DIR/.deploy_hold"
LOCK_FILE="${TRMT_LOCK_FILE:-$APP_DIR/.deploy.lock}"
ts() { date '+%F %T'; }

# 🔒 배포 도구 자신은 롤백하지 않는다 (deploy/ 제외).
# 이걸 되돌리면 hold 로직이 없던 구버전 autodeploy.sh 가 복원되어, 다음 타이머가 hold 를
# 무시하고 방금 되돌린 나쁜 커밋을 그대로 재배포한다 = 롤백이 무의미해짐.
# 배포 도구는 항상 전진(최신)만 한다. 도구 자체를 되돌려야 하면 revert 커밋으로 처리할 것.
EXCLUDE_RE='^deploy/'

usage() { sed -n '2,14p' "$0"; exit "${1:-0}"; }

# 보관 릴리스를 최신순으로 나열. 0건이어도 조용히 성공(set -e 로 조기 종료하지 않게).
list_releases() { ls -1t "$REL_DIR"/*.zip 2>/dev/null || true; }

TARGET=""
case "${1:-}" in
  --list)
    echo "보관 릴리스 ($REL_DIR):"
    cur=$(cat "$SHA_FILE" 2>/dev/null || echo x)
    list_releases | while read -r z; do
      s=$(basename "$z" .zip)
      mark=""
      [ "$s" = "$cur" ] && mark="  <- 현재 배포본"
      printf '  %s  %s%s\n' "${s:0:7}" "$(du -h "$z" | cut -f1)" "$mark"
    done
    if [ -s "$HOLD_FILE" ]; then
      echo "hold (아래 SHA 는 자동배포 차단 중):"
      cut -c1-7 < "$HOLD_FILE" | sed 's/^/  - /'
    fi
    exit 0 ;;
  --unhold)
    rm -f "$HOLD_FILE"
    echo "$(ts) hold 해제 — 다음 타이머부터 자동배포 재개"
    exit 0 ;;
  --to)
    TARGET="${2:-}"; [ -n "$TARGET" ] || usage 2 ;;
  -h|--help) usage 0 ;;
  "") : ;;
  *) usage 2 ;;
esac

# 🔒 autodeploy 타이머(60초)와의 경합 차단. 롤백 도중 타이머가 깨어나 같은 디렉토리를
# 덮어쓰면 반쯤 섞인 상태가 된다. autodeploy.sh 도 같은 락을 잡는다.
exec 9>"$LOCK_FILE"
if ! flock -w 300 9; then
  echo "$(ts) FATAL: 배포 락 획득 실패(5분) — autodeploy 가 도는 중일 수 있음"; exit 1
fi

CURRENT=$(cat "$SHA_FILE" 2>/dev/null || echo "")
[ -n "$CURRENT" ] || { echo "$(ts) FATAL: $SHA_FILE 없음 — 현재 배포본을 알 수 없어 롤백 불가"; exit 1; }

# 대상 결정: 지정 없으면 "현재가 아닌 가장 최근 보관본"
if [ -z "$TARGET" ]; then
  TARGET=$(list_releases \
    | while read -r z; do s=$(basename "$z" .zip); [ "$s" != "$CURRENT" ] && echo "$s"; done \
    | head -1 || true)
fi
[ -n "$TARGET" ] || { echo "$(ts) FATAL: 되돌릴 이전 릴리스가 $REL_DIR 에 없음"; exit 1; }
[ "$TARGET" = "$CURRENT" ] && { echo "$(ts) 대상이 현재 배포본과 같음 — 할 일 없음"; exit 0; }

TARGET_ZIP="$REL_DIR/${TARGET}.zip"
[ -f "$TARGET_ZIP" ] || { echo "$(ts) FATAL: $TARGET_ZIP 없음"; exit 1; }

echo "$(ts) rollback ${CURRENT:0:7} -> ${TARGET:0:7}"

WORK=$(mktemp -d /tmp/trmt_rb.XXXXXX)
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

unzip -oq "$TARGET_ZIP" -d "$WORK"
SRC="$WORK/teammanagement-${TARGET}"
[ -d "$SRC" ] || { echo "$(ts) FATAL: zip 구조가 예상과 다름 ($SRC 없음)"; exit 1; }
# 배포본이 맞는지 최소 확인 — 빈/깨진 zip 을 그대로 덮어써 서비스를 죽이지 않는다.
[ -f "$SRC/app.py" ] || { echo "$(ts) FATAL: $TARGET zip 에 app.py 가 없음 — 손상 의심, 중단"; exit 1; }

# ── 삭제 전파: cp -rf 는 "사라진 파일"을 지우지 않는다.
# 현재 릴리스 zip 도 보관돼 있으면, (현재에만 있고 대상에 없는 파일) = 나쁜 커밋이 추가한 파일 →
# 명시적으로 지운다. 지우는 대상은 반드시 "현재 zip 에 들어있던 경로"로 한정하므로
# instance/·venv/·uploads/ 같은 런타임 데이터는 애초에 후보에 오르지 않는다.
CUR_ZIP="$REL_DIR/${CURRENT}.zip"
if [ -f "$CUR_ZIP" ]; then
  unzip -Z1 "$CUR_ZIP" | sed "s|^teammanagement-${CURRENT}/||" | grep -v '/$' | sort -u > "$WORK/cur.list"
  unzip -Z1 "$TARGET_ZIP" | sed "s|^teammanagement-${TARGET}/||" | grep -v '/$' | sort -u > "$WORK/tgt.list"
  comm -23 "$WORK/cur.list" "$WORK/tgt.list" > "$WORK/stale.list" || true
  n=$(grep -cv '^$' "$WORK/stale.list" || true)
  if [ "${n:-0}" -gt 0 ]; then
    echo "$(ts) 대상에 없는 파일 ${n}건 검토"
    while IFS= read -r f; do
      [ -n "$f" ] || continue
      # 런타임 데이터와 배포 도구는 절대 건드리지 않는다.
      # 🔴 static/uploads/ 를 반드시 포함할 것. repo 는 첨부 몇 개를 실제로 추적하고 있어서
      #    (git ls-files static/uploads → 3건) 그 경로가 릴리스 zip 에 들어간다. 즉 차집합
      #    후보에 오를 수 있고, 빠뜨리면 롤백이 사용자 첨부 원본을 지운다.
      #    `uploads/*` 패턴은 prefix 매칭이라 `static/uploads/x` 에 맞지 않는다 — 별도로 적어야 한다.
      #    autodeploy.sh 의 보호 목록과 항상 같이 고칠 것(둘이 갈리면 한쪽에서만 사고 난다).
      case "$f" in instance/*|venv/*|uploads/*|static/uploads/*|/*|../*|*/../*|*/..|..) continue ;; esac
      printf '%s\n' "$f" | grep -qE "$EXCLUDE_RE" && continue
      [ -d "$APP_DIR/$f" ] && { echo "$(ts) WARN: $f 가 디렉토리 — 수동 확인 필요, 건너뜀"; continue; }
      rm -f "$APP_DIR/$f"
    done < "$WORK/stale.list"
  fi
else
  echo "$(ts) WARN: 현재 릴리스 zip($CURRENT) 미보관 — 삭제 전파 생략(새로 추가된 파일이 남을 수 있음)"
fi

# 배포 도구는 되돌리지 않는다(위 EXCLUDE_RE 주석 참조) — 복원 소스에서 통째로 뺀다.
rm -rf "$SRC/deploy"
cp -rf "$SRC/." "$APP_DIR/"
cd "$APP_DIR"

# hold: 이 SHA 로는 다시 자동배포하지 않는다. 이게 없으면 락 해제 직후 타이머가
# 방금 되돌린 나쁜 커밋을 그대로 다시 배포한다(롤백이 무의미해짐).
#
# 🔴 덮어쓰지 말고 누적할 것. 단일 값으로 두면 연속 롤백에서 앞의 차단이 풀린다:
#    main HEAD 가 A(나쁨) → A→B 롤백(hold=A) → B 도 나빠서 B→C 롤백(hold=B 로 덮임)
#    → REMOTE 는 여전히 A 인데 hold 에 없으니 다음 타이머가 A 를 재배포한다.
#    차단된 SHA 는 집합으로 들고, main 에 새 커밋이 오면 그 SHA 는 집합에 없으므로 자연히 배포된다
#    (= 자동 해제를 위해 파일을 지울 필요가 없다. 지우면 옛 나쁜 커밋의 차단까지 같이 풀린다).
# 아래 검증이 실패해도 hold/sha 는 남긴다 — 디스크의 코드가 실제로 TARGET 이므로
# 그게 사실에 부합하고, 실패했다고 나쁜 커밋으로 되돌아가는 편이 더 위험하다.
# 대신 실패 시 조용히 멈추지 않도록 아래에서 크게 실패시킨다.
{ printf '%s\n' "$CURRENT"; cat "$HOLD_FILE" 2>/dev/null || true; } \
  | grep -E '^[0-9a-f]{40}$' | awk '!seen[$0]++' | head -20 > "$HOLD_FILE.tmp"
mv "$HOLD_FILE.tmp" "$HOLD_FILE"
echo "$TARGET" > "$SHA_FILE"
# 되돌린 릴리스를 최신으로 표시 — 보관 정리(mtime 기준)에서 밀려나지 않게 하고,
# 다음 롤백의 "직전" 계산이 실제 배포 이력과 어긋나지 않게 한다.
touch "$TARGET_ZIP"

# 스키마는 전진만 한다(마이그레이션 되돌리기는 하지 않음) — 구버전 코드가 신버전 스키마 위에서
# 도는 형태가 되므로, 컬럼 추가형 마이그레이션에서만 안전하다. 파괴적 마이그레이션이 섞였다면
# 코드 롤백만으로 부족하니 DB 백업 복구(deploy/restore-check.sh 계열)를 같이 검토할 것.
# (trmt.service 의 ExecStartPre 도 같은 걸 돌리지만 idempotent 하고, 여기서 먼저 돌려야
#  마이그레이션 실패를 restart 전에 잡는다.)
venv/bin/python3 -c "import app; app.init_db(drop=False); app._auto_migrate()"
sudo systemctl restart trmt

# 라이브 검증 — restart 성공이 곧 서비스 정상은 아니다. gunicorn bind = 0.0.0.0:5000
ok=0
for _ in $(seq 1 30); do
  sleep 2
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:5000/login || echo 000)
  [ "$code" = "200" ] && { ok=1; break; }
done
if [ "$ok" != "1" ]; then
  cat <<EOF
$(ts) FATAL: 롤백 후 /login 이 200 이 아님 (마지막 응답: ${code:-없음})
  · 디스크의 코드는 ${TARGET:0:7} 로 되돌아간 상태다(.deployed_sha 도 그렇게 기록됨).
  · hold=${CURRENT:0:7} 가 걸려 있어 자동배포는 멈춰 있다 — 방치하면 계속 멈춘다.
  · 확인: sudo systemctl status trmt / journalctl -u trmt -n 50
  · 재개: deploy/rollback.sh --unhold (원인 해결 후) 또는 --to <다른 sha>
EOF
  exit 1
fi

echo "$(ts) rollback done -> ${TARGET:0:7} (hold=${CURRENT:0:7}, 해제는 --unhold 또는 새 커밋)"
