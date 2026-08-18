"""Fleet Map 선위 신선도 회귀 — 2026-08-18 실사고 2탄.

증상: 지도가 일주일 전 선위를 그리고 있었다. 맥 파이프라인은 vesseltracker AIS 로 **오늘 아침**
좌표를 push 하고 있었는데(`position_ts_epoch` 동봉), 서버의 `_overlay_trmtdb_positions` 가
그 위를 **무조건** TRMT DB(`trmtdb.duckdns.org/api/ship-position`) 좌표로 덮었기 때문.
그 upstream 은 2026-08-11 08:00 에 정지한 상태였다(318척 전부 동일 타임스탬프).

계약: 우선순위 ①이메일 override ②TRMT DB ③vesseltracker AIS ④SVMS noon 은 유지하되,
**TRMT DB 가 확실히 더 낡았을 때만** 건너뛴다. 판정 불가(push 에 시각 없음 / event_at 파싱 불가 /
타임존 불확실 구간)면 기존 우선순위를 그대로 따른다 = 보수적.
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

AIS_LAT, AIS_LNG = 25.0, 56.0
DB_LAT, DB_LNG = 12.3, 44.4

_EPOCH = datetime(1970, 1, 1)


def _now_epoch():
    return (datetime.utcnow() - _EPOCH).total_seconds()


def _kst_today():
    return (datetime.utcnow() + timedelta(hours=9)).date()


def _event_at(hours_ago):
    """naive 'YYYY-MM-DD HH:MM:SS' — upstream 이 주는 형식 그대로(타임존 표기 없음)."""
    return (datetime.utcnow() - timedelta(hours=hours_ago)).strftime('%Y-%m-%d %H:%M:%S')


class FleetPositionFreshnessTests(unittest.TestCase):
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
        os.environ.pop('TRMTDB_API_KEY', None)   # 백그라운드 refresh 가 실제 upstream 을 못 때리게
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
            s['username'] = 'pos-fresh-test'
            s['role'] = 'admin'

    def tearDown(self):
        appmod.DATABASE = self.old['db']
        appmod.app.config['DATABASE'] = self.old['cfg']
        appmod.app.config['TESTING'] = self.old['testing']
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
    def write_fleet(self, ais_hours_ago=1, with_epoch=True):
        """맥이 push 한 payload — vesseltracker AIS 좌표 + 측위 시각."""
        item = {'name': VESSEL, 'imo': '9111111', 'lat': AIS_LAT, 'lng': AIS_LNG,
                'rpt_dt': _kst_today().strftime('%Y%m%d'),
                'position_source': 'AIS · vesseltracker (R-AIS Navtor)',
                'position_ts': (datetime.utcnow() - timedelta(hours=ais_hours_ago))
                .strftime('%Y-%m-%dT%H:%M') + '+0000'}
        if with_epoch:
            # 실제 push 는 문자열 숫자로 보낸다(맥 fleet_enriched.json 실측).
            item['position_ts_epoch'] = str(int(_now_epoch() - ais_hours_ago * 3600))
        email_item = dict(item, name=EMAIL_VESSEL, imo='9222222', lat=26.0, lng=57.0)
        with open(shared_ns.FLEET_MAP_FILE, 'w', encoding='utf-8') as f:
            json.dump({'fleet': [item, email_item], 'supervisors': [],
                       'generated_at': (datetime.utcnow() + timedelta(hours=9))
                       .strftime('%Y-%m-%d %H:%M')}, f)

    def patch_item(self, fn, name=VESSEL):
        """이미 써 둔 push payload 의 한 항목만 손본다."""
        with open(shared_ns.FLEET_MAP_FILE, encoding='utf-8') as f:
            data = json.load(f)
        for it in data['fleet']:
            if it['name'] == name:
                fn(it)
        with open(shared_ns.FLEET_MAP_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f)

    def patch_epoch(self, raw, name=VESSEL):
        self.patch_item(lambda it: it.update({'position_ts_epoch': raw}), name)

    def trmtdb(self, event_at):
        routes_tail._trmtdb_position_cache.update({
            'at': time.monotonic(), 'loaded': True, 'error': None,
            'fetched_at': datetime.utcnow().isoformat(timespec='seconds'),
            'vessels': [{'vessel_name': name, 'imo': imo,
                         'latest': {'latitude': DB_LAT, 'longitude': DB_LNG,
                                    'platform': 'STORMGEO', 'event_at': event_at},
                         'latest_event_at': event_at}
                        for name, imo in ((VESSEL, '9111111'), (EMAIL_VESSEL, '9222222'))],
        })

    def email_override(self):
        with open(shared_ns.FLEET_OVERRIDE_FILE, 'w', encoding='utf-8') as f:
            json.dump({EMAIL_VESSEL.lower(): {
                'vessel': EMAIL_VESSEL, 'lat': 33.3, 'lng': 44.4, 'source': 'email',
                'reported_at': _kst_today().strftime('%Y-%m-%d') + 'T08:00',
            }}, f)

    def fetch(self):
        r = self.client.get('/api/fleet-map/data')
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        return d, {v['name']: v for v in d['fleet']}

    # ── 회귀 ──────────────────────────────────────────────────
    def test_stale_trmtdb_does_not_overwrite_fresh_ais(self):
        """실사고 본체 — 7일 얼어붙은 TRMT DB 가 오늘 아침 AIS 좌표를 덮으면 안 된다."""
        self.write_fleet(ais_hours_ago=1)
        self.trmtdb(_event_at(hours_ago=7 * 24))
        d, fleet = self.fetch()
        v = fleet[VESSEL]
        self.assertEqual((v['lat'], v['lng']), (AIS_LAT, AIS_LNG),
                         '낡은 TRMT DB 좌표가 신선한 AIS 를 덮어씀')
        self.assertNotEqual(v.get('pos_source'), 'trmtdb')
        self.assertIn('AIS', str(v.get('position_source')))
        self.assertEqual(v.get('pos_stale_feed'), 'trmtdb')
        self.assertEqual(d['position_feed']['skipped_stale'], 2)
        self.assertEqual(d['position_feed']['matched'], 0)

    def test_fresh_trmtdb_still_overlays(self):
        """negative control — TRMT DB 가 살아 있으면 종전대로 최우선(이 수정은 오버레이를 끄는 게 아님)."""
        self.write_fleet(ais_hours_ago=3 * 24)
        self.trmtdb(_event_at(hours_ago=1))
        d, fleet = self.fetch()
        v = fleet[VESSEL]
        self.assertEqual((v['lat'], v['lng']), (DB_LAT, DB_LNG))
        self.assertEqual(v['pos_source'], 'trmtdb')
        self.assertEqual(d['position_feed']['matched'], 2)
        self.assertEqual(d['position_feed']['skipped_stale'], 0)

    def test_missing_pushed_epoch_falls_back_to_trmtdb(self):
        """push 에 측위 시각이 없으면 비교 불가 → 종전 우선순위(좌표 없는 것보단 낫다)."""
        self.write_fleet(with_epoch=False)
        self.trmtdb(_event_at(hours_ago=7 * 24))
        d, fleet = self.fetch()
        self.assertEqual((fleet[VESSEL]['lat'], fleet[VESSEL]['lng']), (DB_LAT, DB_LNG))
        self.assertEqual(d['position_feed']['skipped_stale'], 0)

    def test_unparseable_event_at_falls_back_to_trmtdb(self):
        """event_at 이 해석 불가면 '낡았다'고 단정하지 않는다."""
        self.write_fleet(ais_hours_ago=1)
        self.trmtdb('nonsense')
        d, fleet = self.fetch()
        self.assertEqual((fleet[VESSEL]['lat'], fleet[VESSEL]['lng']), (DB_LAT, DB_LNG))
        self.assertEqual(d['position_feed']['skipped_stale'], 0)

    def test_timezone_ambiguity_resolves_in_favor_of_trmtdb(self):
        """event_at 에 타임존 표기가 없다. 존 불확실성(±12h) 안쪽이면 낡았다고 단정하지 않는다.
        AIS 가 방금, TRMT DB 가 6시간 전 → 존을 UTC-12 로 최대한 유리하게 읽으면 아직 더 최신."""
        self.write_fleet(ais_hours_ago=0)
        self.trmtdb(_event_at(hours_ago=6))
        d, fleet = self.fetch()
        self.assertEqual((fleet[VESSEL]['lat'], fleet[VESSEL]['lng']), (DB_LAT, DB_LNG))
        self.assertEqual(d['position_feed']['skipped_stale'], 0)

    def test_ais_stale_warning_survives_skip(self):
        """skip 하면 화면은 AIS 좌표를 쓴다 → AIS 끊김 경고가 반드시 살아 있어야 한다.
        (경고까지 사라지면 낡은 AIS 가 아무 표식 없이 '현재 선위'로 나간다.)"""
        self.write_fleet(ais_hours_ago=10)          # AIS_STALE_HOURS = 6 초과
        self.trmtdb(_event_at(hours_ago=7 * 24))
        _, fleet = self.fetch()
        self.assertEqual((fleet[VESSEL]['lat'], fleet[VESSEL]['lng']), (AIS_LAT, AIS_LNG))
        self.assertTrue(fleet[VESSEL]['ais_stale'], '낡은 AIS 가 경고 없이 표시됨')

    def test_fresh_ais_is_not_flagged_stale(self):
        """반대쪽 control — skip 됐어도 AIS 가 신선하면 끊김 경고를 띄우지 않는다."""
        self.write_fleet(ais_hours_ago=1)
        self.trmtdb(_event_at(hours_ago=7 * 24))
        _, fleet = self.fetch()
        self.assertFalse(fleet[VESSEL]['ais_stale'])

    # ── 올마이트 지적 보강(2026-08-18 리뷰) ─────────────────────
    def test_nonfinite_epoch_does_not_win(self):
        """`Infinity`/`nan` 이 '무조건 더 최신'으로 읽혀 TRMT DB 를 영구히 밀어내면 안 된다."""
        for bogus in ('Infinity', '-Infinity', 'nan'):
            with self.subTest(epoch=bogus):
                self.write_fleet(ais_hours_ago=1)
                self.patch_epoch(bogus)
                self.trmtdb(_event_at(hours_ago=7 * 24))
                _, fleet = self.fetch()
                self.assertEqual((fleet[VESSEL]['lat'], fleet[VESSEL]['lng']), (DB_LAT, DB_LNG))
                self.assertNotIn('pos_stale_feed', fleet[VESSEL])

    def test_absurd_epoch_range_is_rejected(self):
        """먼 미래·먼 과거 epoch 도 비교 불가로 본다(범위 밖 = 신뢰 못 할 값)."""
        for bogus in (str(int(_now_epoch() + 40 * 86400)), '10000'):
            with self.subTest(epoch=bogus):
                self.write_fleet(ais_hours_ago=1)
                self.patch_epoch(bogus)
                self.trmtdb(_event_at(hours_ago=7 * 24))
                _, fleet = self.fetch()
                self.assertEqual((fleet[VESSEL]['lat'], fleet[VESSEL]['lng']), (DB_LAT, DB_LNG))
                self.assertNotIn('pos_stale_feed', fleet[VESSEL])

    def test_no_pushed_coordinates_never_skips(self):
        """push 에 좌표가 없으면 skip 하면 안 된다 — 그 배가 지도에서 통째로 사라진다."""
        self.write_fleet(ais_hours_ago=1)
        self.patch_item(lambda it: it.update({'lat': None, 'lng': None}))
        self.trmtdb(_event_at(hours_ago=7 * 24))
        _, fleet = self.fetch()
        self.assertEqual((fleet[VESSEL]['lat'], fleet[VESSEL]['lng']), (DB_LAT, DB_LNG))
        self.assertNotIn('pos_stale_feed', fleet[VESSEL])

    def test_equal_timestamps_do_not_skip(self):
        """경계 — 상한이 push 시각과 같으면 '확실히 더 낡음' 이 아니다 → 덮어쓴다."""
        self.write_fleet(ais_hours_ago=1)
        # up_at = naive(UTC) + 12h 이므로, event_at 을 push 시각 -12h 로 두면 정확히 동률.
        eq = datetime.utcfromtimestamp(_now_epoch() - 3600 - 12 * 3600)
        self.patch_epoch(str(int(_now_epoch() - 3600)))
        self.trmtdb(eq.strftime('%Y-%m-%d %H:%M:%S'))
        d, fleet = self.fetch()
        self.assertEqual((fleet[VESSEL]['lat'], fleet[VESSEL]['lng']), (DB_LAT, DB_LNG))
        self.assertEqual(d['position_feed']['skipped_stale'], 0)
        # 1초만 더 낡으면 반대편으로 넘어간다(경계가 실제로 여기 있음을 고정).
        self.trmtdb((eq - timedelta(seconds=1)).strftime('%Y-%m-%d %H:%M:%S'))
        d, fleet = self.fetch()
        self.assertEqual((fleet[VESSEL]['lat'], fleet[VESSEL]['lng']), (AIS_LAT, AIS_LNG))
        self.assertEqual(d['position_feed']['skipped_stale'], 2)   # 두 선박 조건이 동일

    def test_event_at_parsing_is_strict_not_prefix(self):
        """앞 19자만 맞는 값('... 08:00:29 쓰레기')을 성공 처리하면 안 된다.
        해석 불가 → 종전 우선순위(TRMT DB) 유지."""
        self.write_fleet(ais_hours_ago=1)
        self.trmtdb(_event_at(hours_ago=7 * 24) + ' 쓰레기')
        d, fleet = self.fetch()
        self.assertEqual((fleet[VESSEL]['lat'], fleet[VESSEL]['lng']), (DB_LAT, DB_LNG))
        self.assertEqual(d['position_feed']['skipped_stale'], 0)

    def test_iso_t_and_minute_precision_still_parse(self):
        """반대편 — 'T' 구분자·분 단위 표기는 계약대로 해석된다(strict 화로 죽지 않았는지)."""
        for raw in (_event_at(hours_ago=7 * 24).replace(' ', 'T'),
                    _event_at(hours_ago=7 * 24)[:16]):
            with self.subTest(event_at=raw):
                self.write_fleet(ais_hours_ago=1)
                self.trmtdb(raw)
                d, fleet = self.fetch()
                self.assertEqual((fleet[VESSEL]['lat'], fleet[VESSEL]['lng']), (AIS_LAT, AIS_LNG))
                self.assertEqual(d['position_feed']['skipped_stale'], 2)

    def test_stale_marker_is_cleared_when_overlay_applies(self):
        """직전 진단 마커가 payload 에 남아 있어도, 덮어쓰는 회차엔 지워진다(모순 필드 방지)."""
        self.write_fleet(ais_hours_ago=3 * 24)
        self.patch_item(lambda it: it.update({'pos_stale_feed': 'trmtdb'}))
        self.trmtdb(_event_at(hours_ago=1))
        _, fleet = self.fetch()
        self.assertEqual(fleet[VESSEL]['pos_source'], 'trmtdb')
        self.assertNotIn('pos_stale_feed', fleet[VESSEL])

    def test_skipped_item_field_set_is_self_consistent(self):
        """skip 항목의 필드 조합이 서로 모순되지 않는지 — 좌표·출처·시각이 전부 AIS 쪽이어야 한다."""
        self.write_fleet(ais_hours_ago=1)
        self.trmtdb(_event_at(hours_ago=7 * 24))
        _, fleet = self.fetch()
        v = fleet[VESSEL]
        self.assertEqual((v['lat'], v['lng']), (AIS_LAT, AIS_LNG))
        self.assertIn('AIS', v['position_source'])
        self.assertNotIn('TRMT DB', v['position_source'])
        # posSrcLabelBase 는 pos_source 로 분기한다 — 'trmtdb'/'email' 이 아니어야 AIS 라벨로 간다.
        self.assertNotIn(v.get('pos_source'), ('trmtdb', 'email'))
        # position_ts / position_ts_epoch 도 AIS 값 그대로여야 ais_stale 판정이 성립한다.
        self.assertTrue(str(v['position_ts']).endswith('+0000'))
        self.assertAlmostEqual(float(v['position_ts_epoch']), _now_epoch() - 3600, delta=120)
        self.assertEqual(v['pos_stale_feed'], 'trmtdb')

    def test_email_override_still_wins(self):
        """우선순위 최상단(이메일)은 이 로직에 영향받지 않는다."""
        self.write_fleet(ais_hours_ago=1)
        self.trmtdb(_event_at(hours_ago=7 * 24))
        self.email_override()
        d, fleet = self.fetch()
        v = fleet[EMAIL_VESSEL]
        self.assertEqual((v['lat'], v['lng']), (33.3, 44.4))
        self.assertEqual(v['pos_source'], 'email')
        # override 선박은 애초에 오버레이 대상이 아니므로 skip 카운트에도 안 들어간다.
        self.assertEqual(d['position_feed']['skipped_stale'], 1)


if __name__ == '__main__':
    unittest.main()
