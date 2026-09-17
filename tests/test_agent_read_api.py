import json
import unittest
from unittest.mock import patch

import app as appmod


DERIVED = "d" * 64


class AgentReadAPITests(unittest.TestCase):
    def setUp(self):
        appmod.app.config.update(TESTING=True)
        self.old_secret_key = appmod.app.config.get("SECRET_KEY")
        appmod.app.config["SECRET_KEY"] = b"agent-read-api-test-key"
        self.client = appmod.app.test_client()
        self.key_patch = patch("helpers_shared._agent_read_key", return_value=DERIVED)
        self.key_patch.start()
        self.headers = {"X-TRMT-Agent-Key": DERIVED}

    def tearDown(self):
        self.key_patch.stop()
        appmod.app.config["SECRET_KEY"] = self.old_secret_key

    def test_agent_capability_is_header_only_and_not_master_key(self):
        self.assertEqual(401, self.client.get("/api/agent/status").status_code)
        self.assertEqual(401, self.client.get(
            "/api/agent/status", headers={"X-API-Key": DERIVED}).status_code)
        self.assertEqual(401, self.client.get(
            f"/api/agent/status?key={DERIVED}").status_code)
        result = self.client.get("/api/agent/status", headers=self.headers)
        self.assertEqual(200, result.status_code)
        self.assertEqual(1, result.get_json()["schema_version"])
        self.assertEqual(401, self.client.get(
            "/api/agent/status", headers={"X-TRMT-Agent-Key": "é"}).status_code)
        with patch("helpers_shared._get_api_key", return_value="master-only"):
            self.assertEqual(401, self.client.get(
                "/api/ext/issues", headers={"X-API-Key": DERIVED}).status_code)

    @patch("routes_calendar_dock.query")
    def test_issue_list_is_bounded_filtered_and_cursor_based(self, query):
        query.return_value = [
            {"id": 11, "vessel": "TEST", "item_topic": "A", "priority": "Normal",
             "status": "Open", "issue_date": "2026-09-01", "due_date": None},
            {"id": 12, "vessel": "TEST", "item_topic": "B", "priority": "Urgent",
             "status": "Open", "issue_date": "2026-09-02", "due_date": "2026-09-20"},
        ]
        result = self.client.get(
            "/api/agent/issues?vessel=TEST&status=Open&limit=1&after_id=10",
            headers=self.headers,
        )
        self.assertEqual(200, result.status_code)
        body = result.get_json()
        self.assertEqual([11], [row["id"] for row in body["items"]])
        self.assertEqual(11, body["next_cursor"])
        sql, params = query.call_args.args
        self.assertIn("lower(v.name) = lower(?)", sql)
        self.assertEqual((10, "TEST", "Open", 2), params)
        self.assertEqual(400, self.client.get(
            "/api/agent/issues?limit=101", headers=self.headers).status_code)
        self.assertEqual(400, self.client.get(
            "/api/agent/issues?limit=abc", headers=self.headers).status_code)

    @patch("routes_calendar_dock.query")
    def test_issue_detail_returns_one_and_normalizes_actions(self, query):
        query.return_value = {
            "id": 7, "vessel": "TEST", "item_topic": "Topic", "description": "Detail",
            "priority": "Normal", "status": "Open", "issue_date": "2026-09-01",
            "due_date": None, "actions": "not-json",
        }
        result = self.client.get("/api/agent/issues/7", headers=self.headers)
        self.assertEqual(200, result.status_code)
        self.assertEqual([], result.get_json()["actions"])
        self.assertEqual((7,), query.call_args.args[1])

    @patch("routes_calendar_dock.query")
    def test_vessel_overview_has_bounded_sections(self, query):
        query.side_effect = [
            {"id": 2, "name": "TEST HORIZON"},
            {"open": 3, "urgent": 1, "next_due": "2026-09-20"},
            [{"title": "Synthetic", "start_date": "2026-09-18", "category": "검사"}],
            [{"description": "Synthetic class", "due_date": "2026-10-01", "category": "COC"}],
        ]
        result = self.client.get(
            "/api/agent/vessels/TEST%20HORIZON/overview", headers=self.headers)
        self.assertEqual(200, result.status_code)
        body = result.get_json()
        self.assertEqual("TEST HORIZON", body["vessel"])
        self.assertEqual(3, body["issues"]["open"])
        self.assertEqual(1, len(body["upcoming_events"]))
        self.assertEqual(1, len(body["class_due"]))
        self.assertEqual(4, query.call_count)


if __name__ == "__main__":
    unittest.main()
