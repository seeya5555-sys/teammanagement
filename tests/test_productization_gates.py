"""Productization regression gates: route contract and authenticated page smoke.

These tests use an isolated database and never call POST/PUT/DELETE routes.  The
route snapshot is deliberately explicit: changing an endpoint name, method set,
strict-slash policy, or defaults is a reviewed contract change.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path

import app as appmod


ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "tests" / "fixtures" / "url_map_snapshot.json"


class ProductizationGatesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = appmod.DATABASE
        self.old_cfg = appmod.app.config["DATABASE"]
        db = os.path.join(self.tmp.name, "gate.db")
        appmod.DATABASE = db
        appmod.app.config["DATABASE"] = db
        appmod.app.config["TESTING"] = True
        with appmod.app.app_context():
            appmod.init_db(drop=False)
            vessel = appmod.execute(
                "INSERT INTO vessels(name, short_name, active) VALUES(?,?,1)",
                ("GATE VESSEL", "GATE"),
            )
            appmod.execute(
                "INSERT INTO dock_reports(vessel_id,title) VALUES(?,?)",
                (vessel, "Gate dock report"),
            )
            appmod.execute(
                "INSERT INTO boarding_reports(vessel_id,title) VALUES(?,?)",
                (vessel, "Gate boarding report"),
            )
            appmod.execute(
                "INSERT INTO biz_trips(title,status) VALUES(?,?)",
                ("Gate business trip", "open"),
            )
        self.client = appmod.app.test_client()
        with self.client.session_transaction() as session:
            session.update(
                user_id=1,
                username="gate-admin",
                display_name="Gate Admin",
                role="admin",
                supervisor_id=None,
            )

    def tearDown(self):
        appmod.DATABASE = self.old_db
        appmod.app.config["DATABASE"] = self.old_cfg
        self.tmp.cleanup()

    @staticmethod
    def _rule_contract(rule):
        return {
            "rule": rule.rule,
            "methods": sorted(rule.methods),
            "endpoint": rule.endpoint,
            "strict_slashes": rule.strict_slashes,
            "defaults": rule.defaults or {},
        }

    def test_url_map_contract_snapshot(self):
        actual = [
            self._rule_contract(rule)
            for rule in sorted(appmod.app.url_map.iter_rules(), key=lambda r: (r.rule, r.endpoint))
        ]
        expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        self.assertEqual(expected, actual)

    def test_authenticated_html_get_pages_render(self):
        """Every non-API GET page is exercised; new HTML routes need a fixture here."""
        fixtures = {
            "/dry-dock/<int:rid>/edit": "/dry-dock/1/edit",
            "/boarding/<int:rid>/edit": "/boarding/1/edit",
            "/expenses/<int:tid>": "/expenses/1",
        }
        known_non_html = {
            "/login",  # authenticated users are redirected; smoke anonymously below
            "/logout",  # mutates the session; not a safe smoke GET
            "/krcon/search",  # JSON search endpoint
            "/krcon/view/<doc_id>",  # JSON document endpoint
            "/dock_procure/template/example",  # XLSX download
            "/dock_procure/template/blank",  # XLSX download
        }
        self.assertEqual(200, appmod.app.test_client().get("/login").status_code)
        exercised = set()
        for rule in sorted(appmod.app.url_map.iter_rules(), key=lambda r: (r.rule, r.endpoint)):
            if "GET" not in rule.methods or rule.endpoint == "static" or rule.rule.startswith("/api"):
                continue
            if rule.rule in known_non_html:
                continue
            path = fixtures.get(rule.rule, rule.rule)
            if "<" in path:
                self.fail(f"HTML GET endpoint lacks a safe parameter fixture: {rule.rule}")
            response = self.client.get(path)
            self.assertLess(
                response.status_code,
                500,
                f"GET {path} ({rule.endpoint}) raised {response.status_code}",
            )
            self.assertEqual(
                200,
                response.status_code,
                f"HTML GET {path} ({rule.endpoint}) did not render successfully",
            )
            self.assertIn("text/html", response.content_type, f"GET {path} is not HTML")
            exercised.add(rule.rule)
        expected_pages = {
            rule.rule
            for rule in appmod.app.url_map.iter_rules()
            if "GET" in rule.methods
            and rule.endpoint != "static"
            and not rule.rule.startswith("/api")
            and rule.rule not in known_non_html
        }
        self.assertEqual(expected_pages, exercised)


if __name__ == "__main__":
    unittest.main()
