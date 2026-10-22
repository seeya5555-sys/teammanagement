"""비용청구 월 OPEX 중복 상신 차단 계약 — 러너 contract 구조를 저장·노출하고 문장(why)은 파싱하지 않는다."""
import json
import os
import tempfile
import unittest

import app as appmod
import routes_calendar_dock as rcd


class FundreqDupGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = (appmod.DATABASE, appmod.app.config["DATABASE"], appmod.app.config.get("TESTING"))
        db = os.path.join(self.tmp.name, "fundreq_dup.db")
        appmod.DATABASE = db
        appmod.app.config["DATABASE"] = db
        appmod.app.config["TESTING"] = True
        with appmod.app.app_context():
            appmod.init_db(drop=False)
        self.client = appmod.app.test_client()
        with self.client.session_transaction() as s:
            s.update(user_id=1, username="admin", display_name="Admin", role="admin", supervisor_id=None)
        self.key = "secret"
        with appmod.app.app_context():
            appmod.execute("INSERT OR REPLACE INTO api_settings(k,v) VALUES('api_key',?)", (self.key,))

    def tearDown(self):
        appmod.DATABASE, appmod.app.config["DATABASE"], appmod.app.config["TESTING"] = self.old
        self.tmp.cleanup()

    def ingest(self, opex_cd, **extra):
        payload = {"opex_cd": opex_cd, "vsl_cd": "INPS", "amt": 60295, "cur_cd": "USD", "tp": "O",
                   "verdict": "pass", "why": "계약대조 2026-10: ...", "raw_row": {"OPEX_CD": opex_cd}}
        payload.update(extra)
        return self.client.post("/api/ext/fundreq/drafts", json=payload, headers={"X-API-Key": self.key})

    def drafts(self):
        r = self.client.get("/api/fundreq/drafts")
        self.assertEqual(200, r.status_code)
        return {d["opex_cd"]: d for d in r.get_json()["drafts"]}

    DUP = {"status": "dup", "month": "2026-10", "dept": "T",
           "others": [{"opex_cd": "INPSCO2609010001", "status": "U", "amt": 60295.0, "cur": "USD", "subj": "TECH OPEX OCT 2026"}]}

    def test_dup_card_exposed_and_single_approve_blocked(self):
        self.assertEqual(201, self.ingest("INPSCO2610010001", verdict="flag", dup=self.DUP).status_code)
        d = self.drafts()["INPSCO2610010001"]
        self.assertEqual("dup", d["dup_check"]["status"])
        self.assertEqual("INPSCO2609010001", d["dup_check"]["others"][0]["opex_cd"])
        r = self.client.post(f"/api/fundreq/drafts/{d['id']}/approve")
        self.assertEqual(409, r.status_code)
        self.assertTrue(r.get_json()["dup"])
        self.assertEqual("pending", self.drafts()["INPSCO2610010001"]["status"])
        # 리젝은 허용
        r = self.client.post(f"/api/fundreq/drafts/{d['id']}/reject", json={"reason": "Duplicate submission"})
        self.assertEqual(200, r.status_code)

    def test_bulk_skips_dup_and_approves_clear(self):
        self.ingest("A1", dup=self.DUP)
        self.ingest("B1", dup={"status": "clear", "month": "2026-10", "dept": "T", "others": []})
        self.ingest("C1")   # 대상 밖(dup 없음)
        ds = self.drafts()
        r = self.client.post("/api/fundreq/drafts/approve-bulk", json={"ids": [ds["A1"]["id"], ds["B1"]["id"], ds["C1"]["id"]]})
        j = r.get_json()
        self.assertEqual([ds["A1"]["id"]], j["dup_blocked_ids"])
        self.assertEqual(sorted([ds["B1"]["id"], ds["C1"]["id"]]), sorted(j["approved_ids"]))

    def test_normalizer_fail_closed(self):
        self.assertIsNone(rcd._fundreq_dup_check(None))
        self.assertIsNone(rcd._fundreq_dup_check({"status": "x"}))
        # others 있으면 status 가 clear 로 와도 dup
        out = rcd._fundreq_dup_check({"status": "clear", "others": [{"opex_cd": "X", "amt": float("nan")}]})
        self.assertEqual("dup", out["status"]); self.assertIsNone(out["others"][0]["amt"])

    def test_reingest_pending_updates_dup(self):
        self.ingest("D1")
        self.ingest("D1", dup=self.DUP)
        self.assertEqual("dup", self.drafts()["D1"]["dup_check"]["status"])


if __name__ == "__main__":
    unittest.main()
