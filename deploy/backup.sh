#!/usr/bin/env bash
# TRMT 백업 — 매일 1회 systemd timer(trmt-backup.timer)가 호출.
#
# 왜 sqlite3 CLI 를 안 쓰나: 이 서버에 sqlite3 CLI 가 없다. 그리고 WAL 모드에서
# 파일을 그냥 cp 하면 -wal 에 남은 커밋을 놓쳐 깨진 스냅샷이 나온다.
# → venv python 의 sqlite3.Connection.backup() (온라인 백업 API)으로 뜬다.
#   서비스 중단·쓰기 차단 없이 일관된 스냅샷이 나오고, 뜬 직후 검증한다.
#
# 설계상 지키는 것 (2026-08-11 올마이트 검증에서 지적된 것들):
#   · 자기복사 후 exec  — autodeploy 의 cp -rf 가 실행 중 이 파일을 덮어써도 안전
#   · flock            — 타이머·수동 실행 중복 방지
#   · umask 077        — 백업물은 소유자만 읽음(DB 전체가 들어있다)
#   · 임시파일 → 검증 → atomic mv — 반쯤 쓰인 산출물이 "최신 백업"으로 오인되지 않음
#   · 검증 실패 시 .last_ok 를 갱신하지 않고 non-zero 종료 (systemd 가 failed 로 기록)
#   · manifest(테이블별 행수) 동시 생성 — 복구 리허설이 "행이 다 살아있나"를 비교할 근거
#
# 산출물 (기본 ~/backups/trmt):
#   db/trmt-YYYYmmdd-HHMMSS.db.gz + .manifest.json   매일, KEEP_DB 세트 보관
#   files/instance-YYYYmmdd.tar.gz                   FILES_EVERY_DAYS 지났을 때만
#   .last_ok / backup.log                            감시용
#
# 수동 실행: bash ~/app/deploy/backup.sh
set -euo pipefail
umask 077

# ---- 0) 자기복사 후 exec ----------------------------------------------------
# 배포(autodeploy 의 cp -rf)가 실행 중인 이 스크립트를 덮어쓰면 bash 가 남은 줄을
# 엉뚱하게 읽는다. /tmp 사본으로 넘겨서 그 경쟁 자체를 없앤다.
if [ -z "${TRMT_BACKUP_SELFEXEC:-}" ]; then
  _self="$(mktemp /tmp/trmt-backup-XXXXXX.sh)"
  cat "$0" > "$_self"
  export TRMT_BACKUP_SELFEXEC=1 TRMT_BACKUP_SELFCOPY="$_self"
  exec /bin/bash "$_self" "$@"
fi

APP_DIR="${TRMT_APP_DIR:-$HOME/app}"
DEST="${TRMT_BACKUP_DIR:-$HOME/backups/trmt}"
KEEP_DB="${TRMT_KEEP_DB:-30}"
KEEP_FILES="${TRMT_KEEP_FILES:-4}"       # 아카이브 1개가 ~370MB (static/uploads 포함)
FILES_EVERY_DAYS="${TRMT_FILES_EVERY_DAYS:-6}"

DB="$APP_DIR/instance/trmt.db"
PY="$APP_DIR/venv/bin/python3"
TS="$(date '+%Y%m%d-%H%M%S')"

mkdir -p "$DEST/db" "$DEST/files"
chmod 700 "$DEST" "$DEST/db" "$DEST/files" 2>/dev/null || true
LOG="$DEST/backup.log"
log() { echo "$(date '+%F %T') $*" | tee -a "$LOG"; }

CLEAN=()
cleanup() {
  if [ "${#CLEAN[@]}" -gt 0 ]; then rm -f -- "${CLEAN[@]}" 2>/dev/null || true; fi
  [ -n "${TRMT_BACKUP_SELFCOPY:-}" ] && rm -f -- "$TRMT_BACKUP_SELFCOPY" 2>/dev/null || true
}
trap cleanup EXIT

# ---- 1) 중복 실행 방지 ------------------------------------------------------
exec 9>"$DEST/.lock"
if ! flock -n 9; then log "이미 실행 중 → 이번 실행 종료"; exit 0; fi

[ -f "$DB" ] || { log "❌ DB 없음: $DB"; exit 1; }
[ -x "$PY" ] || { log "❌ python 없음: $PY"; exit 1; }

# 지난 실행이 죽여놓고 간 잔여물 청소(1일 이상 된 것만 — 동시 실행은 위 flock 이 막음)
find "$DEST/db" -maxdepth 1 -name '.partial-*' -mtime +0 -delete 2>/dev/null || true
find "$DEST/files" -maxdepth 1 -name '*.partial' -mtime +0 -delete 2>/dev/null || true

TMP="$DEST/db/.partial-$TS.db"
TMPMF="$DEST/db/.partial-$TS.manifest.json"
TMPGZ="$DEST/db/.partial-$TS.db.gz"
OUT="$DEST/db/trmt-$TS.db.gz"
OUTMF="$DEST/db/trmt-$TS.manifest.json"
CLEAN+=("$TMP" "$TMPMF" "$TMPGZ")

# ---- 2) DB 온라인 백업 + 검증 + manifest -----------------------------------
# 검증 = integrity_check ok · foreign_key_check 0건(운영 실측 0) · 핵심 테이블 존재 ·
#        테이블 수 하한. 유효한 SQLite 지만 TRMT 로 못 쓰는 파일이 통과하지 않게 한다.
"$PY" - "$DB" "$TMP" "$TMPMF" <<'PYEOF'
import json, sqlite3, sys
src_path, dst_path, mf_path = sys.argv[1], sys.argv[2], sys.argv[3]
REQUIRED = ('users', 'vessels', 'issues', 'dock_procure', 'aor_draft', 'attachments', 'api_settings')
MIN_TABLES = 40                      # 운영 실측 60개. 스키마가 통째로 날아간 파일을 걸러낸다.

src = sqlite3.connect(f'file:{src_path}?mode=ro', uri=True)
dst = sqlite3.connect(dst_path)
with dst:
    src.backup(dst)                  # 온라인 백업 — 쓰기 중에도 일관된 스냅샷
src.close()

chk = dst.execute('PRAGMA integrity_check').fetchone()[0]
if chk != 'ok':
    print(f'integrity_check FAILED: {chk}', file=sys.stderr); sys.exit(2)
fk = dst.execute('PRAGMA foreign_key_check').fetchall()
if fk:
    print(f'foreign_key_check FAILED: {len(fk)}건 위반', file=sys.stderr); sys.exit(3)

tables = [r[0] for r in dst.execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
missing = [t for t in REQUIRED if t not in tables]
if missing:
    print(f'핵심 테이블 누락: {missing}', file=sys.stderr); sys.exit(4)
if len(tables) < MIN_TABLES:
    print(f'테이블 수 {len(tables)} < 하한 {MIN_TABLES}', file=sys.stderr); sys.exit(5)

counts = {}
for t in tables:
    counts[t] = dst.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
uv = dst.execute('PRAGMA user_version').fetchone()[0]
dst.close()

with open(mf_path, 'w', encoding='utf-8') as f:
    json.dump({'source': src_path, 'user_version': uv,
               'tables': len(tables), 'counts': counts}, f, ensure_ascii=False, indent=1)
print(f'tables={len(tables)} integrity=ok fk=0 rows={sum(counts.values())}')
PYEOF

SIZE_RAW="$(stat -c%s "$TMP")"
gzip -9 -c "$TMP" > "$TMPGZ"
gzip -t "$TMPGZ"                       # 압축물 자체 검증 후에만 최종 이름으로 승격
mv "$TMPGZ" "$OUT"
mv "$TMPMF" "$OUTMF"
rm -f "$TMP"
CLEAN=()
log "db backup ok: $(basename "$OUT") ($(stat -c%s "$OUT") bytes, raw $SIZE_RAW)"

# ---- 3) 업로드·첨부 원본 (자주 안 바뀌므로 주기 실행) ----------------------
# 대상 = 서버에만 존재하는 런타임 데이터 2곳:
#   instance/       미리보기 cache·PDF·STT 오디오·fleet json·.secret_key
#   static/uploads/ attachments 테이블이 가리키는 첨부 원본 (git 추적 3개, 서버 실물 109개)
# ⚠️ static/uploads 는 2026-08-11 복구 리허설에서 빠진 게 발각된 곳이다. 지우지 말 것.
# data/·yard_profiles/·static/ota 는 전부 git 추적이라 GitHub 에서 복원 가능 → 제외.
# 주의: DB 와 이 아카이브는 동일 시점 스냅샷이 아니다 → 첨부 RPO 는 최대 FILES_EVERY_DAYS 일.
# 신선도 판정은 신규 포맷(files-*)만 본다. 구 포맷(instance-*)은 static/uploads 가 없어서
# 그걸 "최근 백업 있음"으로 세면 첨부 원본이 FILES_EVERY_DAYS 동안 무백업으로 남는다.
NEWEST="$(find "$DEST/files" -name 'files-*.tar.gz' -mtime "-$FILES_EVERY_DAYS" -print -quit 2>/dev/null || true)"
if [ -z "$NEWEST" ]; then
  FOUT="$DEST/files/files-$(date '+%Y%m%d').tar.gz"
  FTMP="$FOUT.partial"
  CLEAN+=("$FTMP")
  rc=0
  # DB 는 위에서 따로 떴으므로 제외. 옛 수동 스냅샷·삭제보관·캐시도 제외.
  tar -czf "$FTMP" -C "$APP_DIR" \
      --exclude='instance/trmt.db*' \
      --exclude='instance/backups' \
      --exclude='instance/deleted-files-*' \
      --exclude='__pycache__' \
      instance static/uploads || rc=$?
  # tar 는 "읽는 중 파일이 바뀜"에 1 을 낸다(부분 손상 아님). 2 이상은 실패.
  if [ "$rc" -gt 1 ]; then log "❌ tar 실패 rc=$rc"; exit 1; fi
  [ "$rc" -eq 1 ] && log "⚠️ tar rc=1 (읽는 중 변경된 파일 있음 — 아카이브는 유효)"
  tar -tzf "$FTMP" >/dev/null            # 실제로 풀리는지 확인 후 승격
  mv "$FTMP" "$FOUT"
  CLEAN=()
  log "files backup ok: $(basename "$FOUT") ($(stat -c%s "$FOUT") bytes)"
else
  log "files backup skip (최근 ${FILES_EVERY_DAYS}일 내 존재: $(basename "$NEWEST"))"
fi

# ---- 4) 보관기간 정리 -------------------------------------------------------
prune() { # $1=dir $2=glob $3=keep
  local n; n=$(find "$1" -maxdepth 1 -name "$2" | wc -l)
  if [ "$n" -gt "$3" ]; then
    find "$1" -maxdepth 1 -name "$2" -printf '%T@ %p\n' | sort -rn | tail -n +$(( $3 + 1 )) \
      | cut -d' ' -f2- | while IFS= read -r f; do rm -f -- "$f"; log "prune: $(basename "$f")"; done
  fi
}
prune "$DEST/db"    'trmt-*.db.gz'       "$KEEP_DB"
prune "$DEST/db"    'trmt-*.manifest.json' "$KEEP_DB"
prune "$DEST/files" 'files-*.tar.gz'     "$KEEP_FILES"
prune "$DEST/files" 'instance-*.tar.gz'  1      # 구 이름(static/uploads 미포함) — 1개만 남김

# ---- 5) 감시용 상태 파일 (여기까지 왔다는 건 검증까지 통과한 것) --------------
printf '%s %s\n' "$(date '+%F %T')" "$(basename "$OUT")" > "$DEST/.last_ok"
log "done. db=$(find "$DEST/db" -name 'trmt-*.db.gz' | wc -l)개 files=$(find "$DEST/files" -name 'files-*.tar.gz' | wc -l)개 총 $(du -sh "$DEST" | cut -f1)"
