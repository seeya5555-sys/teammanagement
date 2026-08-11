#!/bin/bash
# TRMT off-host backup pull (macOS launchd, daily after server backup timer).
# DB는 fresh online snapshot을 매일 당기고, 최신 files archive+manifest도 검증 후 보관한다.
set -euo pipefail
umask 077

A1_KEY="${TRMT_A1_KEY:-$HOME/.ssh/trmt-a1-ed25519}"
A1="${TRMT_A1:-opc@168.107.9.169}"
BKROOT="${TRMT_OFFSITE_DIR:-$HOME/.openclaw/backups}"
DBDIR="$BKROOT/trmt-db"
FILESDIR="$BKROOT/trmt-files"
LOG="$DBDIR/backup.log"
KEEP_DB="${TRMT_OFFSITE_KEEP_DB:-7}"
KEEP_FILES="${TRMT_OFFSITE_KEEP_FILES:-4}"
mkdir -p "$DBDIR" "$FILESDIR"
LOCK="$BKROOT/.trmt-offsite-pull.lock"
# 🔴 mkdir 락은 trap 이 안 돌면(SIGKILL·강제종료·맥 절전 중 프로세스 정리) 영구히 남는다.
#    그대로 `exit 0` 하면 이후 모든 실행이 로그 한 줄 없이 조용히 죽어서, off-host 백업이
#    멈춘 걸 아무도 모르게 된다(= 정확히 우리가 없애려던 silent no-op).
#    그래서 (a) 오래된 락은 stale 로 보고 회수하고 (b) 넘길 때도 반드시 로그를 남긴다.
LOCK_STALE_MIN="${TRMT_OFFSITE_LOCK_STALE_MIN:-120}"
if ! mkdir "$LOCK" 2>/dev/null; then
  if [ -d "$LOCK" ] && [ -z "$(find "$LOCK" -maxdepth 0 -mmin "-$LOCK_STALE_MIN" 2>/dev/null)" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') WARN: ${LOCK_STALE_MIN}분 넘은 stale lock 회수 후 진행" >>"$LOG"
    rmdir "$LOCK" 2>/dev/null || true
    mkdir "$LOCK" 2>/dev/null || { echo "$(date '+%Y-%m-%d %H:%M:%S') SKIP: lock 회수 실패" >>"$LOG"; exit 0; }
  else
    echo "$(date '+%Y-%m-%d %H:%M:%S') SKIP: 다른 실행이 진행 중(lock)" >>"$LOG"
    exit 0
  fi
fi
TMP=()
cleanup() { [ "${#TMP[@]}" -eq 0 ] || rm -f -- "${TMP[@]}" 2>/dev/null || true; rmdir "$LOCK" 2>/dev/null || true; }
trap cleanup EXIT

ts(){ date '+%Y-%m-%d %H:%M:%S'; }
fail(){ echo "$(ts) FAIL: $*" >>"$LOG"; exit 1; }
SSH=(ssh -i "$A1_KEY" -o BatchMode=yes -o ConnectTimeout=15 "$A1")
SCP=(scp -q -i "$A1_KEY" -o BatchMode=yes)
STAMP="$(date +%Y%m%d-%H%M)"

# 1) fresh DB snapshot: remote online backup -> local integrity -> gzip -> atomic rename.
REMOTE_TMP="/tmp/trmt-offsite-${STAMP}-$$.db"
"${SSH[@]}" "REMOTE_TMP='$REMOTE_TMP' /home/opc/app/venv/bin/python3 - <<'PY'
import os, sqlite3
src = sqlite3.connect('file:/home/opc/app/instance/trmt.db?mode=ro', uri=True)
dst = sqlite3.connect(os.environ['REMOTE_TMP'])
with dst: src.backup(dst)
src.close()
ok = dst.execute('PRAGMA integrity_check').fetchone()[0]
dst.close()
if ok != 'ok': raise SystemExit('remote integrity: ' + str(ok))
PY" || fail "A1 DB snapshot/integrity failed"
DBTMP="$DBDIR/.incoming-$STAMP.db"
TMP+=("$DBTMP" "$DBTMP-shm" "$DBTMP-wal" "$DBTMP-journal")
"${SCP[@]}" "$A1:$REMOTE_TMP" "$DBTMP" || fail "DB scp failed"
"${SSH[@]}" "rm -f '$REMOTE_TMP'" 2>/dev/null || true
loc="$(sqlite3 "$DBTMP" 'PRAGMA integrity_check;' 2>/dev/null || echo err)"
[ "$loc" = ok ] || fail "local DB integrity failed ($loc)"
DBGZTMP="$DBDIR/.trmt-$STAMP.db.gz.partial"; TMP+=("$DBGZTMP")
gzip -c "$DBTMP" > "$DBGZTMP"
gzip -t "$DBGZTMP"
DBGZ="$DBDIR/trmt-$STAMP.db.gz"
mv "$DBGZTMP" "$DBGZ"
rm -f "$DBTMP" "$DBTMP-shm" "$DBTMP-wal" "$DBTMP-journal"

# 2) latest server files archive: manifest first, then .partial archive, sha256+tar verify, atomic rename.
# Server naming contract: files-*.tar.gz.manifest.json
RMF="$("${SSH[@]}" "find /home/opc/backups/trmt/files -maxdepth 1 -name 'files-*.tar.gz.manifest.json' -printf '%T@ %f\n' | sort -rn | head -1 | cut -d' ' -f2-")"
[ -n "$RMF" ] || fail "remote files manifest missing"
case "$RMF" in files-*.tar.gz.manifest.json) ;; *) fail "unsafe remote manifest name";; esac
RARCH="${RMF%.manifest.json}"
LMF="$FILESDIR/$RMF"; LARCH="$FILESDIR/$RARCH"
MF_TMP="$LMF.partial"; TMP+=("$MF_TMP")
"${SCP[@]}" "$A1:/home/opc/backups/trmt/files/$RMF" "$MF_TMP" || fail "files manifest scp failed"
WANT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["sha256"])' "$MF_TMP")"
case "$WANT" in [0-9a-f][0-9a-f]*) [ "${#WANT}" -eq 64 ] || fail "invalid sha256 length";; *) fail "invalid sha256";; esac
if [ ! -f "$LARCH" ] || [ "$(shasum -a 256 "$LARCH" | cut -d' ' -f1)" != "$WANT" ]; then
  ARCH_TMP="$LARCH.partial"; TMP+=("$ARCH_TMP")
  rm -f "$ARCH_TMP"
  "${SCP[@]}" "$A1:/home/opc/backups/trmt/files/$RARCH" "$ARCH_TMP" || fail "files archive scp failed"
  GOT="$(shasum -a 256 "$ARCH_TMP" | cut -d' ' -f1)"
  [ "$GOT" = "$WANT" ] || fail "files archive sha256 mismatch"
  tar -tzf "$ARCH_TMP" >/dev/null || fail "files archive tar validation failed"
  mv "$ARCH_TMP" "$LARCH"
fi
mv "$MF_TMP" "$LMF"

# 2-b) files archive 와 "짝이 맞는" DB 도 같이 가져온다.
# 🔴 위 1) 에서 뜬 DB 는 방금 찍은 fresh snapshot 이라 이 archive 와 시점이 다르다. 서버 backup.sh 는
#    "paired DB 가 참조하는 첨부가 archive 에 전부 있는지"를 검증하고 승격하는데, off-host 에
#    fresh DB 만 두면 그 짝 보장이 깨진다 — fresh DB 가 참조하는 새 첨부는 (files 는 주기 실행이라)
#    archive 에 없을 수 있다. off-host 만으로 복구하는 상황에서 첨부 누락이 되는 경로다.
#    그래서 fresh DB(최신성) 와 paired DB(정합성) 를 둘 다 보관한다.
PAIRED="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("db_backup",""))' "$LMF" 2>/dev/null || true)"
case "$PAIRED" in
  trmt-*.db.gz)
    if [ ! -f "$FILESDIR/$PAIRED" ]; then
      P_TMP="$FILESDIR/$PAIRED.partial"; TMP+=("$P_TMP")
      if "${SCP[@]}" "$A1:/home/opc/backups/trmt/db/$PAIRED" "$P_TMP" 2>/dev/null; then
        if gzip -t "$P_TMP" 2>/dev/null; then
          mv "$P_TMP" "$FILESDIR/$PAIRED"
          "${SCP[@]}" "$A1:/home/opc/backups/trmt/db/${PAIRED%.db.gz}.manifest.json" \
            "$FILESDIR/${PAIRED%.db.gz}.manifest.json" 2>/dev/null || true
        else
          fail "paired DB gzip 검증 실패 ($PAIRED)"
        fi
      else
        # 서버 보관기간이 지나 이미 정리된 경우 — archive 만으로는 첨부 정합성을 확인할 수 없다.
        echo "$(ts) WARN: paired DB $PAIRED 를 서버에서 못 받음(보관기간 만료 의심)" >>"$LOG"
      fi
    fi ;;
  *) echo "$(ts) WARN: files manifest 에 db_backup 이 없음 — paired DB 미확보" >>"$LOG" ;;
esac

# 2-c) drydock(Dock Manager) DB 도 off-host 로 내린다.
# 서버 backup.sh 가 fleet.db 를 뜨긴 하지만 그건 서버 안에만 있다 = 서버가 통째로 죽으면 같이 죽는다.
# 229MB 라 매번 받지 않고, 로컬에 같은 이름이 없을 때만 받는다(fleet.db 는 거의 안 바뀜).
DRYDIR="$BKROOT/trmt-drydock"
KEEP_DRYDOCK="${TRMT_OFFSITE_KEEP_DRYDOCK:-2}"
mkdir -p "$DRYDIR"
RDRY="$("${SSH[@]}" "find /home/opc/backups/trmt/db -maxdepth 1 -name 'fleet-*.db.gz' -printf '%T@ %f\n' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-" || true)"
case "$RDRY" in
  fleet-*.db.gz)
    if [ ! -f "$DRYDIR/$RDRY" ]; then
      D_TMP="$DRYDIR/$RDRY.partial"; TMP+=("$D_TMP")
      rm -f "$D_TMP"
      if "${SCP[@]}" "$A1:/home/opc/backups/trmt/db/$RDRY" "$D_TMP" 2>/dev/null && gzip -t "$D_TMP" 2>/dev/null; then
        mv "$D_TMP" "$DRYDIR/$RDRY"
        "${SCP[@]}" "$A1:/home/opc/backups/trmt/db/${RDRY%.db.gz}.manifest.json" \
          "$DRYDIR/${RDRY%.db.gz}.manifest.json" 2>/dev/null || true
        echo "$(ts) OK drydock=$RDRY/$(du -h "$DRYDIR/$RDRY" | cut -f1)" >>"$LOG"
      else
        # 여기서 죽이지 않는다 — TRMT 백업은 이미 성공적으로 내려왔고, 그걸 drydock 때문에
        # 실패로 만들면 진짜 TRMT 실패와 구분이 안 된다. 대신 반드시 로그에 남긴다.
        echo "$(ts) WARN: drydock DB $RDRY 확보 실패" >>"$LOG"
      fi
    fi
    ls -1t "$DRYDIR"/fleet-*.db.gz 2>/dev/null | tail -n +$((KEEP_DRYDOCK+1)) \
      | while IFS= read -r f; do rm -f "$f" "${f%.db.gz}.manifest.json"; done ;;
  *) echo "$(ts) WARN: 서버에 drydock 백업 없음 — backup.sh 의 drydock 구간 확인 필요" >>"$LOG" ;;
esac

# files archive 가 너무 오래됐으면 알린다. 서버 backup.sh 가 죽어도 여기서는 "가장 최신"을
# 계속 성공적으로 받아오므로, 신선도를 안 보면 영원히 OK 로 보인다(false green).
FILES_MAX_DAYS="${TRMT_OFFSITE_FILES_MAX_DAYS:-8}"
if [ -z "$(find "$LARCH" -maxdepth 0 -mtime "-$FILES_MAX_DAYS" 2>/dev/null)" ]; then
  echo "$(ts) WARN: files archive 가 ${FILES_MAX_DAYS}일 이상 오래됨($(basename "$LARCH")) — 서버 백업 점검 필요" >>"$LOG"
fi

# 3) retention. Manifests without retained archives are removed too.
ls -1t "$DBDIR"/trmt-*.db.gz 2>/dev/null | tail -n +$((KEEP_DB+1)) | xargs -r rm -f 2>/dev/null || true
ls -1t "$FILESDIR"/files-*.tar.gz 2>/dev/null | tail -n +$((KEEP_FILES+1)) | while IFS= read -r f; do rm -f "$f" "$f.manifest.json"; done
SZDB="$(du -h "$DBGZ" | cut -f1)"; SZFILES="$(du -h "$LARCH" | cut -f1)"
echo "$(ts) OK db=$(basename "$DBGZ")/$SZDB files=$(basename "$LARCH")/$SZFILES sha256=${WANT:0:12}" >>"$LOG"
