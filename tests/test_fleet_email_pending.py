"""이메일 선위 '대기중'(email_pending) 회귀 — 2026-08-08.

이메일 모드를 켜면 서버는 watch 만 등록하고, Mac 워처(cron 30분)가 메일을 읽어야
override 가 생긴다. 그 사이 화면은 TRMT DB/AIS 를 조용히 보여줘 "이메일로 안 바뀐다"로
오인됐다(실사고). 그래서 override 미도착 구간을 email_pending 으로 표면화한다.

같이 지키는 것: pending 구간은 **실제로 AIS 를 보여주는 중**이므로 AIS 끊김 경고를
억제하면 안 된다(억제하면 낡은 AIS 좌표가 무경고로 나감 — 올마이트 지적).
"""
import json
import os
import tempfile
import time
import unittest

import app as appmod
from source_bundle import shared_ns


class FleetEmailPendingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = {
            'db': appmod.DATABASE,
            'cfg': appmod.app.config['DATABASE'],
            'map': shared_ns.FLEET_MAP_FILE,
            'ovr': shared_ns.FLEET_OVERRIDE_FILE,
            'watch': shared_ns.FLEET_EMAIL_WATCH_FILE,
            'aisoff': shared_ns.FLEET_AIS_OFF_FILE,
        }
        db = os.path.join(self.tmp.name, 'test.db')
        appmod.DATABASE = db
        appmod.app.config['DATABASE'] = db
        appmod.app.config['TESTING'] = True
        shared_ns.FLEET_MAP_FILE = os.path.join(self.tmp.name, 'fleet_map.json')
        shared_ns.FLEET_OVERRIDE_FILE = os.path.join(self.tmp.name, 'ovr.json')
        shared_ns.FLEET_EMAIL_WATCH_FILE = os.path.join(self.tmp.name, 'watch.json')
        shared_ns.FLEET_AIS_OFF_FILE = os.path.join(self.tmp.name, 'aisoff.json')
        with appmod.app.app_context():
            appmod.init_db(False)
            # 대시보드는 supervisor_vessels 배정된 선박만 내려줌 → 픽스처도 배정해야 fleet 에 남음.
            vid = appmod.execute("INSERT INTO vessels(name) VALUES(?)", ('INDONESIA PROSPERITY',))
            sid = appmod.execute("INSERT INTO supervisors(name) VALUES(?)", ('손유석',))
            appmod.execute("INSERT INTO supervisor_vessels(supervisor_id,vessel_id) VALUES(?,?)",
                           (sid, vid))
        self.client = appmod.app.test_client()
        with self.client.session_transaction() as s:
            s['user_id'] = 1
            s['username'] = 'pending-test'
            s['role'] = 'admin'

    def tearDown(self):
        appmod.DATABASE = self.old['db']
        appmod.app.config['DATABASE'] = self.old['cfg']
        shared_ns.FLEET_MAP_FILE = self.old['map']
        shared_ns.FLEET_OVERRIDE_FILE = self.old['ovr']
        shared_ns.FLEET_EMAIL_WATCH_FILE = self.old['watch']
        shared_ns.FLEET_AIS_OFF_FILE = self.old['aisoff']
        self.tmp.cleanup()

    # ── fixtures ──────────────────────────────────────────────
    def write_fleet(self, stale_ais=False):
        ts = time.time() - (shared_ns.AIS_STALE_HOURS + 2) * 3600 if stale_ais else time.time()
        with open(shared_ns.FLEET_MAP_FILE, 'w', encoding='utf-8') as f:
            json.dump({'fleet': [{
                'name': 'INDONESIA PROSPERITY', 'imo': '9999999',
                'lat': 25.0, 'lng': 56.0,
                'position_source': 'AIS (vesseltracker)',
                'position_ts_epoch': ts,
            }], 'supervisors': [], 'generated_at': '2026-08-08T12:00'}, f)

    def write_json(self, path, obj):
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(obj, f)

    def watch_on(self):
        self.write_json(shared_ns.FLEET_EMAIL_WATCH_FILE,
                        {'indonesia prosperity': {'vessel': 'INDONESIA PROSPERITY'}})

    def override_on(self):
        self.write_json(shared_ns.FLEET_OVERRIDE_FILE, {'indonesia prosperity': {
            'vessel': 'INDONESIA PROSPERITY', 'lat': 26.36833, 'lng': 56.27333,
            'course': None, 'speed': None, 'source': 'email',
            'reported_at': '2026-08-08T12:15', 'until': None,
            'stored_at': '2026-08-08T12:52:01'}})

    def vessel(self):
        r = self.client.get('/api/fleet-map/data')
        self.assertEqual(200, r.status_code)
        return r.get_json()['fleet'][0]

    # ── tests ─────────────────────────────────────────────────
    def test_watch_on_without_override_is_pending(self):
        self.write_fleet()
        self.watch_on()
        v = self.vessel()
        self.assertEqual('email', v['pos_mode'], '토글 상태는 override 와 무관하게 email')
        self.assertTrue(v['email_pending'], 'override 미도착 = 대기중으로 표면화돼야 함')
        self.assertNotEqual('email', v.get('pos_source'), '실제 표시원은 아직 이메일이 아님')

    def test_override_present_is_not_pending(self):
        self.write_fleet()
        self.watch_on()
        self.override_on()
        v = self.vessel()
        self.assertEqual('email', v['pos_mode'])
        self.assertFalse(v['email_pending'], 'override 꽂히면 대기중 해제')
        self.assertEqual('email', v['pos_source'])
        self.assertAlmostEqual(26.36833, v['lat'], places=5)

    def test_watch_off_is_never_pending(self):
        self.write_fleet()
        v = self.vessel()
        self.assertEqual('ais', v['pos_mode'])
        self.assertFalse(v['email_pending'])

    def test_pending_keeps_ais_stale_warning(self):
        """대기중엔 낡은 AIS 가 실제로 화면에 나가므로 끊김 경고를 죽이면 안 됨."""
        self.write_fleet(stale_ais=True)
        self.watch_on()
        v = self.vessel()
        self.assertTrue(v['email_pending'])
        self.assertTrue(v['ais_stale'], 'pending 구간의 stale AIS 경고는 살아 있어야 함')

    def test_active_email_suppresses_ais_stale(self):
        """override 가 꽂혀 이메일 좌표를 쓰는 중이면 AIS 끊김은 무의미 — 기존 동작 유지."""
        self.write_fleet(stale_ais=True)
        self.watch_on()
        self.override_on()
        v = self.vessel()
        self.assertFalse(v['email_pending'])
        self.assertFalse(v['ais_stale'])


if __name__ == '__main__':
    unittest.main()
