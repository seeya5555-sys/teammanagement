import os
import tempfile
import unittest
from unittest.mock import patch

import app as appmod
import routes_calendar_dock as routes


appmod.app.config['CSRF_PROTECT'] = False


class TripListEfficiencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = appmod.DATABASE
        self.old_cfg = appmod.app.config['DATABASE']
        db = os.path.join(self.tmp.name, 'trips.db')
        appmod.DATABASE = db
        appmod.app.config['DATABASE'] = db
        with appmod.app.app_context():
            appmod.init_db(False)
            appmod.execute(
                "INSERT INTO supervisors (id, name) VALUES (901, 'Performance Test')")
            appmod.execute(
                "INSERT INTO biz_trips (id, supervisor_id, title, status, updated_at) "
                "VALUES (101, 901, 'Older', 'open', '2026-08-01 00:00:00')")
            appmod.execute(
                "INSERT INTO biz_trips (id, supervisor_id, title, status, updated_at) "
                "VALUES (102, 901, 'Newer', 'open', '2026-08-02 00:00:00')")
            appmod.execute(
                "INSERT INTO biz_receipts (trip_id, currency, amount, display_order) "
                "VALUES (101, 'USD', 10, 0)")
            appmod.execute(
                "INSERT INTO biz_receipts (trip_id, currency, amount, display_order) "
                "VALUES (101, 'USD', 15, 1)")
            appmod.execute(
                "INSERT INTO biz_receipts (trip_id, currency, amount, display_order) "
                "VALUES (101, 'KRW', 1000, 2)")
            appmod.execute(
                "INSERT INTO biz_receipts (trip_id, currency, amount, display_order) "
                "VALUES (101, NULL, 5, 3)")
        self.client = appmod.app.test_client()
        with self.client.session_transaction() as session:
            session['user_id'] = 1
            session['username'] = 'admin'
            session['role'] = 'admin'
            session['supervisor_id'] = 1

    def tearDown(self):
        appmod.DATABASE = self.old_db
        appmod.app.config['DATABASE'] = self.old_cfg
        self.tmp.cleanup()

    def test_list_uses_one_query_and_preserves_aggregates_and_order(self):
        calls = []
        original = routes.query

        def counted(sql, params=(), one=False):
            calls.append((sql, tuple(params), one))
            return original(sql, params, one)

        with patch.object(routes, 'query', counted):
            response = self.client.get('/api/biz-trips')

        self.assertEqual(200, response.status_code)
        rows = response.get_json()
        self.assertEqual(1, len(calls))
        self.assertEqual([102, 101], [row['id'] for row in rows])
        self.assertEqual(0, rows[0]['receipt_count'])
        self.assertEqual({}, rows[0]['totals'])
        self.assertEqual(4, rows[1]['receipt_count'])
        self.assertEqual({'?': 5.0, 'KRW': 1000.0, 'USD': 25.0}, rows[1]['totals'])
        for row in rows:
            self.assertNotIn('receipt_currency', row)
            self.assertNotIn('currency_count', row)
            self.assertNotIn('currency_total', row)
        self.assertTrue(all(row['can_edit'] for row in rows))

    def test_empty_list_stays_one_query(self):
        calls = []
        original = routes.query

        def counted(sql, params=(), one=False):
            calls.append(sql)
            return original(sql, params, one)

        with patch.object(routes, 'query', counted):
            response = self.client.get('/api/biz-trips?q=missing')

        self.assertEqual(200, response.status_code)
        self.assertEqual([], response.get_json())
        self.assertEqual(1, len(calls))


if __name__ == '__main__':
    unittest.main()
