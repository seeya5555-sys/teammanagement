import os
import tempfile
import unittest
from datetime import date, timedelta

import app as appmod

# 이 파일은 `client.session_transaction()` 으로 로그인해서 CSRF 토큰을 가진
# 적이 없다. TESTING 을 세우지 않는 파일이라 csrf.enforce 의 기본값(=켜짐)에
# 걸리므로, 여기서 명시적으로 끈다. 검사 자체는 tests/test_csrf.py 가 본다.
appmod.app.config['CSRF_PROTECT'] = False


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

    def test_invalid_input_returns_400_without_mutating_the_event(self):
        bad_scope = self.client.get('/api/cal/events?supervisor_id=nope')
        self.assertEqual(400, bad_scope.status_code)
        self.assertEqual(
            {'error': 'supervisor_id 는 정수 또는 all 이어야 합니다.'},
            bad_scope.get_json(),
        )

        for payload, message in (
            ({'title': 0, 'start_date': '2026-08-25'}, 'title 이 필요합니다.'),
            ({'title': 'Event', 'start_date': ''}, 'start_date 가 필요합니다.'),
        ):
            with self.subTest(payload=payload):
                response = self.client.post('/api/cal/events', json=payload)
                self.assertEqual(400, response.status_code)
                self.assertEqual({'error': message}, response.get_json())

        legacy_color = self.client.post('/api/cal/events', json={
            'title': 'Legacy color default', 'start_date': ' ', 'color': '',
        })
        self.assertEqual(201, legacy_color.status_code)
        legacy_row = self.client.get(
            f"/api/cal/events/{legacy_color.get_json()['id']}"
        ).get_json()
        self.assertEqual('blue', legacy_row['color'])

        created = self.client.post('/api/cal/events', json={
            'title': 'Keep me', 'start_date': '2026-08-25', 'color': 'blue',
        })
        self.assertEqual(201, created.status_code)
        eid = created.get_json()['id']

        for payload, message in (
            ({'title': ''}, 'title 이 필요합니다.'),
            ({'start_date': []}, 'start_date 가 필요합니다.'),
            ({'color': None}, 'color 가 필요합니다.'),
        ):
            with self.subTest(payload=payload):
                response = self.client.put(f'/api/cal/events/{eid}', json=payload)
                self.assertEqual(400, response.status_code)
                self.assertEqual({'error': message}, response.get_json())

        row = self.client.get(f'/api/cal/events/{eid}').get_json()
        self.assertEqual('Keep me', row['title'])
        self.assertEqual('2026-08-25', row['start_date'])
        self.assertEqual('blue', row['color'])


if __name__ == '__main__':
    unittest.main()
