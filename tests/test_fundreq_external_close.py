"""SVMS 외부처리(STATUS≠S) stale pending 카드 종결 계약 — pending 만, 사람 결정은 불변, SVMS 쓰기 없음."""
import os
import tempfile
import unittest

import app as appmod


class FundreqExternalCloseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = (appmod.DATABASE, appmod.app.config["DATABASE"], appmod.app.config.get("TESTING"))
        db = os.path.join(self.tmp.name, "fr_ext.db")
        appmod.DATABASE = db; appmod.app.config["DATABASE"] = db; appmod.app.config["TESTING"] = True
        with appmod.app.app_context():
            appmod.init_db(drop=False)
            appmod.execute("INSERT OR REPLACE INTO api_settings(k,v) VALUES('api_key','secret')")
        self.client = appmod.app.test_client()
        self.h = {"X-API-Key": "secret"}

    def tearDown(self):
        appmod.DATABASE, appmod.app.config["DATABASE"], appmod.app.config["TESTING"] = self.old
        self.tmp.cleanup()

    def draft(self, opex, status="pending"):
        with appmod.app.app_context():
            return appmod.execute("INSERT INTO fundreq_draft(opex_cd,vsl_cd,status,raw_row,verdict) VALUES(?,?,?,'{}','pass')", (opex, opex[:4], status))

    def admin(self):
        with self.client.session_transaction() as s:
            s.update(user_id=1, username="admin", display_name="Admin", role="admin", supervisor_id=None)

    def row(self, did):
        with appmod.app.app_context():
            return appmod.query("SELECT * FROM fundreq_draft WHERE id=?", (did,), one=True)

    def test_pending_list_is_read_only_and_pending_only(self):
        a = self.draft("A", "pending"); self.draft("B", "approved"); self.draft("C", "submitted")
        r = self.client.get("/api/ext/fundreq/drafts/pending", headers=self.h)
        self.assertEqual(200, r.status_code)
        self.assertEqual([{"id": a, "opex_cd": "A", "vsl_cd": "A"}], r.get_json()["drafts"])
        self.assertEqual(401, self.client.get("/api/ext/fundreq/drafts/pending").status_code)

    def test_pending_card_closed_as_submitted_or_rejected(self):
        a = self.draft("BGBBCO2609180002"); b = self.draft("X")
        r = self.client.post(f"/api/ext/fundreq/drafts/{a}/external-status", json={"svms_status": "U", "status": "submitted", "result": "r"}, headers=self.h)
        self.assertEqual((200, True, "submitted"), (r.status_code, r.get_json()["applied"], r.get_json()["status"]))
        self.assertEqual(("submitted", "svms-external", "r"), (self.row(a)["status"], self.row(a)["decided_by"], self.row(a)["result"]))
        r = self.client.post(f"/api/ext/fundreq/drafts/{b}/external-status", json={"svms_status": "r"}, headers=self.h)
        self.assertEqual("rejected", self.row(b)["status"])

    def test_human_decisions_and_inflight_are_never_touched(self):
        for st in ("approved", "rejecting", "submitting", "reject_submitting", "submitted", "failed"):
            did = self.draft("H-" + st, st)
            r = self.client.post(f"/api/ext/fundreq/drafts/{did}/external-status", json={"svms_status": "U"}, headers=self.h)
            self.assertEqual((200, False, st), (r.status_code, r.get_json()["applied"], r.get_json()["status"]), st)
            self.assertEqual(st, self.row(did)["status"])

    def test_invalid_inputs_rejected(self):
        a = self.draft("A")
        for body in ({"svms_status": "S"}, {"svms_status": "Z"}, {}, {"svms_status": "U", "status": "rejected"}):
            r = self.client.post(f"/api/ext/fundreq/drafts/{a}/external-status", json=body, headers=self.h)
            self.assertEqual(400, r.status_code, body)
        self.assertEqual("pending", self.row(a)["status"])
        self.assertEqual(404, self.client.post("/api/ext/fundreq/drafts/9999/external-status", json={"svms_status": "U"}, headers=self.h).status_code)
        self.assertEqual(401, self.client.post(f"/api/ext/fundreq/drafts/{a}/external-status", json={"svms_status": "U"}).status_code)

    def test_race_runner_first_then_human_approve_gets_409(self):
        a = self.draft("BGBBCO2609180002")
        self.client.post(f"/api/ext/fundreq/drafts/{a}/external-status", json={"svms_status": "U"}, headers=self.h)
        self.admin()
        r = self.client.post(f"/api/fundreq/drafts/{a}/approve")
        self.assertEqual(409, r.status_code)
        rb = self.client.post("/api/fundreq/drafts/approve-bulk", json={"ids": [a]})
        self.assertEqual("submitted", self.row(a)["status"])   # bulk 도 pending CAS 라 못 뒤집음
        self.assertIn(rb.status_code, (200, 400, 409))

    def test_race_human_first_then_runner_is_noop(self):
        a = self.draft("BGBBCO2609180002")
        self.admin()
        self.assertEqual(200, self.client.post(f"/api/fundreq/drafts/{a}/approve").status_code)
        r = self.client.post(f"/api/ext/fundreq/drafts/{a}/external-status", json={"svms_status": "U"}, headers=self.h)
        self.assertEqual((False, "approved"), (r.get_json()["applied"], r.get_json()["status"]))
        self.assertEqual(("approved", "admin"), (self.row(a)["status"], self.row(a)["decided_by"]))

    def test_repeat_close_is_noop_and_audit_fields_unchanged(self):
        a = self.draft("X1")
        self.client.post(f"/api/ext/fundreq/drafts/{a}/external-status", json={"svms_status": "U", "result": "first"}, headers=self.h)
        first = dict(self.row(a))
        r = self.client.post(f"/api/ext/fundreq/drafts/{a}/external-status", json={"svms_status": "R", "result": "second"}, headers=self.h)
        self.assertEqual((False, "submitted"), (r.get_json()["applied"], r.get_json()["status"]))
        self.assertEqual(first, dict(self.row(a)))

    def test_same_opex_cd_pending_and_closed_cards_coexist(self):
        old = self.draft("DUPL1", "submitted"); new = self.draft("DUPL1", "pending")
        r = self.client.get("/api/ext/fundreq/drafts/pending", headers=self.h)
        self.assertEqual([new], [d["id"] for d in r.get_json()["drafts"]])
        self.client.post(f"/api/ext/fundreq/drafts/{new}/external-status", json={"svms_status": "R"}, headers=self.h)
        self.assertEqual(("submitted", "rejected"), (self.row(old)["status"], self.row(new)["status"]))


if __name__ == "__main__":
    unittest.main()
