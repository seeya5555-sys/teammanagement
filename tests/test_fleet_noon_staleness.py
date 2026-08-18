"""noon 보고 누락 경보 오탐 회귀 — 2026-08-18 실사고.

증상: SVMS 에는 noon 이 매일 정상 적재되고(push 된 rpt_dt=어제) 대시보드/앱은
"noon 보고 누락 12척 · 8/12부터 7일" 을 띄웠다.

원인 2개 — 둘 다 `rpt_dt`(= SVMS noon 보고일) 를 **다른 피드의 날짜로 덮어써** 과거로 끌어내린 것:
  1) `_overlay_trmtdb_positions` 가 ship-position `event_at` 날짜를 rpt_dt 에 대입.
     upstream(trmtdb) 이 2026-08-11 08:00 에 얼자 11척 rpt_dt 가 08-11 로 후퇴 → 7일 누락 오탐.
     ⚠️ 반대 방향도 결함이었다 — upstream 이 정상이면 rpt_dt 가 늘 '오늘' 이라 이 경보는 **영구 무력화**.
  2) 이메일 override 가 `reported_at` 날짜를 무조건 rpt_dt 에 대입.
     override 가 SVMS noon 보다 오래되면(PERU 8/12 vs noon 8/17) 역시 후퇴 → 오탐.

계약: `rpt_dt` 는 SVMS noon 보고일이다(iOS `Fleet.rpt_dt` 주석도 동일). 측위 신선도는
`position_ts`/`pos_reported_at` 이 따로 들고 있으므로 rpt_dt 를 건드릴 이유가 없다.
"""
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta

import app as appmod
import routes_tail
from source_bundle import shared_ns

VESSEL = 'ATLANTIC GENEVA'
EMAIL_VESSEL = 'PERU PROSPERITY'


def _kst_today():
    return (datetime.utcnow() + timedelta(hours=9)).date()


def _ymd(days_ago):
    return (_kst_today() - timedelta(days=days_ago)).strftime('%Y%m%d')


def _iso(days_ago):
    return (_kst_today() - timedelta(days=days_ago)).strftime('%Y-%m-%d') + ' 08:00:29'


class FleetNoonStalenessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = {
            'db': appmod.DATABASE,
            'cfg': appmod.app.config['DATABASE'],
            'map': shared_ns.FLEET_MAP_FILE,
            'ovr': shared_ns.FLEET_OVERRIDE_FILE,
            'aisoff': shared_ns.FLEET_AIS_OFF_FILE,
            'cache': dict(routes_tail._trmtdb_position_cache),
            'apikey': os.environ.get('TRMTDB_API_KEY'),
            'testing': appmod.app.config.get('TESTING'),
        }
        db = os.path.join(self.tmp.name, 'test.db')
        appmod.DATABASE = db
        appmod.app.config['DATABASE'] = db
        appmod.app.config['TESTING'] = True
        shared_ns.FLEET_MAP_FILE = os.path.join(self.tmp.name, 'fleet_map.json')
        shared_ns.FLEET_OVERRIDE_FILE = os.path.join(self.tmp.name, 'ovr.json')
        shared_ns.FLEET_AIS_OFF_FILE = os.path.join(self.tmp.name, 'aisoff.json')
        # 백그라운드 refresh 가 실제 upstream 을 때리지 않도록 키를 비운다(캐시본만 사용).
        os.environ.pop('TRMTDB_API_KEY', None)
        with appmod.app.app_context():
            appmod.init_db(False)
            sid = appmod.execute("INSERT INTO supervisors(name) VALUES(?)", ('손유석',))
            for name in (VESSEL, EMAIL_VESSEL):
                vid = appmod.execute("INSERT INTO vessels(name) VALUES(?)", (name,))
                appmod.execute(
                    "INSERT INTO supervisor_vessels(supervisor_id,vessel_id) VALUES(?,?)",
                    (sid, vid))
        self.client = appmod.app.test_client()
        with self.client.session_transaction() as s:
            s['user_id'] = 1
            s['username'] = 'noon-stale-test'
            s['role'] = 'admin'

    def tearDown(self):
        appmod.DATABASE = self.old['db']
        appmod.app.config['DATABASE'] = self.old['cfg']
        appmod.app.config['TESTING'] = self.old['testing']   # suite 간 상태 누수 차단
        shared_ns.FLEET_MAP_FILE = self.old['map']
        shared_ns.FLEET_OVERRIDE_FILE = self.old['ovr']
        shared_ns.FLEET_AIS_OFF_FILE = self.old['aisoff']
        routes_tail._trmtdb_position_cache.clear()
        routes_tail._trmtdb_position_cache.update(self.old['cache'])
        if self.old['apikey'] is None:
            os.environ.pop('TRMTDB_API_KEY', None)
        else:
            os.environ['TRMTDB_API_KEY'] = self.old['apikey']
        self.tmp.cleanup()

    # ── fixtures ──────────────────────────────────────────────
    def write_fleet(self, noon_days_ago, raw_rpt_dt=None):
        """push 된 payload — SVMS noon 은 이만큼 전에 들어왔다."""
        rpt = raw_rpt_dt if raw_rpt_dt is not None else _ymd(noon_days_ago)
        with open(shared_ns.FLEET_MAP_FILE, 'w', encoding='utf-8') as f:
            json.dump({'fleet': [
                {'name': VESSEL, 'imo': '9111111', 'lat': 25.0, 'lng': 56.0,
                 'rpt_dt': rpt},
                {'name': EMAIL_VESSEL, 'imo': '9222222', 'lat': 26.0, 'lng': 57.0,
                 'rpt_dt': rpt},
            ], 'supervisors': [],
                'generated_at': (datetime.utcnow() + timedelta(hours=9)).strftime('%Y-%m-%d %H:%M')}, f)

    def freeze_trmtdb(self, days_ago):
        """ship-position upstream 이 days_ago 에 멈춘 상태를 캐시에 주입."""
        routes_tail._trmtdb_position_cache.update({
            'at': time.monotonic(), 'loaded': True, 'error': None,
            'fetched_at': datetime.utcnow().isoformat(timespec='seconds'),
            'vessels': [{'vessel_name': VESSEL, 'imo': '9111111',
                         'latest': {'latitude': 25.5, 'longitude': 56.5,
                                    'platform': 'STORMGEO', 'event_at': _iso(days_ago)},
                         'latest_event_at': _iso(days_ago)}],
        })

    def stale_email_override(self, days_ago):
        with open(shared_ns.FLEET_OVERRIDE_FILE, 'w', encoding='utf-8') as f:
            json.dump({EMAIL_VESSEL.lower(): {
                'vessel': EMAIL_VESSEL, 'lat': 26.5, 'lng': 57.5, 'source': 'email',
                'reported_at': (_kst_today() - timedelta(days=days_ago)).strftime('%Y-%m-%d') + 'T08:00',
            }}, f)

    def email_override_raw(self, reported_at):
        with open(shared_ns.FLEET_OVERRIDE_FILE, 'w', encoding='utf-8') as f:
            json.dump({EMAIL_VESSEL.lower(): {
                'vessel': EMAIL_VESSEL, 'lat': 26.5, 'lng': 57.5, 'source': 'email',
                'reported_at': reported_at,
            }}, f)

    def fetch(self):
        r = self.client.get('/api/fleet-map/data')
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        return d, {v['name']: v for v in d['fleet']}, \
            {v['name']: v for v in (d.get('staleness') or {}).get('vessels', [])}

    # ── 회귀 ──────────────────────────────────────────────────
    def test_frozen_position_feed_does_not_fake_noon_gap(self):
        """upstream 이 7일 전에 얼어도, noon 이 어제 들어왔으면 누락 아님."""
        self.write_fleet(noon_days_ago=1)
        self.freeze_trmtdb(days_ago=7)
        _, fleet, stale = self.fetch()
        self.assertEqual(fleet[VESSEL]['rpt_dt'], _ymd(1),
                         'ship-position event_at 이 rpt_dt 를 덮어쓰면 안 된다')
        self.assertNotIn(VESSEL, stale, '얼어붙은 측위 피드로 noon 누락 오탐 발생')
        # 측위 자체는 정상 병합되어야 한다(이 수정이 오버레이를 끄는 게 아님).
        self.assertEqual(fleet[VESSEL]['pos_source'], 'trmtdb')
        self.assertEqual(fleet[VESSEL]['position_ts'], _iso(7))

    def test_stale_email_override_does_not_fake_noon_gap(self):
        """override 가 noon 보다 오래되면 rpt_dt 를 뒤로 끌지 않는다(단조 전진)."""
        self.write_fleet(noon_days_ago=1)
        self.stale_email_override(days_ago=6)
        _, fleet, stale = self.fetch()
        self.assertEqual(fleet[EMAIL_VESSEL]['rpt_dt'], _ymd(1))
        self.assertNotIn(EMAIL_VESSEL, stale, '낡은 이메일 override 로 noon 누락 오탐 발생')
        self.assertEqual(fleet[EMAIL_VESSEL]['pos_source'], 'email')

    def test_newer_email_override_still_suppresses(self):
        """원래 취지(최신 이메일 보고 = 경보 억제)는 유지 — noon 이 낡고 override 가 최신인 경우.
        단 억제는 판정 시점에서만 일어나고 rpt_dt 는 건드리지 않는다(아래 pure-svms 테스트가 짝)."""
        self.write_fleet(noon_days_ago=6)
        self.stale_email_override(days_ago=1)
        _, _, stale = self.fetch()
        self.assertNotIn(EMAIL_VESSEL, stale)

    def test_real_noon_gap_still_alerts(self):
        """negative control — 측위 피드가 **오늘치로 멀쩡해도** 진짜 noon 누락은 반드시 뜬다.
        옛 코드에선 이 경우 rpt_dt 가 오늘로 덮여 경보가 영구 무력화됐다."""
        self.write_fleet(noon_days_ago=5)
        self.freeze_trmtdb(days_ago=0)
        _, fleet, stale = self.fetch()
        self.assertEqual(fleet[VESSEL]['rpt_dt'], _ymd(5))
        self.assertIn(VESSEL, stale, '신선한 측위 피드가 진짜 noon 누락을 가리면 안 된다')
        self.assertEqual(stale[VESSEL]['days'], 5)

    # ── 올마이트 지적 보강(2026-08-18 리뷰) ─────────────────────
    def test_rpt_dt_stays_pure_svms_even_when_email_is_newer(self):
        """계약: 억제는 경보 판정에서만. `rpt_dt` 자체는 어떤 경우에도 SVMS noon 값 그대로.
        (fuel 라벨 'ROB · 최신 운항보고' 와 iOS Fleet.rpt_dt 가 이 값을 그대로 쓴다.)"""
        self.write_fleet(noon_days_ago=6)
        self.stale_email_override(days_ago=1)
        _, fleet, stale = self.fetch()
        self.assertEqual(fleet[EMAIL_VESSEL]['rpt_dt'], _ymd(6),
                         'override 가 rpt_dt 를 오염시키면 안 된다(경보 억제는 판정 시점에서만)')
        self.assertNotIn(EMAIL_VESSEL, stale)

    def test_email_and_position_feed_together(self):
        """두 오염원 동시 적용 — override 선박은 email 만, 나머지는 trmtdb 만 받는다."""
        self.write_fleet(noon_days_ago=1)
        self.freeze_trmtdb(days_ago=7)
        self.stale_email_override(days_ago=6)
        _, fleet, stale = self.fetch()
        self.assertEqual(stale, {}, f'오탐 발생: {sorted(stale)}')
        self.assertEqual(fleet[VESSEL]['rpt_dt'], _ymd(1))
        self.assertEqual(fleet[EMAIL_VESSEL]['rpt_dt'], _ymd(1))
        self.assertEqual(fleet[VESSEL]['pos_source'], 'trmtdb')
        self.assertEqual(fleet[EMAIL_VESSEL]['pos_source'], 'email')

    def test_same_day_email_does_not_flip_anything(self):
        """동일 날짜 = 전진 아님 → 그대로."""
        self.write_fleet(noon_days_ago=4)
        self.stale_email_override(days_ago=4)
        _, fleet, stale = self.fetch()
        self.assertEqual(fleet[EMAIL_VESSEL]['rpt_dt'], _ymd(4))
        self.assertEqual(stale[EMAIL_VESSEL]['days'], 4)

    def test_impossible_date_surfaces_as_unknown_not_silently_dropped(self):
        """허수날짜(20260231)는 조용히 빠지지 않고 '보고일 불명'으로 뜬다(fail-closed)."""
        self.write_fleet(noon_days_ago=1, raw_rpt_dt='20260231')
        _, _, stale = self.fetch()
        self.assertIn(VESSEL, stale)
        self.assertIsNone(stale[VESSEL]['days'])
        self.assertIsNone(stale[VESSEL]['last_rpt'])

    def test_future_email_override_does_not_crash_or_alert(self):
        """미래 날짜 override — miss 음수라 임계 미만 → 경보 없음(예외도 없음)."""
        self.write_fleet(noon_days_ago=9)
        self.email_override_raw(
            (_kst_today() + timedelta(days=3)).strftime('%Y-%m-%d') + 'T08:00')
        _, fleet, stale = self.fetch()
        self.assertNotIn(EMAIL_VESSEL, stale)
        self.assertEqual(fleet[EMAIL_VESSEL]['rpt_dt'], _ymd(9))

    def test_garbage_email_reported_at_falls_back_to_noon(self):
        """override 보고일이 쓰레기면 무시하고 SVMS noon 으로 판정(억제 남용 차단)."""
        self.write_fleet(noon_days_ago=9)
        self.email_override_raw('not-a-date')
        _, _, stale = self.fetch()
        self.assertIn(EMAIL_VESSEL, stale)
        self.assertEqual(stale[EMAIL_VESSEL]['days'], 9)


if __name__ == '__main__':
    unittest.main()
