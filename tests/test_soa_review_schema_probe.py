#!/usr/bin/env python3
"""반려(R) SOA 스냅샷 ingest — 워커 플래그가 True로 남아 있어도 실측 probe 후 저장돼야 함(2026-11-10 사고 회귀)."""
import os, sys, tempfile
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())
DB = tempfile.mktemp(suffix='.db'); os.environ['TRMT_DB'] = DB
import app as A
A.DATABASE = DB; A.app.config['DATABASE'] = DB; A.app.config['TESTING'] = True
A.init_db(drop=False); A._auto_migrate()
import routes_calendar_dock as R
fails = []
def chk(c, n):
    print(('  ok  ' if c else '  ❌  ') + n)
    if not c: fails.append(n)
A.app.app_context().push()
A.execute("INSERT OR REPLACE INTO api_settings (k,v) VALUES ('api_key','smokekey')")
R.SOA_REVIEW_SCHEMA_DEGRADED = True          # 운영 워커 상태 재현(부팅 probe가 이 프로세스에서 안 돎)
err = ''
try:
    R._soa_review_ingest_snapshot({'sx_cd': 'ATGRCX2608280001', 'header_status': 'R', 'lines': []})
except Exception as e:
    err = str(e)
chk('schema_degraded' not in err, 'R 스냅샷이 schema_degraded로 거부되지 않음 (다음 검증 단계 오류: %s)' % err[:60])
chk(R.SOA_REVIEW_SCHEMA_DEGRADED is False, 'probe 후 플래그 해제')
chk(A.query("SELECT COUNT(*) n FROM soa_review_case WHERE sx_cd='__PROBE__ZZZZZZZZ'", one=True)['n'] == 0, 'probe 행 남지 않음')
A.get_db().execute("CREATE TABLE IF NOT EXISTS _x (a)")   # 연결 정상
print(); print('❌ 실패 %d건' % len(fails) if fails else '✅ 전항 통과'); os.unlink(DB); sys.exit(1 if fails else 0)
