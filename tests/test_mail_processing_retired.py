from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "app.py").read_text()
TEMPLATES = {
    path.name: path.read_text()
    for path in (ROOT / "templates").glob("*.html")
}
ALL_TEMPLATE_TEXT = "\n".join(TEMPLATES.values())


class MailProcessingRetirementTests(unittest.TestCase):
    def test_mail_processing_routes_are_removed(self):
        for route in (
            "'/mail'", "'/api/mail", "'/api/ext/mail",
            "'/api/wf/pull-now'", "'/api/wf/pull-status'", "'/api/wf/pull-flag'",
            "'/api/ext/wf/pull-done'",
        ):
            self.assertNotIn(route, APP)

    def test_mail_processing_is_not_exposed_in_any_template(self):
        for text in ("mail_page", "메일 처리", "/api/mail", "/api/wf/pull-now", "/api/wf/pull-flag"):
            self.assertNotIn(text, ALL_TEMPLATE_TEXT)

    def test_dashboard_no_longer_queries_mail_card_runtime_queue(self):
        dashboard_context = APP[APP.index("def _dashboard_ctx"):APP.index("@app.route('/api/dashboard/cockpit')")]
        self.assertNotIn("mail_card", dashboard_context)
        self.assertNotIn("mail_active", dashboard_context)


if __name__ == "__main__":
    unittest.main()
