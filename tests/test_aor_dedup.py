import os
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

import app as appmod
from source_bundle import shared_ns


class AORDedupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = appmod.app.config["DATABASE"]
        self.old_database_constant = appmod.DATABASE
        test_db = os.path.join(self.tmp.name, "test.db")
        appmod.DATABASE = test_db
        appmod.app.config["DATABASE"] = test_db
        with appmod.app.app_context():
            appmod.init_db(drop=False)
            shared_ns._ensure_api_table()
            appmod.execute(
                "INSERT OR REPLACE INTO api_settings (k, v) VALUES ('api_key', ?)",
                ("secret",),
            )

    def tearDown(self):
        appmod.app.config["DATABASE"] = self.old_db
        appmod.DATABASE = self.old_database_constant
        self.tmp.cleanup()

    def _post(self, barrier=None):
        if barrier is not None:
            barrier.wait()
        with appmod.app.test_client() as client:
            return client.post(
                "/api/ext/aor/drafts",
                json={
                    "aor_cd": " atgrca2607220003 ",
                    "vsl_cd": "ATGR",
                    "vsl_nm": "ATLANTIC GREEN",
                    "subj": "DD Engine HT Cooler Gaskets",
                    "amt": 1411,
                    "cur_cd": "USD",
                },
                headers={"X-API-Key": "secret"},
            )

    def test_concurrent_ingest_keeps_one_active_row(self):
        workers = 8
        barrier = threading.Barrier(workers)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            responses = list(pool.map(lambda _: self._post(barrier), range(workers)))

        self.assertTrue(all(r.status_code in (200, 201) for r in responses))
        with sqlite3.connect(appmod.app.config["DATABASE"]) as db:
            rows = db.execute(
                "SELECT aor_cd, status FROM aor_draft WHERE aor_cd=?",
                ("ATGRCA2607220003",),
            ).fetchall()
        self.assertEqual([("ATGRCA2607220003", "pending")], rows)

    def test_unique_race_recovery_updates_existing_pending_winner(self):
        first = self._post()
        self.assertEqual(201, first.status_code)

        real_query = shared_ns.query
        skipped_precheck = False

        def force_stale_precheck(sql, params=(), one=False):
            nonlocal skipped_precheck
            if (not skipped_precheck
                    and sql.startswith(
                        "SELECT id, status FROM aor_draft WHERE upper(trim(aor_cd))=?")):
                skipped_precheck = True
                return None
            return real_query(sql, params, one)

        with shared_ns.patch("query", mock.Mock(side_effect=force_stale_precheck)):
            response = self._post()

        self.assertTrue(skipped_precheck)
        self.assertEqual(200, response.status_code)
        self.assertEqual(
            {"status": "pending", "updated": True, "dedup": True},
            {k: response.get_json()[k] for k in ("status", "updated", "dedup")},
        )
        with sqlite3.connect(appmod.app.config["DATABASE"]) as db:
            self.assertEqual(
                1,
                db.execute(
                    "SELECT COUNT(*) FROM aor_draft WHERE aor_cd=? AND status='pending'",
                    ("ATGRCA2607220003",),
                ).fetchone()[0],
            )

    def test_init_migration_collapses_active_duplicates_by_status_priority(self):
        db_path = appmod.app.config["DATABASE"]
        with sqlite3.connect(db_path) as db:
            # 2026-07-27 이전 배포의 raw-컬럼 index 를 재현한다. 지금의 표현식 index
            # (`upper(trim(aor_cd))`)는 이 혼재 자체를 막으므로, 그걸 깔아둔 채로는
            # "부팅 시 legacy 중복을 정리한다"는 이 마이그레이션 경로를 검사할 수 없다.
            db.execute("DROP INDEX IF EXISTS uq_aor_draft_active_cd")
            db.execute(
                "CREATE UNIQUE INDEX uq_aor_draft_active_cd ON aor_draft(aor_cd) "
                "WHERE status IN ('pending','hold','approved','submitting','submitted',"
                "'rejecting','reject_submitting')")
            db.execute(
                "INSERT INTO aor_draft (aor_cd, status) VALUES (?, ?)",
                (" atgrca2607220003 ", "pending"),
            )
            db.execute(
                "INSERT INTO aor_draft (aor_cd, status) VALUES (?, ?)",
                ("ATGRCA2607220003", "hold"),
            )
            db.commit()

        with appmod.app.app_context():
            appmod.init_db(drop=False)

        with sqlite3.connect(db_path) as db:
            rows = db.execute(
                "SELECT aor_cd, status FROM aor_draft WHERE aor_cd=?",
                ("ATGRCA2607220003",),
            ).fetchall()
            indexes = db.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name='uq_aor_draft_active_cd'"
            ).fetchall()
        self.assertEqual(
            {("ATGRCA2607220003", "duplicate"), ("ATGRCA2607220003", "hold")},
            set(rows),
        )
        with sqlite3.connect(db_path) as db:
            active = db.execute(
                "SELECT aor_cd, status FROM aor_draft WHERE aor_cd=? AND status<>'duplicate'",
                ("ATGRCA2607220003",),
            ).fetchall()
            duplicate = db.execute(
                "SELECT status, submit_result FROM aor_draft WHERE aor_cd=? AND status='duplicate'",
                ("ATGRCA2607220003",),
            ).fetchall()
        self.assertEqual([("ATGRCA2607220003", "hold")], active)
        self.assertEqual(1, len(duplicate))
        self.assertIn("자동 중복 정리", duplicate[0][1])
        self.assertEqual([("uq_aor_draft_active_cd",)], indexes)

        # Re-running startup migration must remain harmless.
        with appmod.app.app_context():
            appmod.init_db(drop=False)
        with sqlite3.connect(db_path) as db:
            self.assertEqual(
                2,
                db.execute(
                    "SELECT COUNT(*) FROM aor_draft WHERE aor_cd=?",
                    ("ATGRCA2607220003",),
                ).fetchone()[0],
            )

    def test_submitted_history_allows_new_active_draft(self):
        """SVMS 리젝→수정→재상신 = 같은 aor_cd 의 새 결재대기. 이력행이 막으면 영구 누락.

        2026-07-30 실측 버그(ATGRCA2607220002 / ATLANTIC GREEN) 회귀 가드.
        """
        with sqlite3.connect(appmod.app.config["DATABASE"]) as db:
            db.execute(
                "INSERT INTO aor_draft (aor_cd, status) VALUES (?, ?)",
                ("ATGRCA2607220003", "submitted"),
            )
            db.commit()

        response = self._post()
        self.assertEqual(201, response.status_code, response.get_data(as_text=True))
        self.assertEqual("pending", response.get_json()["status"])
        with sqlite3.connect(appmod.app.config["DATABASE"]) as db:
            statuses = db.execute(
                "SELECT status FROM aor_draft WHERE aor_cd=? ORDER BY id",
                ("ATGRCA2607220003",),
            ).fetchall()
        self.assertEqual([("submitted",), ("pending",)], statuses)

    def test_rejected_history_allows_new_active_draft(self):
        with sqlite3.connect(appmod.app.config["DATABASE"]) as db:
            db.execute(
                "INSERT INTO aor_draft (aor_cd, status) VALUES (?, ?)",
                ("ATGRCA2607220003", "rejected"),
            )
            db.commit()

        response = self._post()
        self.assertEqual(201, response.status_code)
        self.assertEqual("pending", response.get_json()["status"])
        with sqlite3.connect(appmod.app.config["DATABASE"]) as db:
            statuses = db.execute(
                "SELECT status FROM aor_draft WHERE aor_cd=? ORDER BY id",
                ("ATGRCA2607220003",),
            ).fetchall()
        self.assertEqual([("rejected",), ("pending",)], statuses)


if __name__ == "__main__":
    unittest.main()
