import os
import sqlite3
import tempfile
import unittest

import app as appmod
from source_bundle import shared_ns  # noqa: E402


class AutomationProgressTests(unittest.TestCase):
    """러너 중간보고(automation_run.progress) — 화면에서 '굳음'과 '도는중'을 구분하는 표시 필드.

    지키는 것 3가지:
      · 끝난 run 에는 진행문구가 남지 않는다(남으면 진행중으로 오독)
      · running 아닌 행에는 못 쓴다(끝난 카드가 되살아나지 않음)
      · 구버전 DB(progress 컬럼 없음)도 _auto_migrate 로 살아난다 — 없으면 자동화 탭이 통째로 500
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = appmod.DATABASE
        self.old_cfg_db = appmod.app.config["DATABASE"]
        db = os.path.join(self.tmp.name, "test.db")
        appmod.DATABASE = db
        appmod.app.config["DATABASE"] = db
        with appmod.app.app_context():
            appmod.init_db(drop=False)
            shared_ns._ensure_api_table()
            appmod.execute("INSERT OR REPLACE INTO api_settings(k,v) VALUES('api_key',?)", ("secret",))
        self.client = appmod.app.test_client()
        with self.client.session_transaction() as sess:
            sess["user_id"] = 1
            sess["role"] = "admin"

    def tearDown(self):
        appmod.DATABASE = self.old_db
        appmod.app.config["DATABASE"] = self.old_cfg_db
        self.tmp.cleanup()

    def _seed(self, run_id, status):
        with appmod.app.app_context():
            appmod.execute(
                "INSERT INTO automation_run (run_id,task,mode,status,started_at) "
                "VALUES (?,?,?,?,datetime('now','localtime'))",
                (run_id, "jeonja", "live", status))

    def _post(self, run_id, text):
        return self.client.post(f"/api/ext/automation/{run_id}/progress",
                                json={"progress": text}, headers={"X-API-Key": "secret"})

    def _row(self, run_id):
        with appmod.app.app_context():
            return appmod.query("SELECT status,progress FROM automation_run WHERE run_id=?",
                                (run_id,), one=True)

    def test_progress_written_and_exposed(self):
        self._seed("r-run", "running")
        self.assertEqual(200, self._post("r-run", "준비 3/12 판독중").status_code)
        self.assertEqual("준비 3/12 판독중", self._row("r-run")["progress"])
        runs = self.client.get("/api/automation/runs").get_json()["runs"]
        self.assertEqual("준비 3/12 판독중", runs[0]["progress"])

    def test_progress_rejected_when_not_running(self):
        self._seed("r-done", "done")
        self.assertEqual(200, self._post("r-done", "이러면 안 됨").status_code)
        self.assertIsNone(self._row("r-done")["progress"])

    def test_done_clears_progress(self):
        self._seed("r-fin", "running")
        self._post("r-fin", "상신 2건째")
        self.client.post("/api/ext/automation/r-fin/done",
                         json={"summary": "끝", "exit_code": 0}, headers={"X-API-Key": "secret"})
        row = self._row("r-fin")
        self.assertEqual("done", row["status"])
        self.assertIsNone(row["progress"])

    def test_control_chars_stripped(self):
        self._seed("r-inj", "running")
        self._post("r-inj", "준비\n1/2\r판독")
        self.assertEqual("준비 1/2 판독", self._row("r-inj")["progress"])

    def test_auto_migrate_adds_column_to_legacy_db(self):
        """progress 컬럼 없는 기존 DB — _auto_migrate 가 못 붙이면 /api/automation/runs 가 500."""
        conn = sqlite3.connect(appmod.DATABASE)
        try:
            conn.execute("ALTER TABLE automation_run DROP COLUMN progress")
            conn.commit()
        finally:
            conn.close()
        appmod._auto_migrate()
        cols = {r["name"] for r in self._pragma()}
        self.assertIn("progress", cols)
        self.assertEqual(200, self.client.get("/api/automation/runs").status_code)

    def _pragma(self):
        with appmod.app.app_context():
            return appmod.query("PRAGMA table_info(automation_run)")


if __name__ == "__main__":
    unittest.main()
