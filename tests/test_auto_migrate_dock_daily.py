"""옛 DB 에 실제로 마이그레이션이 도는지 -- 라이브 회귀 잠금.

🔴 2026-08-22 라이브 사고: 새 컬럼 점검 블록이 `PRAGMA table_info` 행을
   `r['name']` 으로 읽었다.  이 커넥션에는 row_factory 가 없어 tuple 이 오므로
   `TypeError` 가 나고, 블록을 감싼 `except Exception` 이 그걸 삼켰다.  결과는
   "마이그레이션이 조용히 안 도는" 상태이고, 라이브에서 후보 캐시가
   `no such column: svms_dock_candidates_json` 로 500 이 됐다.

   기존 테스트는 전부 `init_db()` 로 **새 DB** 를 만들어 schema.sql 의 완성형
   테이블을 쓰기 때문에 ALTER 경로를 한 번도 지나지 않았다.  그래서 옛 모양의
   테이블을 직접 만들어 놓고 `_auto_migrate()` 를 돌린다.
"""
import os
import sqlite3
import tempfile
import unittest

import app as appmod


class AutoMigrateDockDailyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR') or None)
        self.db = os.path.join(self.tmp.name, 'old.db')
        self._saved = appmod.DATABASE
        appmod.DATABASE = self.db
        appmod.app.config['DATABASE'] = self.db

    def tearDown(self):
        appmod.DATABASE = self._saved
        appmod.app.config['DATABASE'] = self._saved
        self.tmp.cleanup()

    def _columns(self, table):
        conn = sqlite3.connect(self.db)
        try:
            return {r[1] for r in conn.execute('PRAGMA table_info(%s)' % table).fetchall()}
        finally:
            conn.close()

    def test_auto_migrate_adds_the_svms_columns_to_an_old_db(self):
        conn = sqlite3.connect(self.db)
        # 새 컬럼이 붙기 **전** 모양. schema.sql 을 쓰면 완성형이 나와 ALTER 경로를 안 탄다.
        conn.executescript("""
            CREATE TABLE dock_daily_project(
                id INTEGER PRIMARY KEY AUTOINCREMENT, vessel_id INTEGER, title TEXT,
                svms_dk_cd TEXT, updated_at TEXT);
            CREATE TABLE dock_daily_report(
                id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER,
                report_date TEXT, status TEXT, revision INTEGER DEFAULT 1,
                svms_sync_status TEXT);
        """)
        conn.commit()
        conn.close()
        before = self._columns('dock_daily_project')
        self.assertNotIn('svms_dock_candidates_json', before)
        # 사고의 기제 자체를 못박는다: 이 커넥션은 tuple 행을 준다.
        probe = sqlite3.connect(self.db)
        try:
            with self.assertRaises(TypeError):
                {r['name'] for r in probe.execute(
                    'PRAGMA table_info(dock_daily_project)').fetchall()}
        finally:
            probe.close()

        appmod._auto_migrate()

        project = self._columns('dock_daily_project')
        self.assertIn('svms_dock_candidates_json', project)
        self.assertIn('svms_dock_synced_at', project)
        report = self._columns('dock_daily_report')
        for col in ('svms_claim_token', 'svms_claimed_at', 'svms_approved_by',
                    'svms_approved_revision', 'svms_approved_hash', 'svms_result_json'):
            self.assertIn(col, report, col)

        # 두 번 돌려도 예외 없이 같은 결과(idempotent).
        appmod._auto_migrate()
        self.assertEqual(project, self._columns('dock_daily_project'))


if __name__ == '__main__':
    unittest.main()
