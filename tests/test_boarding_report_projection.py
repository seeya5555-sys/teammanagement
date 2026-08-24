"""Boarding Report export projection contracts."""

import unittest
from unittest.mock import patch

import boarding_report_projection as projection


class BoardingReportProjectionTests(unittest.TestCase):
    def test_golden_tree_uses_three_queries_and_one_report_binding(self):
        calls = []

        def fake_query(sql, params=(), one=False):
            calls.append((sql, params, one))
            if "FROM boarding_reports" in sql:
                return {"id": 7, "title": "Visit", "vessel_short": "V"}
            if "FROM boarding_report_sections" in sql and "JOIN" not in sql:
                return [
                    {"id": 20, "report_id": 7, "display_order": 1},
                    {"id": 10, "report_id": 7, "display_order": 0},
                ]
            if "FROM boarding_report_blocks" in sql:
                self.assertIn("JOIN boarding_report_sections", sql)
                self.assertIn("WHERE s.report_id = ?", sql)
                self.assertNotIn(" IN (", sql)
                return [
                    {"id": 101, "section_id": 10, "display_order": 0,
                     "content_json": '{"text":"A"}'},
                    {"id": 201, "section_id": 20, "display_order": 0,
                     "content_json": '{"items":["B"]}'},
                ]
            self.fail(sql)

        with patch.object(projection, "query", fake_query):
            result = projection.get_export_report(7)

        self.assertEqual(3, len(calls))
        self.assertEqual((7,), calls[2][1])
        self.assertEqual("Visit", result["title"])
        self.assertEqual({"text": "A"}, result["sections"][1]["blocks"][0]["content"])
        self.assertEqual({"items": ["B"]}, result["sections"][0]["blocks"][0]["content"])
        self.assertNotIn("content_json", result["sections"][0]["blocks"][0])

    def test_missing_empty_and_invalid_content_contracts(self):
        with patch.object(projection, "query", return_value=None) as query_mock:
            self.assertIsNone(projection.get_export_report(404))
            self.assertEqual(1, query_mock.call_count)

        errors = []

        def fake_query(sql, params=(), one=False):
            if "FROM boarding_reports" in sql:
                return {"id": 8, "title": "Bad JSON"}
            if "FROM boarding_report_sections" in sql and "JOIN" not in sql:
                return [{"id": 80, "report_id": 8, "display_order": 0}]
            return [{"id": 801, "section_id": 80, "display_order": 0,
                     "content_json": "not-json"}]

        with patch.object(projection, "query", fake_query):
            result = projection.get_export_report(8, errors.append)

        self.assertEqual({}, result["sections"][0]["blocks"][0]["content"])
        self.assertEqual(1, len(errors))


if __name__ == "__main__":
    unittest.main(verbosity=2)
