"""비용청구 승인 탭 OPEX 현황표(11-13): ext 적재(API 키·검증) → admin 조회, 표시 전용."""
import os
import tempfile
import unittest

import app as appmod


class FundreqOpexMatrixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = (appmod.DATABASE, appmod.app.config["DATABASE"], appmod.app.config.get("TESTING"))
        db = os.path.join(self.tmp.name, "ox.db")
        appmod.DATABASE = db; appmod.app.config["DATABASE"] = db; appmod.app.config["TESTING"] = True
        with appmod.app.app_context():
            appmod.init_db(drop=False)
            appmod.execute("INSERT OR REPLACE INTO api_settings(k,v) VALUES('api_key','secret')")
        self.client = appmod.app.test_client()
        self.h = {"X-API-Key": "secret"}

    def tearDown(self):
        appmod.DATABASE, appmod.app.config["DATABASE"], appmod.app.config["TESTING"] = self.old
        self.tmp.cleanup()

    def admin(self):
        with self.client.session_transaction() as s:
            s.update(user_id=1, username="admin", display_name="Admin", role="admin", supervisor_id=None)

    def payload(self):
        it = {"cd": "BGBBCO2603010001", "st": "C", "amt": 87916.0, "cur": "USD", "subj": "<b>CREW MAR 26</b>", "inv_dt": "20260220", "evil": 1}
        return {"generated_at": "2026-11-13 19:00", "range": {"from": "2025-07", "to": "2027-12"}, "vessels": [
            {"vsl": "BGBB", "name": "BELGIUM B", "owner": "장금", "C": {"2026-03": [it, dict(it, cd="X2")]},
             "T": {"2026-03": [dict(it, amt=True)]}, "unread": []}]}

    def test_ingest_requires_key_and_validates(self):
        self.assertIn(self.client.post("/api/ext/fundreq/opex-matrix", json=self.payload()).status_code, (401, 403))
        no_range = dict(self.payload()); no_range.pop("range")
        for bad in ({}, no_range, {"vessels": [{"vsl": "bad name!"}]}, {"vessels": [{"vsl": "BGBB", "C": {"2026-13": []}}]},
                    {"vessels": [{"vsl": "BGBB", "C": {"2026-03": "x"}}]}):
            self.assertEqual(400, self.client.post("/api/ext/fundreq/opex-matrix", json=bad, headers=self.h).status_code, bad)

    def test_roundtrip_admin_only_and_sanitized(self):
        r = self.client.post("/api/ext/fundreq/opex-matrix", json=self.payload(), headers=self.h)
        self.assertEqual(200, r.status_code, r.get_json())
        self.assertIn(self.client.get("/api/fundreq/opex-matrix").status_code, (302, 401, 403))
        self.admin()
        d = self.client.get("/api/fundreq/opex-matrix").get_json()
        v = d["vessels"][0]
        self.assertEqual(2, len(v["C"]["2026-03"]))
        self.assertNotIn("evil", v["C"]["2026-03"][0])
        self.assertIsNone(v["T"]["2026-03"][0]["amt"])          # bool 금액은 버림
        self.assertTrue(d["updated_at"]); self.assertEqual("2025-07", d["range"]["from"])
        self.client.post("/api/ext/fundreq/opex-matrix", json={"vessels": [], "range": {"from": "2025-07", "to": "2027-12"}}, headers=self.h)   # 통째 교체
        self.assertEqual([], self.client.get("/api/fundreq/opex-matrix").get_json()["vessels"])

    def test_page_has_opex_button_and_escapes(self):
        self.admin()
        html = self.client.get("/fundreq").get_data(as_text=True)
        self.assertIn('id="fr-opex"', html); self.assertIn("const esc=", html)


if __name__ == "__main__":
    unittest.main()
