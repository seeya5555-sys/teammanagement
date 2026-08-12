import os
import sys
import tempfile
import types
import unittest
from unittest import mock

import app as appmod
import routes_calendar_dock as routes


class _FakeAttachment:
    longFilename = "inspection.pdf"
    data = b"%PDF-1.4\nmail attachment\n%%EOF"


class _FakeMessage:
    subject = "MSG preview contract"
    sender = "sender@example.com"
    to = "receiver@example.com"
    cc = ""
    date = "2026-08-12"
    body = "plain text body"
    attachments = [_FakeAttachment()]

    def close(self):
        pass


class MsgPreviewAllTabsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = appmod.DATABASE
        self.old_cfg = appmod.app.config["DATABASE"]
        self.old_upload = routes.UPLOAD_DIR
        db = os.path.join(self.tmp.name, "msg-preview.db")
        appmod.DATABASE = db
        appmod.app.config["DATABASE"] = db
        routes.UPLOAD_DIR = os.path.join(self.tmp.name, "uploads")
        os.makedirs(routes.UPLOAD_DIR)
        with appmod.app.app_context():
            appmod.init_db(False)
            supervisor = appmod.execute(
                "INSERT INTO supervisors(name,email,active) VALUES(?,?,1)",
                ("Preview Supervisor", "preview@example.com"),
            )
            vessel = appmod.execute(
                "INSERT INTO vessels(name,short_name,active) VALUES(?,?,1)",
                ("PREVIEW VESSEL", "PREVIEW"),
            )
            issue = appmod.execute(
                "INSERT INTO issues(supervisor_id,vessel_id,issue_date,item_topic) VALUES(?,?,?,?)",
                (supervisor, vessel, "2026-08-12", "Preview"),
            )
            survey = appmod.execute(
                "INSERT INTO cs_surveys(vessel_id,year,quarter) VALUES(?,?,?)",
                (vessel, 2026, 3),
            )
            vetting = appmod.execute(
                "INSERT INTO vettings(vessel_id,report_number) VALUES(?,?)",
                (vessel, "PREVIEW"),
            )
            self.ids = {
                "issue": appmod.execute(
                    "INSERT INTO attachments(issue_id,filename,stored_name) VALUES(?,?,?)",
                    (issue, "issue.msg", "issue.msg"),
                ),
                "cs": appmod.execute(
                    "INSERT INTO cs_attachments(survey_id,filename,stored_name) VALUES(?,?,?)",
                    (survey, "survey.msg", "cs.msg"),
                ),
                "vetting": appmod.execute(
                    "INSERT INTO vt_attachments(vetting_id,filename,stored_name) VALUES(?,?,?)",
                    (vetting, "vetting.msg", "vetting.msg"),
                ),
            }
        for source in self.ids:
            with open(os.path.join(routes.UPLOAD_DIR, f"{source}.msg"), "wb") as f:
                f.write(b"fake cfb; parser is isolated below")
        self.client = appmod.app.test_client()
        with self.client.session_transaction() as session:
            session.update(user_id=1, username="preview", role="admin", supervisor_id=None)
        fake_module = types.SimpleNamespace(openMsg=lambda _path: _FakeMessage())
        self.extract_patch = mock.patch.dict(sys.modules, {"extract_msg": fake_module})
        self.extract_patch.start()

    def tearDown(self):
        self.extract_patch.stop()
        routes.UPLOAD_DIR = self.old_upload
        appmod.DATABASE = self.old_db
        appmod.app.config["DATABASE"] = self.old_cfg
        self.tmp.cleanup()

    def test_json_and_inner_pdf_preview_work_for_all_attachment_tabs(self):
        for source, aid in self.ids.items():
            with self.subTest(source=source):
                response = self.client.get(f"/api/msg-preview/{source}/{aid}")
                self.assertEqual(200, response.status_code)
                message = response.get_json()["message"]
                self.assertEqual("MSG preview contract", message["subject"])
                self.assertEqual(["inspection.pdf"], [a["filename"] for a in message["attachments"]])
                inner = self.client.get(f"/api/msg-preview/{source}/{aid}/attachments/0")
                self.assertEqual(200, inner.status_code)
                self.assertEqual("application/pdf", inner.mimetype)
                self.assertEqual(404, self.client.get(
                    f"/api/msg-preview/{source}/{aid}/attachments/99").status_code)

    def test_html_shell_and_legacy_issue_endpoint_remain_available(self):
        self.assertEqual(200, self.client.get("/msg-preview").status_code)
        legacy = self.client.get(f"/api/attachments/{self.ids['issue']}/msg-preview")
        self.assertEqual(200, legacy.status_code)

    def test_unknown_source_and_non_msg_fail_closed(self):
        self.assertEqual(404, self.client.get("/api/msg-preview/nope/1").status_code)
        with appmod.app.app_context():
            appmod.execute("UPDATE cs_attachments SET filename='not-msg.pdf' WHERE id=?", (self.ids["cs"],))
        self.assertEqual(404, self.client.get(f"/api/msg-preview/cs/{self.ids['cs']}").status_code)

    def test_preview_requires_login(self):
        anon = appmod.app.test_client()
        self.assertIn(anon.get(f"/api/msg-preview/vetting/{self.ids['vetting']}").status_code,
                      (302, 401, 403))

    def test_issue_preview_keeps_supervisor_scope(self):
        with self.client.session_transaction() as session:
            session.update(role="supervisor", supervisor_id=999)
        self.assertEqual(403, self.client.get(
            f"/api/msg-preview/issue/{self.ids['issue']}").status_code)


if __name__ == "__main__":
    unittest.main()
