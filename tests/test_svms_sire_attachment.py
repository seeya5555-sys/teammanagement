import hashlib
import io
import os
import tempfile
import unittest

import app as appmod
import ai_gemini as routes


class SvmsSireAttachmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = appmod.DATABASE
        self.old_cfg = appmod.app.config["DATABASE"]
        self.old_upload = routes.UPLOAD_DIR
        db = os.path.join(self.tmp.name, "svms-sire.db")
        appmod.DATABASE = db
        appmod.app.config["DATABASE"] = db
        routes.UPLOAD_DIR = os.path.join(self.tmp.name, "uploads")
        os.makedirs(routes.UPLOAD_DIR)
        with appmod.app.app_context():
            appmod.init_db(False)
            appmod.execute("INSERT OR REPLACE INTO api_settings(k,v) VALUES('api_key',?)", ("test-key",))
            vessel = appmod.execute(
                "INSERT INTO vessels(name,short_name,active) VALUES(?,?,1)",
                ("KUWAIT PROSPERITY", "KUWAIT"),
            )
            self.vetting = appmod.execute(
                "INSERT INTO vettings(vessel_id,report_number) VALUES(?,?)",
                (vessel, "LZXN-1955-2760-7793"),
            )
        self.client = appmod.app.test_client()

    def tearDown(self):
        routes.UPLOAD_DIR = self.old_upload
        appmod.DATABASE = self.old_db
        appmod.app.config["DATABASE"] = self.old_cfg
        self.tmp.cleanup()

    def _post(self, payload=b"%PDF-1.4\nreport\n%%EOF", **overrides):
        fields = {
            "vessel_name": "kuwait-prosperity",
            "report_number": "LZXN195527607793",
            "SIRE_CD": "KWPSSR2606020008",
            "FILE_TP": "SMSC",
            "external_file_id": "KWPSSR2606020008|SMSC|SKSMSMSC2606260003",
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        fields.update(overrides)
        fields["file"] = (io.BytesIO(payload), "close.pdf")
        return self.client.post(
            "/api/ext/vettings/svms-attachment",
            data=fields,
            content_type="multipart/form-data",
            headers={"X-API-Key": "test-key"},
        )

    def test_upload_deduplicates_and_preserves_findings(self):
        first = self._post()
        self.assertEqual(201, first.status_code, first.get_data(as_text=True))
        second = self._post()
        self.assertEqual(200, second.status_code, second.get_data(as_text=True))
        self.assertTrue(second.get_json()["deduplicated"])
        with appmod.app.app_context():
            rows = appmod.query("SELECT * FROM vt_attachments WHERE vetting_id=?", (self.vetting,))
            self.assertEqual(1, len(rows))
            self.assertEqual("svms", rows[0]["source"])
            self.assertEqual("close", rows[0]["source_type"])
            self.assertEqual(0, appmod.query("SELECT COUNT(*) n FROM vt_findings", one=True)["n"])
        self.assertEqual(1, len(os.listdir(routes.UPLOAD_DIR)))

    def test_rejects_bad_magic_and_ambiguous_match(self):
        bad = self._post(b"not a pdf")
        self.assertEqual(415, bad.status_code)
        with appmod.app.app_context():
            vessel = appmod.execute(
                "INSERT INTO vessels(name,short_name,active) VALUES(?,?,1)",
                ("KUWAIT-PROSPERITY", "KUWAIT2"),
            )
            appmod.execute(
                "INSERT INTO vettings(vessel_id,report_number) VALUES(?,?)",
                (vessel, "LZXN195527607793"),
            )
        ambiguous = self._post()
        self.assertEqual(409, ambiguous.status_code)
        self.assertIn("ambiguous", ambiguous.get_json()["error"])
        self.assertEqual([], os.listdir(routes.UPLOAD_DIR))

    def test_svms_attachment_cannot_be_deleted(self):
        uploaded = self._post().get_json()
        with self.client.session_transaction() as session:
            session.update(user_id=1, username="admin", role="admin", supervisor_id=None)
        response = self.client.delete(f"/api/vt-attachments/{uploaded['id']}")
        self.assertEqual(403, response.status_code)

    def test_new_hash_supersedes_old_revision_and_list_returns_provenance(self):
        old = self._post().get_json()["id"]
        new = self._post(b"%PDF-1.4\nrevised\n%%EOF").get_json()["id"]
        self.assertNotEqual(old, new)
        with appmod.app.app_context():
            rows = appmod.query(
                "SELECT id,inactive_at FROM vt_attachments WHERE external_file_id=? ORDER BY id",
                ("KWPSSR2606020008|SMSC|SKSMSMSC2606260003",),
            )
            self.assertEqual(2, len(rows))
            self.assertIsNotNone(rows[0]["inactive_at"])
            self.assertIsNone(rows[1]["inactive_at"])
        with self.client.session_transaction() as session:
            session.update(user_id=1, username="admin", role="admin", supervisor_id=None)
        listed = self.client.get(f"/api/vettings/{self.vetting}/attachments")
        self.assertEqual(200, listed.status_code)
        payload = listed.get_json()
        self.assertEqual([new], [row["id"] for row in payload])
        self.assertEqual("svms", payload[0]["source"])
        self.assertEqual("close", payload[0]["source_type"])
        self.assertTrue(payload[0]["synced_at"])

    def test_smsr_is_stored_as_initial(self):
        uploaded = self._post(
            FILE_TP="SMSR",
            external_file_id="KWPSSR2606020008|SMSR|SKSMSMSR2606020004",
        )
        self.assertEqual(201, uploaded.status_code, uploaded.get_data(as_text=True))
        with appmod.app.app_context():
            row = appmod.query("SELECT source_type FROM vt_attachments WHERE id=?",
                               (uploaded.get_json()["id"],), one=True)
            self.assertEqual("initial", row["source_type"])

    def test_api_key_and_exact_match_are_required(self):
        anon = self.client.post("/api/ext/vettings/svms-attachment")
        self.assertEqual(401, anon.status_code)
        missing = self._post(vessel_name="OTHER VESSEL")
        self.assertEqual(409, missing.status_code)


if __name__ == "__main__":
    unittest.main()
