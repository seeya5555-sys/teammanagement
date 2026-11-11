import os
import tempfile
import unittest

import app as appmod
from source_bundle import shared_ns

REF = "SKRTCS2611060023"
H = {"X-API-Key": "secret"}


class JeonjaRunnerHoldTests(unittest.TestCase):
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
        self.c = appmod.app.test_client()
        with self.c.session_transaction() as s:
            s["user_id"] = 1
            s["role"] = "admin"

    def tearDown(self):
        appmod.DATABASE = self.old_db
        appmod.app.config["DATABASE"] = self.old_cfg_db
        self.tmp.cleanup()

    def review(self):
        return self.c.post("/api/ext/jeonja/review", headers=H,
                           json={"run_id": "r", "items": [{"ref": REF, "bucket": "costslip"}]})

    def item(self):
        return self.c.get("/api/automation/jeonja/items").get_json()["items"][0]

    def test_hold_set_kept_on_rereview_and_cleared(self):
        self.assertEqual(200, self.review().status_code)
        r = self.c.post("/api/ext/jeonja/runner-holds", headers=H,
                        json={"holds": [{"ref": REF, "why": "PDF해시 중복 — 사람"}], "at": "2026-11-11 13:00 LIVE"})
        self.assertEqual(1, r.get_json()["updated"])
        it = self.item()
        self.assertEqual("PDF해시 중복 — 사람", it["runner_hold"])
        self.assertEqual(0, it["excluded"])           # 표시 전용: 보류 체크 안 바뀜
        self.review()
        self.assertEqual("PDF해시 중복 — 사람", self.item()["runner_hold"])
        self.c.post("/api/ext/jeonja/runner-holds", headers=H, json={"holds": []})
        self.assertIsNone(self.item()["runner_hold"])
        self.assertEqual(0, self.c.get("/api/ext/jeonja/exclusions", headers=H).get_json()["refs"].__len__())

    def test_auth_and_validation(self):
        self.assertNotEqual(200, self.c.post("/api/ext/jeonja/runner-holds", json={"holds": []}).status_code)
        self.assertEqual(400, self.c.post("/api/ext/jeonja/runner-holds", headers=H, json={}).status_code)


if __name__ == "__main__":
    unittest.main()
