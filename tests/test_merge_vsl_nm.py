"""`deploy/oneoff/merge_vsl_nm.py` 계약 — 파괴적 일회성 병합이라 fail-closed 를 전건 고정한다.

이 스크립트는 `vsl_nm`(문자열 그룹 키)이 갈라진 선박을 하나로 접는다. 잘못 돌면 여러 탭의 데이터가
엉뚱한 배로 붙거나 사라지므로, **중단해야 할 상황에서 실제로 중단하는지**를 negative control 로 고정한다.
"""
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

import app as appmod

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      'deploy', 'oneoff', 'merge_vsl_nm.py')


class MergeVslNmTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, 'test.db')
        self.old, self.old_g = appmod.app.config['DATABASE'], appmod.DATABASE
        appmod.DATABASE = self.db; appmod.app.config['DATABASE'] = self.db
        with appmod.app.app_context():
            appmod.init_db(drop=False)
        # 갈라진 상태를 만든다: 정본('AAA B', Dock 메타 보유) + shim('Aaa B') + 참조 2행
        self.sql("INSERT INTO dock_procure_vessel(vsl_nm,vsl_cd,vtype,survey) VALUES('AAA B','AAAB','VLCC','2ND')")
        self.sql("INSERT INTO dock_procure_vessel(vsl_nm,vsl_cd,origin) VALUES('Aaa B','AAAB','repair')")
        self.sql("INSERT INTO dock_procure(vsl_nm,vsl_cd,req_no,cat_code,subject) VALUES('AAA B','AAAB','R1','R','dock line')")
        self.sql("INSERT INTO dock_procure(vsl_nm,vsl_cd,req_no,cat_code,subject) VALUES('Aaa B','AAAB','RR9','R','repair line')")
        self.sql("INSERT INTO vessels(name,vsl_cd,active) VALUES('Aaa B','AAAB',1)")
        self.sql("INSERT INTO repair_request(vessel_id,vsl_cd,vsl_nm,subject,category,equipment,app_voy,"
                 "app_port_cd,app_dt,cause,inspection,detail,stock,reason_cd,dept_cd) "
                 "VALUES(1,'AAAB','Aaa B','s','M/E','M/E','1','KRPUS','20260815','c','i','d','vendor','P','E')")

    def tearDown(self):
        appmod.app.config['DATABASE'] = self.old; appmod.DATABASE = self.old_g
        self.tmp.cleanup()

    # ---- helpers ----------------------------------------------------------
    def sql(self, q, args=()):
        with sqlite3.connect(self.db) as d:
            d.execute(q, args)

    def count(self, name):
        with sqlite3.connect(self.db) as d:
            return (d.execute("SELECT COUNT(*) FROM dock_procure WHERE vsl_nm=?", (name,)).fetchone()[0]
                    + d.execute("SELECT COUNT(*) FROM repair_request WHERE vsl_nm=?", (name,)).fetchone()[0]
                    + d.execute("SELECT COUNT(*) FROM dock_procure_vessel WHERE vsl_nm=?", (name,)).fetchone()[0])

    def merge(self, *extra, rows=2, src='Aaa B', dst='AAA B'):
        return subprocess.run([sys.executable, SCRIPT, '--db', self.db, '--from', src, '--to', dst,
                               '--expect-rows', str(rows), *extra],
                              capture_output=True, text=True)

    # ---- positive --------------------------------------------------------
    def test_dry_run_changes_nothing_then_apply_merges(self):
        dry = self.merge()
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertEqual(self.count('Aaa B'), 3)                  # dry-run 은 무변경

        got = self.merge('--apply')
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(self.count('Aaa B'), 0)                  # 잔여 참조 0
        with sqlite3.connect(self.db) as d:
            d.row_factory = sqlite3.Row
            self.assertEqual(d.execute("SELECT COUNT(*) FROM dock_procure_vessel WHERE vsl_nm=?",
                                       ('AAA B',)).fetchone()[0], 1)
            self.assertEqual(d.execute("SELECT COUNT(*) FROM dock_procure WHERE vsl_nm=?",
                                       ('AAA B',)).fetchone()[0], 2)
            keep = d.execute("SELECT vtype,survey FROM dock_procure_vessel WHERE vsl_nm=?",
                             ('AAA B',)).fetchone()
            self.assertEqual((keep['vtype'], keep['survey']), ('VLCC', '2ND'))   # 정본 메타 보존
        self.assertTrue([f for f in os.listdir(self.tmp.name) if '.premerge-' in f])  # 백업 남김

    # ---- negative controls ----------------------------------------------
    def test_unique_conflict_aborts_without_changing_anything(self):
        """같은 `req_no` 가 정본에도 있으면 UNIQUE(vsl_nm,req_no) 충돌 — 손대지 않고 중단."""
        self.sql("INSERT INTO dock_procure(vsl_nm,vsl_cd,req_no,cat_code,subject) "
                 "VALUES('AAA B','AAAB','RR9','R','clash')")
        got = self.merge('--apply')
        self.assertEqual(got.returncode, 2, got.stdout + got.stderr)
        self.assertEqual(self.count('Aaa B'), 3)

    def test_row_count_drift_aborts(self):
        """예상 행수와 실측이 다르면 중단 — 사고 시점 이후 데이터가 늘어난 채로 돌리는 걸 막는다."""
        got = self.merge('--apply', rows=99)
        self.assertNotEqual(got.returncode, 0)
        self.assertEqual(self.count('Aaa B'), 3)

    def test_unknown_vsl_nm_table_aborts(self):
        """스크립트가 모르는 `vsl_nm` 참조 테이블이 생기면 중단(fail-open 금지)."""
        self.sql("CREATE TABLE later_feature(id INTEGER PRIMARY KEY, vsl_nm TEXT)")
        got = self.merge('--apply')
        self.assertNotEqual(got.returncode, 0)
        self.assertIn('later_feature', got.stderr)
        self.assertEqual(self.count('Aaa B'), 3)

    def test_trigger_on_target_table_aborts(self):
        """trigger 가 있으면 부수효과를 예측할 수 없어 중단."""
        self.sql("CREATE TRIGGER t_dp AFTER UPDATE ON dock_procure BEGIN SELECT 1; END")
        got = self.merge('--apply')
        self.assertNotEqual(got.returncode, 0)
        self.assertIn('trigger', got.stderr)
        self.assertEqual(self.count('Aaa B'), 3)

    def test_meta_clash_requires_explicit_override(self):
        """양쪽 메타가 서로 다르면 기본 중단 — 명시 플래그로만 강행."""
        self.sql("UPDATE dock_procure_vessel SET survey='3RD' WHERE vsl_nm='Aaa B'")
        blocked = self.merge('--apply')
        self.assertNotEqual(blocked.returncode, 0)
        self.assertEqual(self.count('Aaa B'), 3)

        forced = self.merge('--apply', '--allow-meta-loss')
        self.assertEqual(forced.returncode, 0, forced.stderr)
        self.assertEqual(self.count('Aaa B'), 0)

    def test_missing_canonical_row_refuses_to_rename(self):
        """정본 표기가 없으면 개명이 되어 버린다 — 이 스크립트는 병합만 하므로 거부."""
        got = self.merge('--apply', dst='NO SUCH VESSEL')
        self.assertNotEqual(got.returncode, 0)
        self.assertEqual(self.count('Aaa B'), 3)

    def test_different_vessel_code_aborts(self):
        """`vsl_cd` 가 다르면 다른 배일 수 있어 중단."""
        self.sql("UPDATE dock_procure_vessel SET vsl_cd='ZZZZ' WHERE vsl_nm='Aaa B'")
        got = self.merge('--apply')
        self.assertNotEqual(got.returncode, 0)
        self.assertEqual(self.count('Aaa B'), 3)


if __name__ == '__main__':
    unittest.main()
