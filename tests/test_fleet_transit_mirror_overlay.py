"""Fleet Map 메인 선위 = 통항 미러(팀장님 사이트) 최우선 — 응답 레벨 오버레이 회귀."""
import json
import os
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

import app as appmod
import routes_tail
import transit_mirror as tm
from source_bundle import shared_ns

MINE = 'PERU PROSPERITY'
OTHER = 'OTHER SHIP'          # 다른 감독 배정
AIS_LAT, AIS_LNG = 10.0, 20.0
MIR_LAT, MIR_LNG = 26.5, 56.3


class FleetMirrorOverlayTests(unittest.TestCase):
    def setUp(self):
        tm.reset_cache()
        self.tmp = tempfile.TemporaryDirectory()
        self.old = {
            'db': appmod.DATABASE, 'cfg': appmod.app.config['DATABASE'],
            'map': shared_ns.FLEET_MAP_FILE, 'ovr': shared_ns.FLEET_OVERRIDE_FILE,
            'aisoff': shared_ns.FLEET_AIS_OFF_FILE,
            'cache': dict(routes_tail._trmtdb_position_cache),
            'apikey': os.environ.get('TRMTDB_API_KEY'),
            'testing': appmod.app.config.get('TESTING'),
            'secret': appmod.app.config['SECRET_KEY'],
        }
        self.db = os.path.join(self.tmp.name, 'test.db')
        appmod.DATABASE = self.db
        appmod.app.config['DATABASE'] = self.db
        appmod.app.config['TESTING'] = True
        appmod.app.config['SECRET_KEY'] = b'mirror-overlay-test'
        shared_ns.FLEET_MAP_FILE = os.path.join(self.tmp.name, 'fleet_map.json')
        shared_ns.FLEET_OVERRIDE_FILE = os.path.join(self.tmp.name, 'ovr.json')
        shared_ns.FLEET_AIS_OFF_FILE = os.path.join(self.tmp.name, 'aisoff.json')
        os.environ.pop('TRMTDB_API_KEY', None)
        routes_tail._trmtdb_position_cache.update({
            'at': time.monotonic(), 'loaded': True, 'error': None,
            'fetched_at': datetime.utcnow().isoformat(timespec='seconds'), 'vessels': []})
        with appmod.app.app_context():
            appmod.init_db(False)
            s1 = appmod.execute("INSERT INTO supervisors(name) VALUES('감독1')")
            s2 = appmod.execute("INSERT INTO supervisors(name) VALUES('감독2')")
            v1 = appmod.execute("INSERT INTO vessels(name) VALUES(?)", (MINE,))
            v2 = appmod.execute("INSERT INTO vessels(name) VALUES(?)", (OTHER,))
            appmod.execute("INSERT INTO supervisor_vessels(supervisor_id,vessel_id) VALUES(?,?)", (s1, v1))
            appmod.execute("INSERT INTO supervisor_vessels(supervisor_id,vessel_id) VALUES(?,?)", (s2, v2))
            uid = appmod.execute(
                "INSERT INTO users(username,password_hash,supervisor_id,role) VALUES('m1','x',?,'member')", (s1,))
        self.client = appmod.app.test_client()
        with self.client.session_transaction() as s:
            s['user_id'] = uid
            s['role'] = 'member'
            s['supervisor_id'] = s1
        self.now = float(int(time.time()))   # positionTs 정수화로 경계 테스트가 흔들리지 않게

    def tearDown(self):
        tm.reset_cache()
        appmod.DATABASE = self.old['db']
        appmod.app.config['DATABASE'] = self.old['cfg']
        appmod.app.config['TESTING'] = self.old['testing']
        appmod.app.config['SECRET_KEY'] = self.old['secret']
        shared_ns.FLEET_MAP_FILE = self.old['map']
        shared_ns.FLEET_OVERRIDE_FILE = self.old['ovr']
        shared_ns.FLEET_AIS_OFF_FILE = self.old['aisoff']
        routes_tail._trmtdb_position_cache.clear()
        routes_tail._trmtdb_position_cache.update(self.old['cache'])
        if self.old['apikey'] is not None:
            os.environ['TRMTDB_API_KEY'] = self.old['apikey']
        self.tmp.cleanup()

    def write_fleet(self, ais_hours_ago=2):
        items = []
        for name in (MINE, OTHER):
            items.append({'name': name, 'lat': AIS_LAT, 'lng': AIS_LNG, 'rpt_dt': '20261030',
                          'position_source': 'AIS · vesseltracker',
                          'position_ts_epoch': str(int(self.now - ais_hours_ago * 3600))})
        with open(shared_ns.FLEET_MAP_FILE, 'w', encoding='utf-8') as f:
            json.dump({'fleet': items, 'supervisors': [], 'generated_at': None}, f)

    def mirror(self, hours_ago, name=MINE):
        vessels = [{'name': n, 'latitude': MIR_LAT, 'longitude': MIR_LNG, 'positionSource': 'EMAIL',
                    'positionLastUpdate': '10/30 09:00', 'positionTs': int(self.now - hours_ago * 3600),
                    'speed': '11.5', 'category': '통항중'} for n in (name, OTHER)]

        def g(path, timeout=None):
            return {'/api/vessels': vessels, '/api/transit-plan/today': {},
                    '/api/transit-plan/registry': {}}[path]
        return mock.patch.object(tm, '_get_json', side_effect=g)

    def fetch(self):
        r = self.client.get('/api/fleet-map/data')
        self.assertEqual(200, r.status_code)
        return {v['name']: v for v in r.get_json()['fleet']}

    def test_newer_mirror_overrides(self):
        self.write_fleet(ais_hours_ago=5)
        with self.mirror(hours_ago=1):
            f = self.fetch()
        v = f[MINE]
        self.assertEqual((MIR_LAT, MIR_LNG), (v['lat'], v['lng']))
        self.assertEqual('mirror', v['pos_source'])
        self.assertIn('팀장님 사이트(E-mail', v['pos_mirror_label'])
        self.assertEqual(11.5, v['speed'])
        self.assertEqual('20261030', v['rpt_dt'])

    def test_fresh_mirror_wins_even_if_older_than_current(self):
        self.write_fleet(ais_hours_ago=1)
        with self.mirror(hours_ago=48):   # 72h 이내 → 현재(1h 전 AIS)보다 낡아도 미러 우선
            v = self.fetch()[MINE]
        self.assertEqual((MIR_LAT, MIR_LNG), (v['lat'], v['lng']))
        self.assertEqual('mirror', v['pos_source'])

    def test_stale_mirror_newer_than_current_still_wins(self):
        self.write_fleet(ais_hours_ago=24 * 10)
        with self.mirror(hours_ago=24 * 5):
            self.assertEqual(MIR_LAT, self.fetch()[MINE]['lat'])

    def test_ais_off_vessel_also_uses_mirror(self):
        # AIS off(SVMS 고정) 선박도 미러가 권위 — 걸프 AIS off 선박이 바로 미러가 필요한 대상.
        self.write_fleet(ais_hours_ago=5)
        with open(shared_ns.FLEET_AIS_OFF_FILE, 'w', encoding='utf-8') as f:
            json.dump({MINE.lower(): {'since': 'x'}}, f)
        with self.mirror(hours_ago=1):
            self.assertEqual('mirror', self.fetch()[MINE].get('pos_source'))

    def test_older_mirror_keeps_existing(self):
        self.write_fleet(ais_hours_ago=1)
        with self.mirror(hours_ago=24 * 90):   # 몇 달 전 BBC 선박 같은 경우
            v = self.fetch()[MINE]
        self.assertEqual((AIS_LAT, AIS_LNG), (v['lat'], v['lng']))
        self.assertNotEqual('mirror', v.get('pos_source'))

    def test_unknown_current_time_uses_72h_window(self):
        self.write_fleet()
        with open(shared_ns.FLEET_MAP_FILE, encoding='utf-8') as f:
            d = json.load(f)
        for it in d['fleet']:
            it.pop('position_ts_epoch')
        with open(shared_ns.FLEET_MAP_FILE, 'w', encoding='utf-8') as f:
            json.dump(d, f)
        with self.mirror(hours_ago=80):
            self.assertEqual(AIS_LAT, self.fetch()[MINE]['lat'])
        tm.reset_cache()
        with self.mirror(hours_ago=10):
            self.assertEqual(MIR_LAT, self.fetch()[MINE]['lat'])

    def test_upstream_down_unchanged(self):
        self.write_fleet()
        with mock.patch.object(tm, '_get_json', side_effect=OSError('down')):
            v = self.fetch()[MINE]
        self.assertEqual((AIS_LAT, AIS_LNG), (v['lat'], v['lng']))
        with mock.patch.object(tm, 'fleet_positions', side_effect=RuntimeError('boom')):
            self.assertEqual(AIS_LAT, self.fetch()[MINE]['lat'])

    def test_unassigned_vessel_unchanged(self):
        self.write_fleet(ais_hours_ago=5)
        with self.mirror(hours_ago=1):
            out = []
            with appmod.app.test_request_context():
                from flask import session
                with self.client.session_transaction() as s:
                    sid = s['supervisor_id']
                session['supervisor_id'] = sid
                fleet = [{'name': OTHER, 'lat': AIS_LAT, 'lng': AIS_LNG}]
                out, n = tm.apply_fleet_overrides(fleet, lambda v: None)
        self.assertEqual(0, n)
        self.assertEqual(AIS_LAT, out[0]['lat'])

    def test_no_db_write(self):
        self.write_fleet(ais_hours_ago=5)

        def snap():
            con = sqlite3.connect(self.db)
            try:
                return {t: con.execute('SELECT * FROM "%s"' % t).fetchall()
                        for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            finally:
                con.close()
        before = snap()
        with self.mirror(hours_ago=1):
            self.assertEqual(MIR_LAT, self.fetch()[MINE]['lat'])
        self.assertEqual(before, snap())

    # ── reviewer round 2 ─────────────────────────────────────────
    def _direct(self, fleet, cur, sup=True, hours_ago=None, ts=None):
        with appmod.app.test_request_context():
            from flask import session
            with self.client.session_transaction() as s:
                sid = s['supervisor_id']
            if sup:
                session['supervisor_id'] = sid
            return tm.apply_fleet_overrides(fleet, lambda v: cur, now=self.now)

    def _item(self):
        return {'name': MINE, 'lat': AIS_LAT, 'lng': AIS_LNG, 'nested': {'a': [1]}}

    def test_over_72h_tie_not_applied(self):
        ts = self.now - 100 * 3600
        with self.mirror(hours_ago=100):
            out, n = self._direct([self._item()], cur=int(ts))
        self.assertEqual(0, n)
        self.assertEqual(AIS_LAT, out[0]['lat'])
        tm.reset_cache()
        with self.mirror(hours_ago=100):
            out, n = self._direct([self._item()], cur=int(ts) - 1)
        self.assertEqual(1, n)

    def test_exact_72h_boundary_wins(self):
        with self.mirror(hours_ago=72):
            out, n = self._direct([self._item()], cur=self.now)   # 현재가 더 최신이어도
        self.assertEqual(1, n)
        self.assertEqual(MIR_LAT, out[0]['lat'])

    def test_original_dicts_never_mutated(self):
        orig = [self._item()]
        snapshot = json.dumps(orig, sort_keys=True)
        with self.mirror(hours_ago=1):
            out, n = self._direct(orig, cur=None)
        self.assertEqual(1, n)
        self.assertEqual(snapshot, json.dumps(orig, sort_keys=True))
        self.assertIsNot(out[0], orig[0])
        out[0]['nested']['a'].append(2)
        self.assertEqual([1], orig[0]['nested']['a'])

    def test_shared_cache_A_then_B_then_admin(self):
        """A(배정감독) 요청 후 같은 캐시로 B(다른 감독)·admin(sup 없음)은 원본 그대로."""
        shared = [self._item(), {'name': OTHER, 'lat': AIS_LAT, 'lng': AIS_LNG}]
        snapshot = json.dumps(shared, sort_keys=True)
        with self.mirror(hours_ago=1):
            out_a, n_a = self._direct(shared, cur=None)
            self.assertEqual(1, n_a)
            # B = 감독2 (OTHER 만 배정; MINE 은 비배정)
            with appmod.app.test_request_context():
                from flask import session
                session['supervisor_id'] = 2
                out_b, n_b = tm.apply_fleet_overrides(shared, lambda v: None, now=self.now)
            # admin(sup 없음)
            out_c, n_c = self._direct(shared, cur=None, sup=False)
        self.assertEqual(AIS_LAT, [v for v in out_b if v['name'] == MINE][0]['lat'])
        self.assertEqual(0, n_c)
        self.assertEqual(AIS_LAT, out_c[0]['lat'])
        self.assertEqual(snapshot, json.dumps(shared, sort_keys=True))

    def test_admin_without_supervisor_route_sees_originals(self):
        self.write_fleet(ais_hours_ago=5)
        with self.mirror(hours_ago=1):
            self.assertEqual(MIR_LAT, self.fetch()[MINE]['lat'])      # A 먼저 → 캐시 채움
            with self.client.session_transaction() as s:
                s['role'] = 'admin'
                s.pop('supervisor_id')
            with appmod.app.app_context():
                appmod.execute("UPDATE users SET role='admin', supervisor_id=NULL WHERE username='m1'")
            f = self.fetch()
        self.assertEqual(AIS_LAT, f[MINE]['lat'])
        self.assertNotEqual('mirror', f[MINE].get('pos_source'))

    def test_mid_overlay_exception_returns_original(self):
        orig = [self._item(), dict(self._item(), name='X')]
        with self.mirror(hours_ago=1), \
                mock.patch.object(tm, 'norm_name', side_effect=lambda x: (_ for _ in ()).throw(RuntimeError('x'))
                                  if x == 'X' else tm.re.sub(r'[^A-Z0-9]', '', str(x or '').upper())):
            out, n = self._direct(orig, cur=None)
        self.assertIs(out, orig)
        self.assertEqual(0, n)
        self.assertEqual(AIS_LAT, orig[0]['lat'])

    def test_slow_read_deadline(self):
        def slow(path, timeout=None):
            time.sleep(3)
            return []
        with mock.patch.object(tm, 'TIMEOUT_SEC', 0.3), mock.patch.object(tm, '_get_json', side_effect=slow):
            t0 = time.monotonic()
            out, n = self._direct([self._item()], cur=None)
            el = time.monotonic() - t0
        self.assertLess(el, 1.5)
        self.assertEqual(0, n)
        self.assertEqual(AIS_LAT, out[0]['lat'])
        with mock.patch.object(tm, 'TIMEOUT_SEC', 0.3), mock.patch.object(tm, '_get_json', side_effect=slow):
            t0 = time.monotonic()
            data, stale, _ = tm.get_upstream()
            self.assertLess(time.monotonic() - t0, 2.0)
        self.assertIsNone(data)
        self.assertTrue(stale)

    def _warm_today(self, names):
        tk = datetime.now(tm.KST).strftime('%Y%m%d')
        reg = {tk: [{'vessel': n, 'key': tm.norm_name(n)} for n in names]}
        tm._cache['data'] = {'vessels': [], 'today': {'registry': reg}, 'registry': reg}
        tm._cache['at'] = time.time()

    def test_transit_today_flag_on_main_marker(self):
        self.write_fleet(ais_hours_ago=1)
        self._warm_today([MINE, OTHER])
        with mock.patch.object(tm, '_get_json', side_effect=OSError('down')):  # 위치 미러 없음
            v = self.fetch()[MINE]
        self.assertTrue(v.get('transit_today'))
        self.assertEqual(AIS_LAT, v['lat'])          # 깜빡임만, 위치는 그대로

    def test_transit_today_not_for_unassigned_and_original_untouched(self):
        self._warm_today([OTHER])
        orig = [{'name': OTHER, 'lat': 1}, {'name': MINE, 'lat': 1}]
        snap = json.dumps(orig)
        with mock.patch.object(tm, '_get_json', side_effect=OSError('down')):
            out, n = self._direct(orig, cur=None)
        self.assertEqual(snap, json.dumps(orig))
        self.assertFalse(any(x.get('transit_today') for x in out))

    def test_transit_today_cold_cache_nonblocking(self):
        self.write_fleet(ais_hours_ago=1)

        def slow(path, timeout=None):
            time.sleep(2)
            raise OSError('slow')
        with mock.patch.object(tm, '_get_json', side_effect=slow), mock.patch.object(tm, 'TIMEOUT_SEC', 0.3):
            t0 = time.monotonic()
            v = self.fetch()[MINE]
            self.assertLess(time.monotonic() - t0, 1.5)
        self.assertFalse(v.get('transit_today'))


if __name__ == '__main__':
    unittest.main()
