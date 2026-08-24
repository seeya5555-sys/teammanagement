import sqlite3
import unittest
from unittest.mock import patch

import routes_tail


class ClassExportEfficiencyTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE vessels (
                id INTEGER PRIMARY KEY, name TEXT, class_society TEXT,
                manager TEXT, active INTEGER
            );
            CREATE TABLE supervisor_vessels (supervisor_id INTEGER, vessel_id INTEGER);
            CREATE TABLE class_status (
                id INTEGER PRIMARY KEY, vessel_id INTEGER, updated_at TEXT
            );
            CREATE TABLE class_status_items (
                id INTEGER PRIMARY KEY, cs_id INTEGER, category TEXT, no TEXT,
                description TEXT
            );
            INSERT INTO vessels VALUES
                (1, 'Alpha', 'KR', NULL, 1),
                (2, 'Bravo', NULL, ' M2 ', 1),
                (3, 'Inactive', 'DNV', 'M3', 0);
            INSERT INTO supervisor_vessels VALUES (7, 1), (8, 2), (7, 3);
            INSERT INTO class_status VALUES
                (10, 1, '2026-01-01'), (11, 1, '2026-02-01'),
                (20, 2, '2026-02-01'), (30, 3, '2026-03-01');
            INSERT INTO class_status_items VALUES
                (100, 10, 'COC', 'old', 'old snapshot'),
                (101, 11, 'COC', '2', 'latest second'),
                (102, 11, 'COC', '1', 'latest first'),
                (300, 30, 'COC', '1', 'inactive');
        ''')
        self.calls = []

    def tearDown(self):
        self.db.close()

    def query(self, sql, params=(), one=False):
        self.calls.append((sql, tuple(params), one))
        rows = self.db.execute(sql, params).fetchall()
        return (rows[0] if rows else None) if one else rows

    def test_latest_snapshot_is_batched_and_contract_is_preserved(self):
        with patch.object(routes_tail, 'query', self.query):
            result = routes_tail._class_export_vessels()

        self.assertEqual(3, len(self.calls))
        self.assertEqual([1], [v['id'] for v in result])
        self.assertEqual('', result[0]['manager'])
        self.assertEqual(['1', '2'], [item['no'] for item in result[0]['items']])
        self.assertEqual([11, 11], [item['cs_id'] for item in result[0]['items']])
        self.assertNotIn('rn', result[0]['items'][0].keys())
        self.assertFalse(any(item['description'] == 'old snapshot' for item in result[0]['items']))

    def test_supervisor_scope_stays_three_queries_and_excludes_other_vessels(self):
        with patch.object(routes_tail, 'query', self.query):
            result = routes_tail._class_export_vessels(7)

        self.assertEqual(3, len(self.calls))
        self.assertTrue(all(params == (7,) for _, params, _ in self.calls))
        self.assertEqual([1], [v['id'] for v in result])

    def test_empty_vessel_scope_short_circuits_after_one_query(self):
        with patch.object(routes_tail, 'query', self.query):
            result = routes_tail._class_export_vessels(999)

        self.assertEqual([], result)
        self.assertEqual(1, len(self.calls))


if __name__ == '__main__':
    unittest.main()
