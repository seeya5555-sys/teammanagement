import os
import tempfile
import unittest
from datetime import date, timedelta

import app as appmod


class CalendarCompletionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = appmod.DATABASE
        self.old_cfg = appmod.app.config['DATABASE']
        db = os.path.join(self.tmp.name, 'test.db')
        appmod.DATABASE = db
        appmod.app.config['DATABASE'] = db
        with appmod.app.app_context():
            appmod.init_db(False)
        self.client = appmod.app.test_client()
        with self.client.session_transaction() as session:
            session['user_id'] = 1
            session['username'] = 'admin'
            session['role'] = 'admin'

    def tearDown(self):
        appmod.DATABASE = self.old_db
        appmod.app.config['DATABASE'] = self.old_cfg
        self.tmp.cleanup()

    def test_completed_round_trip_and_dashboard_mirror(self):
        start = (date.today() + timedelta(days=1)).isoformat()
        created = self.client.post('/api/cal/events', json={
            'title': 'Mirror me', 'start_date': start, 'completed': True,
        })
        self.assertEqual(201, created.status_code)
        eid = created.get_json()['id']

        listed = self.client.get(f'/api/cal/events?start={start}&end={start}')
        self.assertEqual(1, listed.get_json()[0]['completed'])

        cockpit = self.client.get('/api/dashboard/cockpit').get_json()
        mirrored = next(x for x in cockpit['due'] if x['title'] == 'Mirror me')
        self.assertTrue(mirrored['completed'])

        updated = self.client.put(f'/api/cal/events/{eid}', json={'completed': False})
        self.assertEqual(200, updated.status_code)
        row = self.client.get(f'/api/cal/events/{eid}').get_json()
        self.assertEqual(0, row['completed'])


if __name__ == '__main__':
    unittest.main()
