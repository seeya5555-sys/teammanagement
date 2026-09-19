"""비용청구 카드 계약 대조 배지 계약 — 러너 contract 구조를 저장·노출하고 문장(why)은 파싱하지 않는다."""
import json
import os
import tempfile
import unittest

import app as appmod
import routes_calendar_dock as rcd


class FundreqContractBadgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = (appmod.DATABASE, appmod.app.config["DATABASE"], appmod.app.config.get("TESTING"))
        db = os.path.join(self.tmp.name, "fundreq_contract.db")
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

    def test_normalizer_only_trusts_runner_structure(self):
        self.assertIsNone(rcd._fundreq_contract_check(None))
        self.assertIsNone(rcd._fundreq_contract_check("not json"))
        self.assertIsNone(rcd._fundreq_contract_check(json.dumps({"status": "weird"})))
        self.assertIsNone(rcd._fundreq_contract_check({"summary": "계약대조 ... 차액 USD 0.00"}))
        # 표시 fail-closed: pass/mismatch 인데 금액 비정상·모순이면 flag 로 강등(잘못된 '계약 일치' 금지)
        out = rcd._fundreq_contract_check({"status": "pass", "expected": 60295, "diff": True, "month": 7, "basis": "DAILY"})
        self.assertEqual({"status": "flag", "expected": 60295.0, "diff": None, "month": None, "basis": "DAILY"}, out)
        for bad in (float("nan"), float("inf"), 10 ** 400, "0.00", None):
            self.assertEqual("flag", rcd._fundreq_contract_check({"status": "pass", "expected": 1.0, "diff": bad})["status"])
            self.assertEqual("flag", rcd._fundreq_contract_check({"status": "mismatch", "expected": bad, "diff": 5.0})["status"])
        self.assertEqual("flag", rcd._fundreq_contract_check({"status": "pass", "expected": 60295.0, "diff": 500.0})["status"])
        self.assertEqual("flag", rcd._fundreq_contract_check({"status": "mismatch", "expected": 60295.0, "diff": 0.0})["status"])
        self.assertEqual("pass", rcd._fundreq_contract_check({"status": "pass", "expected": 60295.0, "diff": -0.01})["status"])
        self.assertEqual("mismatch", rcd._fundreq_contract_check({"status": "mismatch", "expected": 60295.0, "diff": 0.02})["status"])
        self.assertEqual("flag", rcd._fundreq_contract_check({"status": "flag"})["status"])   # 확인필요는 금액 없어도 그대로

    def test_ingest_stores_and_list_exposes_contract_check(self):
        r = self.ingest("INPSCO2609180002", contract={"status": "pass", "expected": 60295.0, "diff": 0.0,
                                                      "month": "2026-10", "basis": "DAILY"})
        self.assertEqual(201, r.status_code)
        self.ingest("SAPSCO2609180001", verdict="flag",
                    contract={"status": "flag", "expected": None, "diff": None, "month": None, "basis": None})
        self.ingest("BGBBCO2609180001", verdict="pass")  # 계약표 대상 밖 → contract 없음
        self.ingest("KWPSCO2609180001", verdict="mismatch", contract={"status": "mismatch", "expected": 60295.0, "diff": 500.0, "month": "2026-10", "basis": "DAILY"})
        ds = self.drafts()
        self.assertEqual({"status": "pass", "expected": 60295.0, "diff": 0.0, "month": "2026-10", "basis": "DAILY"},
                         ds["INPSCO2609180002"]["contract_check"])
        self.assertEqual("flag", ds["SAPSCO2609180001"]["contract_check"]["status"])
        self.assertIsNone(ds["BGBBCO2609180001"]["contract_check"])
        self.assertEqual(500.0, ds["KWPSCO2609180001"]["contract_check"]["diff"])
        # 계약과 DN 판정은 독립: 계약 pass 여도 verdict(DN 불일치)는 그대로 노출돼 두 배지가 같이 보인다.
        self.ingest("GYPSCO2609180001", verdict="mismatch", why="DN(60000.0) ≠ Cost(60295)",
                    contract={"status": "pass", "expected": 60295.0, "diff": 0.0, "month": "2026-10", "basis": "DAILY"})
        g = self.drafts()["GYPSCO2609180001"]
        self.assertEqual(("mismatch", "pass"), (g["verdict"], g["contract_check"]["status"]))
        # 위조/모순 구조는 ingest 단계에서 flag 로 저장된다(목록 응답도 flag).
        self.ingest("CPPSCO2609180001", contract={"status": "pass", "expected": "60295", "diff": 0.0})
        self.assertEqual("flag", self.drafts()["CPPSCO2609180001"]["contract_check"]["status"])

    def test_reingest_updates_and_clears_contract_check(self):
        self.ingest("INPSCO2609180002", contract={"status": "mismatch", "expected": 1.0, "diff": 2.0, "month": "2026-10", "basis": "DAILY"})
        r = self.ingest("INPSCO2609180002", contract={"status": "pass", "expected": 60295.0, "diff": 0.0, "month": "2026-10", "basis": "DAILY"})
        self.assertEqual(200, r.status_code)
        self.assertEqual("pass", self.drafts()["INPSCO2609180002"]["contract_check"]["status"])
        self.ingest("INPSCO2609180002")   # 다음 검토에서 계약 대상 밖이 되면 배지도 사라져야 함
        self.assertIsNone(self.drafts()["INPSCO2609180002"]["contract_check"])

    def test_migration_idempotent_and_decided_cards_untouched(self):
        with appmod.app.app_context():
            appmod.init_db(drop=False)   # 반복 init 에도 contract_check 컬럼 추가가 실패하지 않아야 함
            cols = [r[1] for r in appmod.query("PRAGMA table_info(fundreq_draft)")]
        self.assertEqual(1, cols.count("contract_check"))
        self.ingest("SAPSCO2609180009", contract={"status": "mismatch", "expected": 1.0, "diff": 9.0})
        did = self.drafts()["SAPSCO2609180009"]["id"]
        with appmod.app.app_context():
            appmod.execute("UPDATE fundreq_draft SET status='approved' WHERE id=?", (did,))
        r = self.ingest("SAPSCO2609180009", contract={"status": "pass", "expected": 1.0, "diff": 0.0})
        self.assertEqual(200, r.status_code)
        self.assertTrue(r.get_json().get("dedup"))   # 결정된 카드는 재적재로 덮지 않음
        self.assertEqual("mismatch", self.drafts()["SAPSCO2609180009"]["contract_check"]["status"])


if __name__ == "__main__":
    unittest.main()
