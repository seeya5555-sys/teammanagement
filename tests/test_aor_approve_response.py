"""AOR 승인 응답이 빈 502/비-JSON이어도 안전하게 상태를 재조회하는 UI 계약."""
from pathlib import Path
import re
import unittest


TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / "aor.html"


class AORApproveResponseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = TEMPLATE.read_text(encoding="utf-8")
        match = re.search(
            r"async function approve\(el, d\) \{(?P<body>.*?)\n  \}\n\n  async function reject",
            cls.source,
            re.S,
        )
        if not match:
            raise AssertionError("approve() 함수 경계를 찾지 못함")
        cls.approve = match.group("body")

    def test_does_not_force_json_on_empty_gateway_response(self):
        self.assertIn("const raw = await r.text()", self.approve)
        self.assertNotIn("await r.json()", self.approve)
        self.assertIn("if (raw)", self.approve)
        self.assertIn("JSON.parse(raw)", self.approve)

    def test_non_json_and_json_array_cannot_reach_success_path(self):
        self.assertIn("let parsed = false", self.approve)
        self.assertIn("parsed = !!j && typeof j === 'object' && !Array.isArray(j)", self.approve)
        self.assertIn("if (!parsed)", self.approve)

    def test_http_error_reconciles_card_state_without_post_retry(self):
        error_branch = self.approve.split("if (!r.ok)", 1)[1].split("if (!parsed)", 1)[0]
        self.assertIn("HTTP ${r.status}", error_branch)
        self.assertIn("await load()", error_branch)
        self.assertNotIn("fetch(", error_branch)
        self.assertLess(error_branch.index("await load()"), error_branch.index("current.textContent"))

    def test_malformed_success_response_also_reconciles(self):
        malformed_branch = self.approve.split("if (!parsed)", 1)[1]
        self.assertIn("await load()", malformed_branch)
        self.assertIn("응답 형식", malformed_branch)


if __name__ == "__main__":
    unittest.main()
