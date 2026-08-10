#!/usr/bin/env bash
# 복구 리허설 — 백업이 "실제로 되살아나는지" 확인한다. 프로덕션 DB 는 건드리지 않는다.
#
# 백업 파일이 생겼다는 사실은 복구 가능성을 증명하지 않는다. 여기서 보는 것:
#   1) 최신 백업 압축 해제 (+ gzip -t)
#   2) PRAGMA integrity_check == ok, foreign_key_check == 0건
#   3) manifest 대조 — 테이블 수·테이블별 행수가 백업 시점과 일치하는가
#   4) 앱 코드(app.py)로 그 DB 를 실제로 열어 읽고, 사본에 쓰기까지 되는가
#   5) Flask test client 로 /login 200 (앱이 기동됨)
#   6) 최신 첨부 아카이브가 실제로 풀리는가 (tar -tzf) + 첨부 참조 표본 존재 확인
# 하나라도 실패하면 non-zero 로 죽는다.
#
# ⚠️ 이것이 증명하지 않는 것: 인증된 사용자 시나리오 전체, 외부 연동(SVMS·APNs),
#    운영 서비스로의 실제 컷오버 절차와 RTO. 그건 별도 절차로 확인해야 한다.
#
# 수동 실행: bash ~/app/deploy/restore-check.sh
set -euo pipefail
APP_DIR="${TRMT_APP_DIR:-$HOME/app}"
DEST="${TRMT_BACKUP_DIR:-$HOME/backups/trmt}"
PY="$APP_DIR/venv/bin/python3"

LATEST="$(find "$DEST/db" -maxdepth 1 -name 'trmt-*.db.gz' -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-)"
[ -n "$LATEST" ] || { echo "❌ 백업 없음: $DEST/db"; exit 1; }
MF="${LATEST%.db.gz}.manifest.json"
[ -f "$MF" ] || { echo "❌ manifest 없음: $MF"; exit 1; }

WORK="$(mktemp -d /tmp/trmt-restore-XXXXXX)"
trap 'rm -rf "$WORK"' EXIT
echo "· 대상 백업 : $LATEST ($(stat -c%s "$LATEST") bytes)"
gzip -t "$LATEST"
gunzip -c "$LATEST" > "$WORK/trmt.db"
echo "· 압축 해제 : $(stat -c%s "$WORK/trmt.db") bytes"

FARCH="$(find "$DEST/files" -maxdepth 1 -name 'instance-*.tar.gz' -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-)"
if [ -n "$FARCH" ]; then
  tar -tzf "$FARCH" > "$WORK/files.list"
  echo "· 첨부 아카이브: $(basename "$FARCH") — 항목 $(wc -l < "$WORK/files.list")개, 정상 해독"
else
  echo "⚠️ 첨부 아카이브 없음(아직 주기 도달 안 함) — DB 만 검증함"
fi

cd "$APP_DIR"
"$PY" - "$WORK/trmt.db" "$MF" "${WORK}/files.list" <<'PYEOF'
import json, os, sqlite3, sys
p, mf_path, flist = sys.argv[1], sys.argv[2], sys.argv[3]
mf = json.load(open(mf_path, encoding='utf-8'))

# --- 2) DB 레벨 검증 ---
c = sqlite3.connect(p)
ok = c.execute('PRAGMA integrity_check').fetchone()[0]
assert ok == 'ok', f'integrity_check: {ok}'
fk = c.execute('PRAGMA foreign_key_check').fetchall()
assert not fk, f'foreign_key_check 위반 {len(fk)}건'
tables = [r[0] for r in c.execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
uv = c.execute('PRAGMA user_version').fetchone()[0]
print(f'  integrity=ok · fk위반=0 · 테이블 {len(tables)}개 · user_version={uv}')

# --- 3) manifest 대조 ---
assert uv == mf['user_version'], f"user_version {uv} != manifest {mf['user_version']}"
assert len(tables) == mf['tables'], f"테이블 수 {len(tables)} != manifest {mf['tables']}"
diff = []
for t, want in mf['counts'].items():
    got = c.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
    if got != want:
        diff.append(f'{t}: {got} != {want}')
assert not diff, '행수 불일치: ' + ', '.join(diff[:10])
print(f'  manifest 대조 통과 — {len(mf["counts"])}개 테이블 행수 전부 일치 (총 {sum(mf["counts"].values())}행)')
c.close()

# --- 4) 앱 레벨: app.py 가 이 DB 를 읽고 쓰는가 ---
import app as A
A.DATABASE = p
A.app.config['DATABASE'] = p
A.app.config['TESTING'] = True
with A.app.app_context():
    for t in ('users', 'vessels', 'issues', 'dock_procure'):
        n = A.query(f'SELECT COUNT(*) AS c FROM {t}')[0]['c']
        print(f'  {t:<14}{n}행')
    # 쓰기 가능성 확인 — 사본이므로 안전. 흔적은 지운다.
    A.execute('CREATE TABLE _restore_probe (x INTEGER)')
    A.execute('INSERT INTO _restore_probe (x) VALUES (1)')
    assert A.query('SELECT COUNT(*) AS c FROM _restore_probe')[0]['c'] == 1
    A.execute('DROP TABLE _restore_probe')
    print('  쓰기 probe 통과 (사본에 CREATE/INSERT/DROP)')

    # --- 5) 앱 기동 ---
    r = A.app.test_client().get('/login')
    assert r.status_code == 200, f'/login → {r.status_code}'
    print(f'  GET /login → 200 ({len(r.data)} bytes)')

    # --- 6) 첨부 참조 표본이 아카이브 안에 있는가 ---
    if os.path.exists(flist) and os.path.getsize(flist):
        names = set(open(flist, encoding='utf-8', errors='replace').read().split())
        rows = A.query("SELECT stored_name FROM attachments WHERE stored_name IS NOT NULL "
                       "ORDER BY id DESC LIMIT 20")
        if rows:
            hit = sum(1 for r0 in rows
                      if any(os.path.basename(n) == os.path.basename(r0['stored_name']) for n in names))
            print(f'  첨부 표본 {len(rows)}건 중 아카이브에 존재 {hit}건'
                  + ('' if hit else '  ⚠️ 0건 — 첨부 경로 규칙 확인 필요'))
print('✅ 복구 리허설 통과 — 이 백업으로 앱이 기동되고, 데이터가 백업 시점과 행수까지 일치함')
PYEOF
