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
if ! mkdir "$LOCK" 2>/dev/null; then exit 0; fi
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

# 3) retention. Manifests without retained archives are removed too.
ls -1t "$DBDIR"/trmt-*.db.gz 2>/dev/null | tail -n +$((KEEP_DB+1)) | xargs -r rm -f 2>/dev/null || true
ls -1t "$FILESDIR"/files-*.tar.gz 2>/dev/null | tail -n +$((KEEP_FILES+1)) | while IFS= read -r f; do rm -f "$f" "$f.manifest.json"; done
SZDB="$(du -h "$DBGZ" | cut -f1)"; SZFILES="$(du -h "$LARCH" | cut -f1)"
echo "$(ts) OK db=$(basename "$DBGZ")/$SZDB files=$(basename "$LARCH")/$SZFILES sha256=${WANT:0:12}" >>"$LOG"
