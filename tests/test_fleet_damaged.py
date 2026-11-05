"""피격 선박 표시 + 수동 선위 — 2026-11-05.

피격 선박은 AIS·SVMS 측위가 불량하므로 형이 수동으로 표시·위치를 지정한다.
수동 좌표는 이메일 override·AIS 등 모든 자동 선위보다 우선하고, 피격 해제 시 함께 지워진다.
"""
import json
import os
import tempfile
import unittest

import app as appmod
from source_bundle import shared_ns

NAME = 'BELGIUM B'


class FleetDamagedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = {k: getattr(shared_ns, k) for k in
                    ('FLEET_MAP_FILE', 'FLEET_OVERRIDE_FILE', 'FLEET_EMAIL_WATCH_FILE',
                     'FLEET_AIS_OFF_FILE', 'FLEET_DAMAGED_FILE')}
        self.old_db = (appmod.DATABASE, appmod.app.config['DATABASE'])
        db = os.path.join(self.tmp.name, 'test.db')
        appmod.DATABASE = db
        appmod.app.config['DATABASE'] = db
        appmod.app.config['TESTING'] = True
        for k in self.old:
            setattr(shared_ns, k, os.path.join(self.tmp.name, k.lower() + '.json'))
        with appmod.app.app_context():
            appmod.init_db(False)
            vid = appmod.execute("INSERT INTO vessels(name) VALUES(?)", (NAME,))
            sid = appmod.execute("INSERT INTO supervisors(name) VALUES(?)", ('손유석',))
            appmod.execute("INSERT INTO supervisor_vessels(supervisor_id,vessel_id) VALUES(?,?)", (sid, vid))
        with open(shared_ns.FLEET_MAP_FILE, 'w', encoding='utf-8') as f:
            json.dump({'fleet': [{'name': NAME, 'lat': 25.0, 'lng': 56.0,
                                  'position_source': 'AIS (vesseltracker)'}],
                       'supervisors': [], 'generated_at': '2026-11-05T12:00'}, f)
        self.client = appmod.app.test_client()
        with self.client.session_transaction() as s:
            s['user_id'] = 1
            s['username'] = 'dmg-test'
            s['role'] = 'admin'

    def tearDown(self):
        appmod.DATABASE, appmod.app.config['DATABASE'] = self.old_db
        for k, v in self.old.items():
            setattr(shared_ns, k, v)
        self.tmp.cleanup()

    def post(self, **body):
        return self.client.post('/api/fleet-map/damaged', json={'vessel': NAME, **body})

    def vessel(self):
        r = self.client.get('/api/fleet-map/data')
        self.assertEqual(200, r.status_code)
        return r.get_json()['fleet'][0]

    def test_default_not_damaged(self):
        v = self.vessel()
        self.assertFalse(v['damaged'])
        self.assertEqual(25.0, v['lat'])

    def test_position_requires_damaged(self):
        r = self.post(lat=26.0, lng=56.5)
        self.assertEqual(400, r.status_code)

    def test_mark_damaged_and_manual_position_wins(self):
        self.assertTrue(self.post(damaged=True).get_json()['damaged'])
        v = self.vessel()
        self.assertTrue(v['damaged'])
        self.assertEqual(25.0, v['lat'], '좌표 지정 전엔 자동 선위 유지')
        # 이메일 override 가 있어도 수동 좌표가 최종 우선
        with open(shared_ns.FLEET_OVERRIDE_FILE, 'w', encoding='utf-8') as f:
            json.dump({'belgium b': {'lat': 24.0, 'lng': 55.0, 'source': 'email',
                                     'reported_at': '2026-11-05T10:00'}}, f)
        self.assertEqual(200, self.post(lat=26.12345, lng=56.54321).status_code)
        v = self.vessel()
        self.assertAlmostEqual(26.12345, v['lat'], places=5)
        self.assertAlmostEqual(56.54321, v['lng'], places=5)
        self.assertEqual('manual', v['pos_source'])
        self.assertTrue(v['damaged_manual_pos'])

    def test_clear_pos_keeps_damaged(self):
        self.post(damaged=True)
        self.post(lat=26.0, lng=56.5)
        self.post(clear_pos=True)
        v = self.vessel()
        self.assertTrue(v['damaged'])
        self.assertFalse(v.get('damaged_manual_pos', False))
        self.assertEqual(25.0, v['lat'])

    def test_undamage_removes_position(self):
        self.post(damaged=True)
        self.post(lat=26.0, lng=56.5)
        self.post(damaged=False)
        v = self.vessel()
        self.assertFalse(v['damaged'])
        self.assertEqual(25.0, v['lat'])
        # 다시 피격 표시해도 옛 좌표가 되살아나지 않음
        self.post(damaged=True)
        self.assertEqual(25.0, self.vessel()['lat'])

    def test_hit_date(self):
        self.assertEqual(400, self.post(hit_date='2026-11-03').status_code, '피격 표시 전엔 거부')
        self.post(damaged=True)
        self.assertEqual(400, self.post(hit_date='11/03').status_code)
        self.assertEqual(200, self.post(hit_date='2026-11-03').status_code)
        self.assertEqual('2026-11-03', self.vessel()['damaged_hit_date'])
        self.post(hit_date='')
        self.assertIsNone(self.vessel()['damaged_hit_date'])
        self.post(hit_date='2026-11-03')
        self.post(damaged=False)
        self.post(damaged=True)
        self.assertIsNone(self.vessel()['damaged_hit_date'], '해제 후 재표시 시 옛 피격일 안 살아남')

    def test_rejects_bad_input(self):
        self.post(damaged=True)
        self.assertEqual(400, self.post(lat=91, lng=56).status_code)
        self.assertEqual(400, self.post(lat='x', lng=56).status_code)
        self.assertEqual(400, self.post(lat=float('nan'), lng=56).status_code)
        self.assertEqual(400, self.post(damaged='yes').status_code)
        self.assertEqual(400, self.client.post('/api/fleet-map/damaged', json={}).status_code)


if __name__ == '__main__':
    unittest.main()
