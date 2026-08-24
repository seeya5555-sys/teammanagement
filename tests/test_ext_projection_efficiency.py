import unittest
from unittest.mock import patch

import routes_calendar_dock as routes


class ExtProjectionEfficiencyTests(unittest.TestCase):
    def test_surveys_batch_findings_without_changing_shape(self):
        calls = []

        def fake_query(sql, params=(), one=False):
            calls.append((sql, params))
            if 'FROM cs_surveys' in sql:
                return [
                    {'id': 1, 'vessel_name': 'A'},
                    {'id': 2, 'vessel_name': 'B'},
                ]
            if 'FROM cs_findings' in sql:
                return [
                    {'survey_id': 1, 'id': 11, 'category': 'Defect', 'no': '1',
                     'item': 'I', 'description': 'D', 'remark': 'R', 'status': 'Open'},
                    {'survey_id': 1, 'id': 12, 'category': 'Observation', 'no': '1',
                     'item': 'J', 'description': 'E', 'remark': 'S', 'status': 'Open'},
                    {'survey_id': 999, 'id': 99, 'category': 'Defect', 'no': '1',
                     'item': 'orphan', 'description': '', 'remark': '', 'status': 'Open'},
                ]
            self.fail(sql)

        with patch.object(routes, 'query', fake_query):
            result = routes._ext_surveys()

        self.assertEqual(2, len(calls))
        self.assertNotIn('survey_id', result[0]['findings'][0])
        self.assertEqual('cs_finding:11', result[0]['findings'][0]['ref'])
        self.assertEqual([11, 12], [row['id'] for row in result[0]['findings']])
        self.assertEqual([], result[1]['findings'])

    def test_vettings_batch_findings_without_changing_shape(self):
        calls = []

        def fake_query(sql, params=(), one=False):
            calls.append((sql, params))
            if 'FROM vettings' in sql:
                return [{'id': 3, 'vessel_name': 'C'}]
            if 'FROM vt_findings' in sql:
                return [
                    {'vetting_id': 3, 'id': 31, 'no': '1', 'item': 'I',
                     'description': 'D', 'remark': 'R', 'user_remark': 'U',
                     'priority': 'High', 'status': 'Open'},
                    {'vetting_id': 3, 'id': 32, 'no': '2', 'item': 'J',
                     'description': 'E', 'remark': 'S', 'user_remark': 'V',
                     'priority': 'Normal', 'status': 'Closed'},
                ]
            self.fail(sql)

        with patch.object(routes, 'query', fake_query):
            result = routes._ext_vettings()

        self.assertEqual(2, len(calls))
        self.assertNotIn('vetting_id', result[0]['findings'][0])
        self.assertEqual('vt_finding:31', result[0]['findings'][0]['ref'])
        self.assertEqual([31, 32], [row['id'] for row in result[0]['findings']])

    def test_report_tree_fetches_all_blocks_in_one_query(self):
        calls = []

        def fake_query(sql, params=(), one=False):
            calls.append((sql, params))
            if 'FROM dock_report_sections' in sql:
                return [
                    {'id': 10, 'report_id': 7, 'display_order': 0},
                    {'id': 20, 'report_id': 7, 'display_order': 1},
                ]
            if 'FROM dock_report_blocks' in sql:
                self.assertEqual((7,), params)
                self.assertIn('JOIN dock_report_sections', sql)
                self.assertIn('ORDER BY b.section_id, b.display_order, b.id', sql)
                return [
                    {'id': 101, 'section_id': 10, 'display_order': 0,
                     'content_json': '{"text":"A"}'},
                    {'id': 201, 'section_id': 20, 'display_order': 0,
                     'content_json': None},
                ]
            self.fail(sql)

        with patch.object(routes, 'query', fake_query):
            result = routes._report_tree(7, 'dock_report_sections', 'dock_report_blocks')

        self.assertEqual(2, len(calls))
        self.assertEqual({'text': 'A'}, result[0]['blocks'][0]['content'])
        self.assertNotIn('content_json', result[0]['blocks'][0])
        self.assertIsNone(result[1]['blocks'][0]['content'])

    def test_report_tree_with_no_sections_does_not_query_blocks(self):
        calls = []

        def fake_query(sql, params=(), one=False):
            calls.append(sql)
            return []

        with patch.object(routes, 'query', fake_query):
            result = routes._report_tree(8, 'dock_report_sections', 'dock_report_blocks')

        self.assertEqual([], result)
        self.assertEqual(1, len(calls))


if __name__ == '__main__':
    unittest.main()
