from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "app.py").read_text()
SCHEMA = (ROOT / "schema.sql").read_text()
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

    def test_runtime_url_map_has_no_mail_processing_routes(self):
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        import app as appmod

        forbidden_prefixes = (
            "/mail", "/api/mail", "/api/ext/mail",
            "/api/wf/pull", "/api/ext/wf/pull",
        )
        live_rules = [rule.rule for rule in appmod.app.url_map.iter_rules()]
        for rule in live_rules:
            self.assertFalse(rule.startswith(forbidden_prefixes), rule)

    def test_historical_mail_rows_are_retained_for_audit_only(self):
        self.assertIn("CREATE TABLE IF NOT EXISTS mail_card", SCHEMA)
        self.assertIn("historical rows are retained in SQLite for audit only", APP)

    def test_mail_processing_is_not_exposed_in_any_template(self):
        for text in (
            "mail_page", "메일 처리", "/api/mail", "/api/wf/pull-now", "/api/wf/pull-flag",
            "mail_active", "stats.mail_active", "approvals.wf1", "['wf1']",
        ):
            self.assertNotIn(text, ALL_TEMPLATE_TEXT)

    def test_dashboard_no_longer_queries_mail_card_runtime_queue(self):
        dashboard_context = APP[APP.index("def _dashboard_ctx"):APP.index("@app.route('/api/dashboard/cockpit')")]
        self.assertNotIn("mail_card", dashboard_context)
        self.assertNotIn("mail_active", dashboard_context)


if __name__ == "__main__":
    unittest.main()
