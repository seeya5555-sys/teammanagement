import unittest
from pathlib import Path


class BaseAssetScopeTests(unittest.TestCase):
    def test_daily_only_dependencies_are_endpoint_scoped(self):
        templates = Path(__file__).parents[1] / "templates"
        base = (templates / "base.html").read_text()
        daily = (templates / "index.html").read_text()
        self.assertIn("{% block page_dependencies %}{% endblock %}", base)
        self.assertNotIn("Sortable.min.js", base)
        self.assertNotIn("daily_vessel_scope.js", base)
        self.assertIn("{% block page_dependencies %}", daily)
        self.assertEqual(daily.count("Sortable.min.js"), 1)
        self.assertEqual(daily.count("daily_vessel_scope.js"), 1)


if __name__ == "__main__":
    unittest.main()
