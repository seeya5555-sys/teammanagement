"""Mobile Fleet Map panel contracts."""
from pathlib import Path
import unittest


TEMPLATE = Path(__file__).with_name("templates") / "dashboard.html"


class FleetMapMobileTests(unittest.TestCase):
    def test_mobile_panel_exposes_track_control_as_top_sheet_action(self):
        html = TEMPLATE.read_text()

        self.assertIn("@media(max-width:600px)", html)
        self.assertIn(".fm-panel{top:auto;bottom:0;right:0;width:100%;max-width:none", html)
        self.assertIn(".fm-panel.open{transform:translateY(0)}", html)
        self.assertIn(".fm-track-row{position:sticky", html)
        self.assertIn(".fm-track-toggle{min-height:44px", html)
        self.assertIn('aria-pressed="false"', html)
        self.assertNotIn('font-size:12px;font-weight:600">표시</button>', html)
        self.assertLess(html.index("trackToggle(v)+"), html.index("emailToggle(v)+"))


if __name__ == "__main__":
    unittest.main()
