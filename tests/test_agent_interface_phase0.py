import json
import tempfile
import unittest
from pathlib import Path

from scripts.trmt_agent_phase0 import (
    REQUIRED_VARIANTS,
    compare_runs,
    deep_link_inventory,
    fixture_baseline,
    load_runs,
    route_manifest,
)


class AgentInterfacePhase0Tests(unittest.TestCase):
    def test_route_manifest_is_stable_and_records_guards(self):
        manifest = route_manifest()
        self.assertGreater(manifest["route_count"], 400)
        ext_issues = [r for r in manifest["routes"] if r["path"] == "/api/ext/issues" and r["methods"] == ["GET"]]
        self.assertEqual(1, len(ext_issues))
        self.assertIn("api_key_required", ext_issues[0]["guards"])
        widget = [r for r in manifest["routes"] if r["path"] == "/api/widget/issues"]
        self.assertEqual(1, len(widget))
        self.assertIn("login_required", widget[0]["guards"])

    def test_deep_links_are_candidates_not_claimed_working(self):
        inventory = deep_link_inventory()
        self.assertGreater(inventory["candidate_count"], 0)
        self.assertIn("require behavioral verification", inventory["note"])

    def test_fixture_baseline_labels_bytes_as_not_tokens(self):
        result = fixture_baseline()
        self.assertEqual("serialized payload bytes; not model tokens", result["measurement_kind"])
        for row in result["measurements"].values():
            self.assertGreater(row["payload_reduction_pct"], 0)

    def test_compare_requires_real_counter_fields_and_three_variants(self):
        rows = self.valid_runs()
        report = compare_runs(rows)
        self.assertTrue(report["decision"]["task_tool"])
        self.assertFalse(report["decision"]["webmcp"])

    def test_jsonl_rejects_missing_token_metrics(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "runs.jsonl"
            path.write_text(json.dumps({"case": "x"}) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "missing fields"):
                load_runs(path)

    def test_compare_rejects_missing_representative_cases(self):
        rows = [row for row in self.valid_runs() if row["case"] == "list_issues"]
        with self.assertRaisesRegex(ValueError, "cases must be exactly"):
            compare_runs(rows)

    def test_compare_rejects_duplicate_iterations(self):
        rows = self.valid_runs()
        rows[1]["iteration"] = rows[0]["iteration"]
        with self.assertRaisesRegex(ValueError, "duplicate iteration"):
            compare_runs(rows)

    def test_jsonl_rejects_string_boolean(self):
        row = self.valid_runs()[0]
        row["success"] = "false"
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "runs.jsonl"
            path.write_text(json.dumps(row) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "must be a JSON boolean"):
                load_runs(path)

    @staticmethod
    def valid_runs():
        rows = []
        for case in ("list_issues", "get_issue", "vessel_overview"):
            for variant in REQUIRED_VARIANTS:
                for iteration in range(5):
                    factor = {"current_ui": 100, "task_tool": 50, "webmcp": 80}[variant]
                    rows.append({
                        "case": case, "variant": variant, "iteration": iteration,
                        "input_tokens": factor, "output_tokens": 10, "schema_tokens": 5,
                        "image_tokens": 0, "duration_ms": factor * 10,
                        "success": True, "scope_violation": False, "side_effect": False,
                    })
        return rows


if __name__ == "__main__":
    unittest.main()
