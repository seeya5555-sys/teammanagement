"""초안 생성 실패 AOR 도 카드는 적재되되, 공란 Comment 가 기존 초안을 덮거나 그대로 상신되지 않는 계약."""
import os
import tempfile
import unittest

import app as appmod
from source_bundle import shared_ns


class AORBlankDraftTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = appmod.app.config["DATABASE"]
        self.old_database_constant = appmod.DATABASE
        self.old_csrf = appmod.app.config.get("CSRF_PROTECT")
        db = os.path.join(self.tmp.name, "test.db")
        appmod.DATABASE = db
        appmod.app.config["DATABASE"] = db
        appmod.app.config["CSRF_PROTECT"] = False
        with appmod.app.app_context():
            appmod.init_db(drop=False)
            shared_ns._ensure_api_table()
            appmod.execute("INSERT OR REPLACE INTO api_settings (k, v) VALUES ('api_key', ?)", ("secret",))
            self.uid = appmod.execute(
                "INSERT INTO users(username,password_hash,display_name,role,active) VALUES(?,?,?,?,1)",
                ("aor-admin", appmod.generate_password_hash("pw"), "AOR", "admin"))
        self.client = appmod.app.test_client()

    def tearDown(self):
        appmod.app.config["DATABASE"] = self.old_db
        appmod.DATABASE = self.old_database_constant
        appmod.app.config["CSRF_PROTECT"] = self.old_csrf
        self.tmp.cleanup()

    def _ingest(self, comment, amt=100, conf=0, subj="SERVICE"):
        return self.client.post("/api/ext/aor/drafts", headers={"X-API-Key": "secret"}, json={
            "aor_cd": "SAPSCA2609240001", "vsl_nm": "SOUTH AFRICA PROSPERITY", "subj": subj,
            "amt": amt, "cur_cd": "USD", "proposed_comment": comment, "match_conf": conf,
            "email_subj": "초안 미완성" if not comment else "thread", "approval_app_no": "0003",
            "raw_row": {"AOR_CD": "SAPSCA2609240001"}, "attach_files": ["q.pdf"],
            "outlook_evidence": [{"subject": "RFQ", "quote": "q"}] * 2 if comment else [],
            "outlook_match_keys": ["vessel", "subject"] if comment else [],
        })

    def _row(self):
        with appmod.app.app_context():
            return appmod.query("SELECT * FROM aor_draft WHERE aor_cd='SAPSCA2609240001'", one=True)

    def test_blank_comment_card_is_created(self):
        r = self._ingest("")
        self.assertEqual(201, r.status_code)
        self.assertEqual("pending", self._row()["status"])
        self.assertEqual("", self._row()["proposed_comment"])

    def test_blank_reingest_keeps_existing_comment_when_facts_same(self):
        self._ingest("1. 기존 정상 초안", conf=95)
        before = self._row()
        r = self._ingest("")
        self.assertEqual(200, r.status_code)
        row = self._row()
        self.assertEqual("1. 기존 정상 초안", row["proposed_comment"])
        self.assertEqual(95, row["match_conf"])
        self.assertEqual("thread", row["email_subj"])
        self.assertEqual(before["outlook_evidence"], row["outlook_evidence"])
        self.assertEqual(before["outlook_match_keys"], row["outlook_match_keys"])

    def test_blank_reingest_with_changed_amount_or_subject_blanks_stale_comment(self):
        for kw in ({"amt": 222}, {"subj": "SERVICE REVISED"}):
            self._ingest("1. 옛 금액 초안", conf=95)
            self._ingest("", **kw)
            row = self._row()
            self.assertEqual("", row["proposed_comment"], kw)
            self.assertEqual(0, row["match_conf"])
            for k, v in kw.items():
                self.assertEqual(v, row[k])

    def test_nonblank_reingest_still_replaces_comment(self):
        self._ingest("1. 옛 초안")
        self._ingest("1. 새 초안")
        self.assertEqual("1. 새 초안", self._row()["proposed_comment"])

    def test_approve_rejects_blank_comment(self):
        self._ingest("")
        did = self._row()["id"]
        with self.client.session_transaction() as s:
            s.update(user_id=self.uid, username="aor-admin", display_name="AOR", role="admin", permanent=False)
        for body in ({"proposed_comment": "   "}, {"proposed_comment": None}, {}):
            r = self.client.post(f"/api/aor/drafts/{did}/approve", json=body)
            self.assertEqual(400, r.status_code, body)
            self.assertEqual("proposed_comment", r.get_json()["field"])
            self.assertEqual("pending", self._row()["status"])

    def test_reject_advice_stored_and_cleared(self):
        r = self.client.post("/api/ext/aor/drafts", headers={"X-API-Key": "secret"}, json={
            "aor_cd": "SAPSCA2609240001", "vsl_nm": "SOUTH AFRICA PROSPERITY", "subj": "SERVICE", "amt": 100,
            "cur_cd": "USD", "proposed_comment": "", "raw_row": {"AOR_CD": "SAPSCA2609240001"},
            "reject_suggest": "첨부가 타선박 문서", "reject_draft": "1. Wrong vessel.\n2. Please re-submit."})
        self.assertEqual(201, r.status_code)
        self.assertEqual("첨부가 타선박 문서", self._row()["reject_suggest"])
        self.assertIn("re-submit", self._row()["reject_draft"])
        self._ingest("1. 새 정상 초안")   # 초안 성공 재적재 → 권고 해제
        self.assertIsNone(self._row()["reject_suggest"])
        self.assertIsNone(self._row()["reject_draft"])

    def test_blank_reingest_does_not_attach_advice_over_preserved_comment(self):
        self._ingest("1. 기존 정상 초안", conf=95)
        self.client.post("/api/ext/aor/drafts", headers={"X-API-Key": "secret"}, json={
            "aor_cd": "SAPSCA2609240001", "vsl_nm": "SOUTH AFRICA PROSPERITY", "subj": "SERVICE", "amt": 100,
            "cur_cd": "USD", "proposed_comment": "", "raw_row": {"AOR_CD": "SAPSCA2609240001"},
            "reject_suggest": "첨부 없음", "reject_draft": "1. x\n2. y"})
        self.assertEqual("1. 기존 정상 초안", self._row()["proposed_comment"])
        self.assertIsNone(self._row()["reject_suggest"])

    def test_web_card_prefills_reject_prompt(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1] / "templates" / "aor.html").read_text(encoding="utf-8")
        self.assertIn("d.reject_draft || ''", src)
        self.assertIn("리젝 권고", src)


if __name__ == "__main__":
    unittest.main()
