"""Dry Dock Report read-projection extraction contracts."""

import os
import tempfile
import unittest
from unittest.mock import patch

import app as appmod
import dock_report_projection as projection


class DockReportProjectionTests(unittest.TestCase):
    def test_list_filters_shape_and_order_clause_are_preserved(self):
        calls = []

        def fake_query(sql, params=(), one=False):
            calls.append((sql, params, one))
            return [{"id": 9, "title": "Alpha", "vessel_name": "Vessel"}]

        with patch.object(projection, "query", fake_query):
            result = projection.list_reports("true", "4", "draft", "Alpha")

        self.assertEqual([{"id": 9, "title": "Alpha", "vessel_name": "Vessel"}], result)
        sql, params, one = calls[0]
        self.assertIn("d.is_template = ?", sql)
        self.assertIn("d.vessel_id = ?", sql)
        self.assertIn("d.status = ?", sql)
        self.assertIn("d.title LIKE ? OR d.shipyard LIKE ? OR d.dock_no LIKE ?", sql)
        self.assertIn("ORDER BY d.updated_at DESC, d.id DESC", sql)
        self.assertEqual([1, "4", "draft", "%Alpha%", "%Alpha%", "%Alpha%"], params)
        self.assertFalse(one)

    def test_detail_uses_three_queries_and_preserves_nested_golden_shape(self):
        calls = []

        def fake_query(sql, params=(), one=False):
            calls.append((sql, params, one))
            if "FROM dock_reports" in sql:
                return {"id": 7, "title": "Dock", "vessel_short": "V"}
            if "FROM dock_report_sections" in sql and "JOIN" not in sql:
                return [
                    {"id": 20, "report_id": 7, "title": "Second", "display_order": 1},
                    {"id": 10, "report_id": 7, "title": "First", "display_order": 0},
                ]
            if "FROM dock_report_blocks" in sql:
                self.assertIn("JOIN dock_report_sections", sql)
                self.assertIn("WHERE s.report_id = ?", sql)
                self.assertIn("ORDER BY b.section_id, b.display_order, b.id", sql)
                return [
                    {"id": 101, "section_id": 10, "display_order": 0,
                     "content_json": '{"text":"A"}'},
                    {"id": 201, "section_id": 20, "display_order": 0,
                     "content_json": '{"items":["B"]}'},
                ]
            self.fail(sql)

        with patch.object(projection, "query", fake_query):
            result = projection.get_report(7)

        self.assertEqual(3, len(calls))
        self.assertEqual((7,), calls[2][1])
        self.assertEqual({"text": "A"}, result["sections"][1]["blocks"][0]["content"])
        self.assertEqual({"items": ["B"]}, result["sections"][0]["blocks"][0]["content"])
        self.assertNotIn("content_json", result["sections"][0]["blocks"][0])

    def test_missing_and_empty_report_skip_child_queries(self):
        with patch.object(projection, "query", return_value=None) as query_mock:
            self.assertIsNone(projection.get_report(404))
            self.assertEqual(1, query_mock.call_count)

        def empty_query(sql, params=(), one=False):
            if "FROM dock_reports" in sql:
                return {"id": 8, "title": "Empty"}
            return []

        with patch.object(projection, "query", side_effect=empty_query) as query_mock:
            result = projection.get_report(8)
            self.assertEqual([], result["sections"])
            self.assertEqual(2, query_mock.call_count)

    def test_invalid_content_falls_back_to_empty_object_and_reports_error(self):
        errors = []

        def fake_query(sql, params=(), one=False):
            if "FROM dock_reports" in sql:
                return {"id": 3, "vessel_type": "Bulk"}
            if "FROM dock_report_sections" in sql and "JOIN" not in sql:
                return [{"id": 30, "report_id": 3, "display_order": 0}]
            return [{"id": 301, "section_id": 30, "display_order": 0,
                     "content_json": "not-json"}]

        with patch.object(projection, "query", fake_query):
            result = projection.get_export_report(3, errors.append)

        self.assertEqual("Bulk", result["vessel_type"])
        self.assertEqual({}, result["sections"][0]["blocks"][0]["content"])
        self.assertEqual(1, len(errors))


class DockReportProjectionAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = appmod.DATABASE
        self.old_cfg = appmod.app.config["DATABASE"]
        database = os.path.join(self.tmp.name, "dock-report.db")
        appmod.DATABASE = database
        appmod.app.config["DATABASE"] = database
        appmod.app.config["CSRF_PROTECT"] = False
        with appmod.app.app_context():
            appmod.init_db(False)
            vessel_id = appmod.execute(
                "INSERT INTO vessels (name, short_name) VALUES (?, ?)",
                ("Projection Vessel", "PV"),
            )
            self.report_id = appmod.execute(
                "INSERT INTO dock_reports (vessel_id, title, created_by) VALUES (?, ?, ?)",
                (vessel_id, "Projection Report", "admin"),
            )
            section_id = appmod.execute(
                "INSERT INTO dock_report_sections (report_id, title, display_order) "
                "VALUES (?, ?, ?)",
                (self.report_id, "Hull", 0),
            )
            appmod.execute(
                "INSERT INTO dock_report_blocks "
                "(section_id, block_type, content_json, display_order) VALUES (?, ?, ?, ?)",
                (section_id, "paragraph", '{"text":"Checked"}', 0),
            )
        self.client = appmod.app.test_client()
        with self.client.session_transaction() as session:
            session["user_id"] = 1
            session["username"] = "admin"
            session["role"] = "admin"

    def tearDown(self):
        appmod.DATABASE = self.old_db
        appmod.app.config["DATABASE"] = self.old_cfg
        self.tmp.cleanup()

    def test_existing_http_shape_status_and_endpoint_are_preserved(self):
        listed = self.client.get("/api/dock-reports")
        self.assertEqual(200, listed.status_code)
        self.assertEqual("Projection Report", listed.get_json()[0]["title"])
        self.assertTrue(listed.get_json()[0]["can_edit"])

        detail = self.client.get(f"/api/dock-reports/{self.report_id}")
        self.assertEqual(200, detail.status_code)
        body = detail.get_json()
        self.assertEqual("PV", body["vessel_short"])
        self.assertTrue(body["can_edit"])
        self.assertEqual({"text": "Checked"}, body["sections"][0]["blocks"][0]["content"])
        self.assertNotIn("content_json", body["sections"][0]["blocks"][0])
        self.assertEqual(
            "routes_calendar_dock.api_dock_get",
            next(rule.endpoint for rule in appmod.app.url_map.iter_rules()
                 if rule.rule == "/api/dock-reports/<int:rid>" and "GET" in rule.methods),
        )
        self.assertEqual(404, self.client.get("/api/dock-reports/999999").status_code)


if __name__ == "__main__":
    unittest.main(verbosity=2)
