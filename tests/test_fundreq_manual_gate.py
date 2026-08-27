import os
import tempfile
import unittest

import app as appmod


class FundreqManualGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = appmod.DATABASE
        self.old_cfg = appmod.app.config["DATABASE"]
        self.old_testing = appmod.app.config.get("TESTING")
        db = os.path.join(self.tmp.name, "fundreq_gate.db")
        appmod.DATABASE = db
        appmod.app.config["DATABASE"] = db
        appmod.app.config["TESTING"] = True
        with appmod.app.app_context():
            appmod.init_db(drop=False)
        self.client = appmod.app.test_client()
        with self.client.session_transaction() as s:
            s.update(user_id=1, username="gate-admin", display_name="Gate Admin",
                     role="admin", supervisor_id=None)

    def tearDown(self):
        appmod.DATABASE = self.old_db
        appmod.app.config["DATABASE"] = self.old_cfg
        appmod.app.config["TESTING"] = self.old_testing
        self.tmp.cleanup()

    def draft(self, status="pending", raw_row="{}"):
        with appmod.app.app_context():
            return appmod.execute(
                "INSERT INTO fundreq_draft(opex_cd,status,raw_row,verdict) VALUES(?,?,?,'pass')",
                (f"FR-{status}-{os.urandom(3).hex()}", status, raw_row),
            )

    def row(self, did):
        with appmod.app.app_context():
            return appmod.query("SELECT * FROM fundreq_draft WHERE id=?", (did,), one=True)

    def test_bulk_approve_only_selected_pending_rows_with_source(self):
        selected = self.draft()
        unselected = self.draft()
        already = self.draft("submitted")
        no_source = self.draft(raw_row="")
        body = self.client.post("/api/fundreq/drafts/approve-bulk", json={
            "ids": [selected, selected, already, no_source, 999999]
        }).get_json()
        self.assertEqual([selected], body["approved_ids"])
        self.assertEqual(1, body["approved"])
        self.assertEqual(3, body["skipped"])
        self.assertEqual("approved", self.row(selected)["status"])
        self.assertEqual("pending", self.row(unselected)["status"])
        self.assertEqual("submitted", self.row(already)["status"])
        self.assertEqual("pending", self.row(no_source)["status"])
        self.assertEqual("gate-admin", self.row(selected)["decided_by"])
        self.assertTrue(self.row(selected)["decided_at"])

    def test_empty_selection_is_noop(self):
        did = self.draft()
        self.assertEqual({"approved": 0, "skipped": 0, "approved_ids": []},
                         self.client.post("/api/fundreq/drafts/approve-bulk", json={"ids": []}).get_json())
        self.assertEqual("pending", self.row(did)["status"])


if __name__ == "__main__":
    unittest.main()
