"""SLOW 측위 플랫폼 폐기 회귀 — 형 지시 2026-08-18 ("Slow 파싱도 없애고, 해당 항목은 폐기").

배경: trmtdb `/api/ship-position?platform=ALL` 은 한 선박에 STORMGEO / VESSEL / SLOW 세 소스를
함께 준다(실측 `latest.platform` 분포 = STORMGEO 233 / VESSEL 66 / SLOW 19). SLOW 는
`position_date` 컬럼에 날짜 아닌 시각 문자열(`22:51:45`)이 섞여 있어 신뢰할 수 없다고 판정됐다.

계약:
  1. `latest` 가 SLOW 면 `by_platform` 에서 SLOW 를 뺀 최신 레코드로 **대체**한다.
  2. 대체본이 없으면 그 선박은 오버레이를 **건너뛴다**(SLOW 좌표로 덮지 않는다) → AIS/SVMS 폴백.
  3. `history` 항적에서도 SLOW 점을 뺀다(안 빼면 polyline 에만 폐기 소스가 남는다).
  4. 🔴 `platform` 키가 없거나 문자열이 아니면 **유지**한다 — '모르는 것' 을 폐기로 몰면
     upstream 이 필드를 빼는 배포 한 번에 선위가 통째로 사라진다(fail-closed 방향).
  5. 대체본을 쓸 때 row 레벨 `latest_event_at`(SLOW 포함 산출값) 을 신선도 폴백으로 쓰지 않는다.

⚠️ 여기 SLOW = trmtdb **측위 플랫폼**이다. 맥 파이프라인의 `slow_overlay.py`(slowspace 동정·ETA)
   와는 완전히 별개 시스템이고 그건 손대지 않았다.
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
IMO = '9111111'

AIS_LAT, AIS_LNG = 25.0, 56.0
SLOW_LAT, SLOW_LNG = 1.11, 2.22
GEO_LAT, GEO_LNG = 12.3, 44.4

_EPOCH = datetime(1970, 1, 1)


def _now_epoch():
    return (datetime.utcnow() - _EPOCH).total_seconds()


def _kst_today():
    return (datetime.utcnow() + timedelta(hours=9)).date()


def _event_at(hours_ago):
    """upstream 이 주는 형식 그대로 — naive 'YYYY-MM-DD HH:MM:SS'(타임존 표기 없음)."""
    return (datetime.utcnow() - timedelta(hours=hours_ago)).strftime('%Y-%m-%d %H:%M:%S')


def _rec(platform, lat, lng, hours_ago):
    return {'latitude': lat, 'longitude': lng, 'platform': platform,
            'event_at': _event_at(hours_ago)}


class SlowPlatformRetiredTests(unittest.TestCase):
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
            vid = appmod.execute("INSERT INTO vessels(name) VALUES(?)", (VESSEL,))
            appmod.execute(
                "INSERT INTO supervisor_vessels(supervisor_id,vessel_id) VALUES(?,?)",
                (sid, vid))
        self.client = appmod.app.test_client()
        with self.client.session_transaction() as s:
            s['user_id'] = 1
            s['username'] = 'slow-retire-test'
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
    def write_fleet(self, ais_hours_ago=1):
        """맥이 push 한 payload — vesseltracker AIS 좌표 + 측위 시각."""
        item = {'name': VESSEL, 'imo': IMO, 'lat': AIS_LAT, 'lng': AIS_LNG,
                'rpt_dt': _kst_today().strftime('%Y%m%d'),
                'position_source': 'AIS · vesseltracker (R-AIS Navtor)',
                'position_ts': (datetime.utcnow() - timedelta(hours=ais_hours_ago))
                .strftime('%Y-%m-%dT%H:%M') + '+0000',
                'position_ts_epoch': str(int(_now_epoch() - ais_hours_ago * 3600))}
        with open(shared_ns.FLEET_MAP_FILE, 'w', encoding='utf-8') as f:
            json.dump({'fleet': [item], 'supervisors': [],
                       'generated_at': (datetime.utcnow() + timedelta(hours=9))
                       .strftime('%Y-%m-%d %H:%M')}, f)

    def patch_item(self, fn):
        """이미 써 둔 push payload 를 손본다."""
        with open(shared_ns.FLEET_MAP_FILE, encoding='utf-8') as f:
            data = json.load(f)
        for it in data['fleet']:
            fn(it)
        with open(shared_ns.FLEET_MAP_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f)

    def upstream(self, row):
        routes_tail._trmtdb_position_cache.update({
            'at': time.monotonic(), 'loaded': True, 'error': None,
            'fetched_at': datetime.utcnow().isoformat(timespec='seconds'),
            'vessels': [dict({'vessel_name': VESSEL, 'imo': IMO}, **row)],
        })

    def fetch(self):
        r = self.client.get('/api/fleet-map/data')
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        return d, {v['name']: v for v in d['fleet']}

    def track(self):
        r = self.client.get('/api/fleet-map/track', query_string={'vessel': VESSEL})
        self.assertEqual(r.status_code, 200)
        return r.get_json()

    # ── 계약 1: SLOW latest 는 대체본으로 갈아탄다 ─────────────
    def test_slow_latest_is_replaced_by_stormgeo(self):
        """실측 케이스(SOUTH AFRICA PROSPERITY) — latest 가 SLOW 면 STORMGEO 로 대체."""
        self.write_fleet(ais_hours_ago=3 * 24)
        self.upstream({
            'latest': _rec('SLOW', SLOW_LAT, SLOW_LNG, 1),
            'latest_event_at': _event_at(1),
            'by_platform': {'SLOW': [_rec('SLOW', SLOW_LAT, SLOW_LNG, 1)],
                            'STORMGEO': [_rec('STORMGEO', GEO_LAT, GEO_LNG, 5),
                                         _rec('STORMGEO', 9.9, 9.9, 30)]},
        })
        d, fleet = self.fetch()
        v = fleet[VESSEL]
        self.assertEqual((v['lat'], v['lng']), (GEO_LAT, GEO_LNG),
                         '폐기한 SLOW 좌표가 화면에 나감')
        self.assertEqual(v['pos_source'], 'trmtdb')
        self.assertNotIn('SLOW', v['position_source'])
        self.assertIn('STORMGEO', v['position_source'])
        self.assertEqual(d['position_feed']['matched'], 1)
        self.assertEqual(d['position_feed']['retired_dropped'], 0)

    def test_replacement_picks_newest_non_retired_record(self):
        """대체본은 SLOW 제외 후 event_at 최대 — 플랫폼 간에도 최신을 고른다."""
        self.write_fleet(ais_hours_ago=3 * 24)
        self.upstream({
            'latest': _rec('SLOW', SLOW_LAT, SLOW_LNG, 1),
            'latest_event_at': _event_at(1),
            'by_platform': {'STORMGEO': [_rec('STORMGEO', 5.5, 6.6, 20)],
                            'VESSEL': [_rec('VESSEL', GEO_LAT, GEO_LNG, 4)]},
        })
        _, fleet = self.fetch()
        self.assertEqual((fleet[VESSEL]['lat'], fleet[VESSEL]['lng']), (GEO_LAT, GEO_LNG))
        self.assertIn('VESSEL', fleet[VESSEL]['position_source'])

    def _slow_latest_with_untimed_replacement(self):
        """대체본에 event_at 이 없고, row 레벨 시각만 (SLOW 기준으로) 신선한 upstream."""
        row_ts = _event_at(1)
        no_ts = _rec('STORMGEO', GEO_LAT, GEO_LNG, 5)
        no_ts.pop('event_at')
        self.upstream({
            'latest': _rec('SLOW', SLOW_LAT, SLOW_LNG, 1),
            'latest_event_at': row_ts,
            'by_platform': {'SLOW': [_rec('SLOW', SLOW_LAT, SLOW_LNG, 1)],
                            'STORMGEO': [no_ts]},
        })
        return row_ts

    def test_untimed_replacement_does_not_overwrite_timed_position(self):
        """🔴 계약 5 + 올마이트 지적 — 대체본 시각을 모르면 **시각 있는** 기존 좌표를 덮지 않는다.

        `latest_event_at` 은 SLOW 를 포함해 산출된 값이라(여기선 SLOW 가 1시간 전 → 신선) 그걸
        대체본 시각으로 빌려 쓰면 폐기한 소스의 시각으로 신선도를 판정하게 된다. 빌려오지 않으면
        나이를 모르는데, 나이 모를 좌표가 오늘 아침 AIS 를 밀어내면 2026-08-18 실사고와 같은 형태다.
        """
        self.write_fleet(ais_hours_ago=1)
        row_ts = self._slow_latest_with_untimed_replacement()
        d, fleet = self.fetch()
        v = fleet[VESSEL]
        self.assertEqual((v['lat'], v['lng']), (AIS_LAT, AIS_LNG))
        self.assertNotEqual(v.get('pos_source'), 'trmtdb')
        self.assertNotEqual(v.get('position_ts'), row_ts,
                            'SLOW 기준 timestamp 가 대체 레코드 시각으로 붙음')
        self.assertEqual(v.get('pos_stale_feed'), 'trmtdb')
        self.assertEqual(d['position_feed']['skipped_no_ts'], 1)
        self.assertEqual(d['position_feed']['matched'], 0)
        self.assertEqual(d['position_feed']['skipped_stale'], 0)

    def test_untimed_replacement_is_used_when_push_has_no_position(self):
        """반대편 — push 에 좌표가 없으면 시각 미상 대체본이라도 쓴다.
        (지도에서 배가 통째로 사라지는 게 나이 모를 좌표보다 나쁘다 = 기존 계약.)"""
        self.write_fleet(ais_hours_ago=1)
        self.patch_item(lambda it: it.update({'lat': None, 'lng': None}))
        row_ts = self._slow_latest_with_untimed_replacement()
        d, fleet = self.fetch()
        v = fleet[VESSEL]
        self.assertEqual((v['lat'], v['lng']), (GEO_LAT, GEO_LNG))
        self.assertEqual(v['pos_source'], 'trmtdb')
        self.assertIsNone(v.get('position_ts'))      # row 레벨 SLOW 시각을 빌려오지 않았다
        self.assertNotEqual(v.get('position_ts'), row_ts)
        self.assertEqual(d['position_feed']['matched'], 1)
        self.assertEqual(d['position_feed']['skipped_no_ts'], 0)

    # ── 올마이트 지적 보강(2026-08-18 리뷰) ─────────────────────
    def test_replacement_ordering_uses_parsed_time_not_lexical(self):
        """🔴 `event_at` 사전순 비교 금지 — 'T' 구분자·분 단위 표기가 섞이면 사전순 ≠ 시간순.

        '2026-...T...'(T) 는 '2026-... '(공백) 보다 사전순으로 항상 크다. 아래 fixture 는
        **더 오래된** 레코드에 T 표기를 줘서, 사전순 비교면 그 낡은 좌표를 최신으로 뽑게 만든다.
        """
        self.write_fleet(ais_hours_ago=3 * 24)
        old_t = _rec('STORMGEO', 8.88, 9.99, 30)
        old_t['event_at'] = _event_at(30).replace(' ', 'T')      # 낡음 + 사전순 최대
        new_space = _rec('VESSEL', GEO_LAT, GEO_LNG, 2)          # 최신 + 사전순 작음
        self.upstream({
            'latest': _rec('SLOW', SLOW_LAT, SLOW_LNG, 1),
            'latest_event_at': _event_at(1),
            'by_platform': {'STORMGEO': [old_t], 'VESSEL': [new_space]},
        })
        _, fleet = self.fetch()
        self.assertEqual((fleet[VESSEL]['lat'], fleet[VESSEL]['lng']), (GEO_LAT, GEO_LNG),
                         '사전순 비교라 낡은 레코드를 최신으로 뽑음')

    def test_untimed_candidate_loses_to_timed_candidate(self):
        """해석 불가/누락 시각 레코드는 최하위로 밀되 후보 자격은 유지한다."""
        self.write_fleet(ais_hours_ago=3 * 24)
        junk = _rec('STORMGEO', 8.88, 9.99, 1)
        junk['event_at'] = '쓰레기'
        self.upstream({
            'latest': _rec('SLOW', SLOW_LAT, SLOW_LNG, 1),
            'latest_event_at': _event_at(1),
            'by_platform': {'STORMGEO': [junk],
                            'VESSEL': [_rec('VESSEL', GEO_LAT, GEO_LNG, 20)]},
        })
        _, fleet = self.fetch()
        self.assertEqual((fleet[VESSEL]['lat'], fleet[VESSEL]['lng']), (GEO_LAT, GEO_LNG))

    def test_invalid_coordinate_candidate_falls_through_to_next(self):
        """🔴 최신 후보의 좌표가 깨졌으면 **그 다음 정상 후보**를 쓴다.
        (예전 로직은 깨진 걸 뽑아 놓고 호출부 검증에서 걸려 선박이 통째로 빠졌다.)"""
        for bad in ({'latitude': None, 'longitude': None},
                    {'latitude': 999.0, 'longitude': 0.0},
                    {'latitude': 'x', 'longitude': 'y'},
                    {'latitude': float('inf'), 'longitude': 0.0}):
            with self.subTest(bad=bad):
                self.write_fleet(ais_hours_ago=3 * 24)
                newest_bad = dict(_rec('STORMGEO', 0.0, 0.0, 1), **bad)
                self.upstream({
                    'latest': _rec('SLOW', SLOW_LAT, SLOW_LNG, 1),
                    'latest_event_at': _event_at(1),
                    'by_platform': {'STORMGEO': [newest_bad],
                                    'VESSEL': [_rec('VESSEL', GEO_LAT, GEO_LNG, 10)]},
                })
                d, fleet = self.fetch()
                self.assertEqual((fleet[VESSEL]['lat'], fleet[VESSEL]['lng']),
                                 (GEO_LAT, GEO_LNG))
                self.assertEqual(d['position_feed']['matched'], 1)
                self.assertEqual(d['position_feed']['retired_dropped'], 0)

    def test_all_candidates_invalid_counts_as_retired_drop(self):
        """정상 후보가 하나도 없으면 제외 — 그리고 그 사실이 카운터에 남아야 진단이 산다."""
        self.write_fleet(ais_hours_ago=1)
        self.upstream({
            'latest': _rec('SLOW', SLOW_LAT, SLOW_LNG, 1),
            'latest_event_at': _event_at(1),
            'by_platform': {'STORMGEO': [dict(_rec('STORMGEO', 0.0, 0.0, 1),
                                              latitude=None, longitude=None)]},
        })
        d, fleet = self.fetch()
        self.assertEqual((fleet[VESSEL]['lat'], fleet[VESSEL]['lng']), (AIS_LAT, AIS_LNG))
        self.assertEqual(d['position_feed']['retired_dropped'], 1)

    def test_by_platform_key_slow_is_skipped_even_if_record_says_otherwise(self):
        """`by_platform` 키가 SLOW 면 그 배열은 통째로 건너뛴다(레코드가 뭐라 주장하든)."""
        self.write_fleet(ais_hours_ago=1)
        mislabeled = _rec('STORMGEO', SLOW_LAT, SLOW_LNG, 1)     # 키는 SLOW, 레코드는 STORMGEO
        self.upstream({
            'latest': _rec('SLOW', 3.0, 4.0, 1),
            'latest_event_at': _event_at(1),
            'by_platform': {'SLOW': [mislabeled]},
        })
        d, fleet = self.fetch()
        self.assertEqual((fleet[VESSEL]['lat'], fleet[VESSEL]['lng']), (AIS_LAT, AIS_LNG))
        self.assertEqual(d['position_feed']['retired_dropped'], 1)

    def test_record_platform_slow_is_skipped_even_under_other_key(self):
        """반대 방향 — 키가 STORMGEO 여도 레코드 `platform` 이 SLOW 면 후보 아님(양쪽 다 본다)."""
        self.write_fleet(ais_hours_ago=1)
        self.upstream({
            'latest': _rec('SLOW', 3.0, 4.0, 1),
            'latest_event_at': _event_at(1),
            'by_platform': {'STORMGEO': [_rec('SLOW', SLOW_LAT, SLOW_LNG, 1)]},
        })
        d, fleet = self.fetch()
        self.assertEqual((fleet[VESSEL]['lat'], fleet[VESSEL]['lng']), (AIS_LAT, AIS_LNG))
        self.assertEqual(d['position_feed']['retired_dropped'], 1)

    def test_no_slow_string_leaks_into_api_payload(self):
        """폐기 소스 이름이 응답 어디에도(라벨·항적·진단) 새 나가지 않는다."""
        self.write_fleet(ais_hours_ago=3 * 24)
        self.upstream({
            'latest': _rec('SLOW', SLOW_LAT, SLOW_LNG, 1),
            'latest_event_at': _event_at(1),
            'by_platform': {'SLOW': [_rec('SLOW', SLOW_LAT, SLOW_LNG, 1)],
                            'STORMGEO': [_rec('STORMGEO', GEO_LAT, GEO_LNG, 5)]},
            'history': [_rec('SLOW', SLOW_LAT, SLOW_LNG, 9),
                        _rec('STORMGEO', 10.0, 20.0, 10)],
        })
        d, _ = self.fetch()
        self.assertNotIn('SLOW', json.dumps(d, ensure_ascii=False).upper())
        self.assertNotIn('SLOW', json.dumps(self.track(), ensure_ascii=False).upper())

    def test_non_retired_latest_still_uses_row_level_timestamp(self):
        """반대편 control — 폐기와 무관한 레코드는 종전대로 row 레벨 폴백을 쓴다
        (`_allow_row_ts` 게이트가 정상 경로까지 막지 않았는지)."""
        self.write_fleet(ais_hours_ago=3 * 24)
        row_ts = _event_at(1)
        no_ts = _rec('STORMGEO', GEO_LAT, GEO_LNG, 1)
        no_ts.pop('event_at')
        self.upstream({'latest': no_ts, 'latest_event_at': row_ts})
        d, fleet = self.fetch()
        self.assertEqual((fleet[VESSEL]['lat'], fleet[VESSEL]['lng']), (GEO_LAT, GEO_LNG))
        self.assertEqual(fleet[VESSEL]['position_ts'], row_ts)
        self.assertEqual(d['position_feed']['matched'], 1)

    # ── 계약 2: 대체본이 없으면 오버레이를 건너뛴다 ─────────────
    def test_slow_only_vessel_is_dropped_from_overlay(self):
        """SLOW 밖에 없는 선박(담당 밖 12척)은 오버레이에서 빠지고 AIS 폴백으로 내려간다."""
        self.write_fleet(ais_hours_ago=1)
        self.upstream({
            'latest': _rec('SLOW', SLOW_LAT, SLOW_LNG, 1),
            'latest_event_at': _event_at(1),
            'by_platform': {'SLOW': [_rec('SLOW', SLOW_LAT, SLOW_LNG, 1)]},
        })
        d, fleet = self.fetch()
        v = fleet[VESSEL]
        self.assertEqual((v['lat'], v['lng']), (AIS_LAT, AIS_LNG))
        self.assertNotEqual(v.get('pos_source'), 'trmtdb')
        self.assertIn('AIS', v['position_source'])
        self.assertEqual(d['position_feed']['matched'], 0)
        self.assertEqual(d['position_feed']['retired_dropped'], 1)
        # skip 이 아니라 애초에 후보에서 제외 — 신선도 카운터와 섞이지 않아야 진단이 산다.
        self.assertEqual(d['position_feed']['skipped_stale'], 0)

    def test_slow_latest_without_by_platform_is_dropped(self):
        """구 배포본(by_platform 없음)에서 latest 가 SLOW 면 대체 불가 → 제외."""
        self.write_fleet(ais_hours_ago=1)
        self.upstream({'latest': _rec('SLOW', SLOW_LAT, SLOW_LNG, 1),
                       'latest_event_at': _event_at(1)})
        d, fleet = self.fetch()
        self.assertEqual((fleet[VESSEL]['lat'], fleet[VESSEL]['lng']), (AIS_LAT, AIS_LNG))
        self.assertEqual(d['position_feed']['retired_dropped'], 1)

    def test_slow_case_and_whitespace_variants_are_retired(self):
        """플랫폼 문자열 표기 흔들림(소문자·공백)도 같은 소스로 본다."""
        for raw in ('slow', ' Slow ', 'SLOW'):
            with self.subTest(platform=raw):
                self.write_fleet(ais_hours_ago=1)
                self.upstream({'latest': _rec(raw, SLOW_LAT, SLOW_LNG, 1),
                               'latest_event_at': _event_at(1)})
                d, fleet = self.fetch()
                self.assertEqual((fleet[VESSEL]['lat'], fleet[VESSEL]['lng']),
                                 (AIS_LAT, AIS_LNG))
                self.assertEqual(d['position_feed']['retired_dropped'], 1)

    # ── 계약 4: 모르는 것은 유지(fail-closed 방향) ──────────────
    def test_missing_platform_key_is_kept(self):
        """🔴 `platform` 이 없으면 폐기로 몰지 않는다 — upstream 필드 제거 배포 한 번에
        선위가 통째로 사라지는 걸 막는다."""
        self.write_fleet(ais_hours_ago=3 * 24)
        latest = _rec('STORMGEO', GEO_LAT, GEO_LNG, 1)
        latest.pop('platform')
        self.upstream({'latest': latest, 'latest_event_at': _event_at(1)})
        d, fleet = self.fetch()
        self.assertEqual((fleet[VESSEL]['lat'], fleet[VESSEL]['lng']), (GEO_LAT, GEO_LNG))
        self.assertEqual(d['position_feed']['matched'], 1)
        self.assertEqual(d['position_feed']['retired_dropped'], 0)

    def test_non_string_platform_is_kept(self):
        """platform 이 None/숫자여도 유지 — 판정은 명확히 일치할 때만."""
        for bogus in (None, 0, ['SLOW']):
            with self.subTest(platform=bogus):
                self.write_fleet(ais_hours_ago=3 * 24)
                self.upstream({'latest': dict(_rec('X', GEO_LAT, GEO_LNG, 1),
                                              platform=bogus),
                               'latest_event_at': _event_at(1)})
                d, fleet = self.fetch()
                self.assertEqual((fleet[VESSEL]['lat'], fleet[VESSEL]['lng']),
                                 (GEO_LAT, GEO_LNG))
                self.assertEqual(d['position_feed']['retired_dropped'], 0)

    # ── negative control: 폐기 아닌 소스는 종전과 동일 ───────────
    def test_stormgeo_latest_is_unchanged(self):
        """STORMGEO/VESSEL latest 는 이 변경에 영향받지 않는다(233+66척 = 대다수)."""
        self.write_fleet(ais_hours_ago=3 * 24)
        self.upstream({
            'latest': _rec('STORMGEO', GEO_LAT, GEO_LNG, 1),
            'latest_event_at': _event_at(1),
            'by_platform': {'SLOW': [_rec('SLOW', SLOW_LAT, SLOW_LNG, 0)]},
        })
        d, fleet = self.fetch()
        v = fleet[VESSEL]
        self.assertEqual((v['lat'], v['lng']), (GEO_LAT, GEO_LNG))
        self.assertEqual(v['pos_source'], 'trmtdb')
        self.assertEqual(d['position_feed']['matched'], 1)
        self.assertEqual(d['position_feed']['retired_dropped'], 0)

    def test_missing_latest_is_not_resurrected_from_by_platform(self):
        """범위 고정 — latest 가 없는 선박을 by_platform 으로 새로 발굴하지 않는다.
        (이 변경은 'SLOW 폐기' 지, 신규 매칭 확대가 아니다.)"""
        self.write_fleet(ais_hours_ago=1)
        self.upstream({'latest': {}, 'latest_event_at': _event_at(1),
                       'by_platform': {'STORMGEO': [_rec('STORMGEO', GEO_LAT, GEO_LNG, 1)]}})
        d, fleet = self.fetch()
        self.assertEqual((fleet[VESSEL]['lat'], fleet[VESSEL]['lng']), (AIS_LAT, AIS_LNG))
        self.assertEqual(d['position_feed']['matched'], 0)
        # latest 자체가 없는 건 '폐기 때문에 빠진 것' 이 아니다 → 카운터를 오염시키지 않는다.
        self.assertEqual(d['position_feed']['retired_dropped'], 0)

    # ── 계약 3: 항적에서도 SLOW 점을 뺀다 ───────────────────────
    def test_track_excludes_slow_points(self):
        """실측: KUWAIT history 349점 = STORMGEO 104 / VESSEL 111 / SLOW 134 혼합."""
        self.write_fleet(ais_hours_ago=3 * 24)
        self.upstream({
            'latest': _rec('STORMGEO', GEO_LAT, GEO_LNG, 1),
            'latest_event_at': _event_at(1),
            'history': [_rec('STORMGEO', 10.0, 20.0, 10),
                        _rec('SLOW', SLOW_LAT, SLOW_LNG, 9),
                        _rec('VESSEL', 11.0, 21.0, 8),
                        _rec('slow', 3.33, 4.44, 7)],
        })
        points = self.track()['points']
        coords = {(p['lat'], p['lng']) for p in points}
        self.assertEqual(coords, {(10.0, 20.0), (11.0, 21.0)},
                         '폐기한 SLOW 점이 polyline 에 남음')

    def test_track_keeps_points_without_platform(self):
        """항적 쪽도 '모르면 유지' — platform 없는 이력점을 지우면 항적이 사라진다."""
        self.write_fleet(ais_hours_ago=3 * 24)
        pt = _rec('STORMGEO', 10.0, 20.0, 10)
        pt.pop('platform')
        self.upstream({
            'latest': _rec('STORMGEO', GEO_LAT, GEO_LNG, 1),
            'latest_event_at': _event_at(1),
            'history': [pt, _rec('VESSEL', 11.0, 21.0, 8)],
        })
        coords = {(p['lat'], p['lng']) for p in self.track()['points']}
        self.assertEqual(coords, {(10.0, 20.0), (11.0, 21.0)})


if __name__ == '__main__':
    unittest.main()
