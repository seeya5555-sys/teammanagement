"""Vetting Last Result 검사일 +3개월 수검 필요 워닝 계약."""
from pathlib import Path
import json
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
VT_JS = ROOT.joinpath("static/js/vt.js").read_text(encoding="utf-8")
CSS = ROOT.joinpath("static/css/main.css").read_text(encoding="utf-8")


class VettingReinspectionWarningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        script = f"""
const vm = require('vm');
const context = {{
  window: {{TRMT: {{}}}},
  document: {{addEventListener: () => {{}}, querySelector: () => null}},
  localStorage: {{getItem: () => null, setItem: () => {{}}}},
  console,
  Date,
}};
vm.createContext(context);
vm.runInContext({json.dumps(VT_JS)}, context);
const cases = [
  [{{valid:'Last Result', inspection_date:'2026-05-25'}}, '2026-08-24T12:00:00', false],
  [{{valid:'Last Result', inspection_date:'2026-05-25'}}, '2026-08-25T00:00:00', true],
  [{{valid:'Last Result', inspection_date:'2026-01-31'}}, '2026-04-30T09:00:00', true],
  [{{valid:'Last Result', inspection_date:'2024-11-30'}}, '2025-02-28T09:00:00', true],
  [{{valid:'Next Plan', inspection_date:'2026-01-01'}}, '2026-08-25T09:00:00', false],
  [{{valid:'Last Result', inspection_date:''}}, '2026-08-25T09:00:00', false],
  [{{valid:'Last Result', inspection_date:'2026-02-30'}}, '2026-08-25T09:00:00', false],
];
const out = cases.map(([v, now, expected]) => [context.vtNeedsInspection(v, new Date(now)), expected]);
const latest = context.vtLatestLastResult([
  {{valid:'Last Result', inspection_date:'9999-invalid'}},
  {{id:1, valid:'Last Result', inspection_date:'2026-01-01'}},
  {{id:2, valid:'Next Plan', inspection_date:'2027-01-01'}},
  {{id:3, valid:'Last Result', inspection_date:'2026-07-01'}},
]);
process.stdout.write(JSON.stringify({{out, latestId: latest && latest.id}}));
"""
        completed = subprocess.run(
            ["node", "-e", script], cwd=ROOT, text=True, capture_output=True, check=True
        )
        cls.results = json.loads(completed.stdout)

    def test_calendar_month_boundaries_and_status_gate(self):
        self.assertTrue(all(actual == expected for actual, expected in self.results["out"]), self.results)
        self.assertEqual(3, self.results["latestId"])

    def test_web_summary_wires_warning_badge(self):
        self.assertIn("const latestLastResult = vtLatestLastResult(vts);", VT_JS)
        self.assertIn("if (vtNeedsInspection(latestLastResult))", VT_JS)
        self.assertIn("'수검 필요'", VT_JS)
        self.assertIn(".vt-summary-inspection-due", CSS)


if __name__ == "__main__":
    unittest.main()
