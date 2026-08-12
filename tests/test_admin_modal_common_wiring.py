import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "static" / "js" / "app.js"


class AdminModalCommonWiringContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = APP_JS.read_text(encoding="utf-8")

    def function_body(self, name, next_marker):
        match = re.search(
            rf"function {re.escape(name)}\([^)]*\) \{{(?P<body>.*?)\n\}}\n\n{next_marker}",
            self.source,
            re.S,
        )
        self.assertIsNotNone(match, f"{name} body not found")
        return match.group("body")

    def test_editor_save_handlers_are_wired_in_common_bootstrap(self):
        body = self.function_body("wireCommon", r"// \u2500+ Init")
        for button_id, handler in (
            ("btn-vedit-save", "saveVesselEdit"),
            ("btn-sedit-save", "saveSupervisorEdit"),
            ("btn-uedit-save", "saveUserEdit"),
        ):
            self.assertIn(
                f"$('#{button_id}').addEventListener('click', {handler})",
                body,
            )

    def test_editor_save_handlers_are_not_daily_only(self):
        body = self.function_body("wireEvents", r"// \u2500+ \uACF5용 와이어링")
        self.assertNotIn("btn-vedit-save", body)
        self.assertNotIn("btn-sedit-save", body)
        self.assertNotIn("btn-uedit-save", body)

    def test_member_vessel_editor_is_wired_outside_admin_gate(self):
        body = self.function_body("wireCommon", r"// \u2500+ Init")
        self.assertLess(body.index("btn-vedit-save"), body.index("if (adminBtn)"))

    def test_escape_checks_role_optional_modals_safely(self):
        body = self.function_body("wireCommon", r"// \u2500+ Init")
        self.assertIn("const modalIsOpen = (selector)", body)
        for selector in (
            "#vessel-edit-modal",
            "#supervisor-edit-modal",
            "#user-edit-modal",
            "#password-modal",
            "#admin-modal",
        ):
            self.assertIn(f"modalIsOpen('{selector}')", body)

    def test_common_wiring_is_invoked_before_daily_wiring(self):
        self.assertLess(
            self.source.rindex("wireCommon();"),
            self.source.rindex("wireEvents();"),
        )


if __name__ == "__main__":
    unittest.main()
