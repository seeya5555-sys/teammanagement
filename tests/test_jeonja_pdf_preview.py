import os
import tempfile
import unittest

import app as appmod


class JeonjaPdfPreviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = appmod.DATABASE
        self.old_cfg_db = appmod.app.config["DATABASE"]
        self.old_pdf_dir = appmod.JEONJA_PDF_DIR
        db = os.path.join(self.tmp.name, "test.db")
        appmod.DATABASE = db
        appmod.app.config["DATABASE"] = db
        appmod.JEONJA_PDF_DIR = os.path.join(self.tmp.name, "jeonja_pdfs")
        os.makedirs(appmod.JEONJA_PDF_DIR, exist_ok=True)
        with appmod.app.app_context():
            appmod.init_db(drop=False)
            appmod._ensure_api_table()
            appmod.execute("INSERT OR REPLACE INTO api_settings(k,v) VALUES('api_key',?)", ("secret",))
        self.client = appmod.app.test_client()
        with self.client.session_transaction() as sess:
            sess["user_id"] = 1
            sess["role"] = "admin"

    def tearDown(self):
        appmod.DATABASE = self.old_db
        appmod.app.config["DATABASE"] = self.old_cfg_db
        appmod.JEONJA_PDF_DIR = self.old_pdf_dir
        self.tmp.cleanup()

    def review(self, bucket="pass"):
        return self.client.post(
            "/api/ext/jeonja/review",
            json={"run_id": "r1", "items": [{"ref": "ATBGCO2607220001", "bucket": bucket, "subj": "test"}]},
            headers={"X-API-Key": "secret"},
        )

    def upload(self):
        return self.client.post(
            "/api/ext/jeonja/review/ATBGCO2607220001/pdf",
            data=b"%PDF-1.4\nreviewed invoice\n%%EOF",
            content_type="application/pdf",
            headers={"X-API-Key": "secret"},
        )

    def test_upload_preview_and_complete_cleanup(self):
        self.assertEqual(200, self.review().status_code)
        self.assertEqual(200, self.upload().status_code)
        items = self.client.get("/api/automation/jeonja/items").get_json()["items"]
        self.assertTrue(items[0]["has_pdf"])
        pdf = self.client.get("/api/automation/jeonja/items/ATBGCO2607220001/pdf")
        self.assertEqual(200, pdf.status_code)
        self.assertEqual("application/pdf", pdf.mimetype)
        pdf.close()

        done = self.client.post(
            "/api/ext/jeonja/review/ATBGCO2607220001/complete",
            json={}, headers={"X-API-Key": "secret"},
        )
        self.assertEqual(200, done.status_code)
        self.assertTrue(done.get_json()["row_deleted"])
        self.assertTrue(done.get_json()["pdf_deleted"])
        self.assertEqual([], self.client.get("/api/automation/jeonja/items").get_json()["items"])
        self.assertEqual(404, self.client.get("/api/automation/jeonja/items/ATBGCO2607220001/pdf").status_code)

    def test_new_review_clears_stale_preview_and_already_rejects_upload(self):
        # SVMS current truth says already: even a previously held row is intentionally removed.
        self.assertEqual(200, self.review(bucket="mismatch").status_code)
        self.assertEqual(200, self.upload().status_code)
        self.assertEqual(200, self.review(bucket="already").status_code)
        self.assertEqual([], self.client.get("/api/automation/jeonja/items").get_json()["items"])
        self.assertEqual(404, self.client.get("/api/automation/jeonja/items/ATBGCO2607220001/pdf").status_code)
        self.assertEqual(404, self.upload().status_code)

    def test_preserves_case_variant_hold_and_fail_closes_invalid_ref(self):
        with appmod.app.app_context():
            appmod.execute("INSERT INTO jeonja_review_item(ref,bucket,excluded) VALUES(?,?,1)",
                           ("atbgco2607220001", "pass"))
        r = self.review()
        self.assertEqual(1, r.get_json()["kept_excluded"])
        item = self.client.get("/api/automation/jeonja/items").get_json()["items"][0]
        self.assertEqual(1, item["excluded"])

        bad = self.client.post(
            "/api/ext/jeonja/review", json={"run_id": "r2", "items": [{"ref": "BAD/REF", "bucket": "already"}]},
            headers={"X-API-Key": "secret"})
        self.assertEqual(1, bad.get_json()["invalid_refs"])
        item = self.client.get("/api/automation/jeonja/items").get_json()["items"][0]
        self.assertEqual("flag", item["bucket"])
        self.assertEqual(1, item["excluded"])

    def test_complete_refuses_held_row(self):
        self.assertEqual(200, self.review(bucket="mismatch").status_code)
        done = self.client.post(
            "/api/ext/jeonja/review/ATBGCO2607220001/complete",
            json={}, headers={"X-API-Key": "secret"})
        self.assertEqual(409, done.status_code)
        self.assertEqual(1, len(self.client.get("/api/automation/jeonja/items").get_json()["items"]))

    def test_rejects_non_pdf_and_unknown_ref(self):
        self.assertEqual(200, self.review().status_code)
        bad = self.client.post(
            "/api/ext/jeonja/review/ATBGCO2607220001/pdf",
            data=b"not-pdf", headers={"X-API-Key": "secret"},
        )
        self.assertEqual(400, bad.status_code)
        missing = self.client.post(
            "/api/ext/jeonja/review/NOPECO2601010001/pdf",
            data=b"%PDF-1.4\n%%EOF", headers={"X-API-Key": "secret"},
        )
        self.assertEqual(404, missing.status_code)


if __name__ == "__main__":
    unittest.main()
