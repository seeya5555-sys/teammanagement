"""slowspace '최신 동정' 항목 폐기 회귀 — 형 지시 2026-08-18.

원문: "P2 슬로우는 항목 삭제해줘. 필요없음 이제 Slow 파싱도 없애고. 해당 항목은 폐기."
그리고 배포 후에도 화면에 남아 있는 걸 보고: "이거 아직 나오는데?"(선박 상세 사이드바의
`Slow · 최신 동정` / `동정  Departed Tanjung Pelepas (2026-08-05 03:40)`).

⚠️ 이름이 겹치는 **다른** 폐기 건과 섞지 말 것:
  - 여기(이 파일) = 맥 `automation/fleet-map/slow_overlay.py` 가 slowspace.io 에서 긁어
    push payload 에 얹던 **선박 필드** `slow_last_notif`(+ 옛 `slow_eta`/`slow_next_port`).
  - `test_fleet_slow_platform_retired.py` = trmtdb `/api/ship-position` 의 **측위 플랫폼**
    `SLOW`. 완전 별개 시스템이다.

폐기는 3층으로 했고, 이 파일은 서버·템플릿 두 층과 맥 wiring 을 지킨다:
  1. 생산측 — `slow_overlay.py`/`slowspace.py` 를 `automation/_retired/` 로 내림.
  2. 서버 수용측 — push 가 폐기 필드를 **저장하지 않는다**(외부 입력이라, 옛 러너가 남아
     돌거나 누가 옛 payload 를 수동으로 밀면 렌더만 지운 상태에선 조용히 되살아난다).
  3. 렌더 — `templates/dashboard.html` 의 `slowSec()` 제거.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import app as appmod
from source_bundle import shared_ns

VESSEL = 'ATLANTIC GENEVA'
API_KEY = 'slow-notif-retire-test-key'
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKSPACE_FLEET_MAP = os.path.expanduser('~/.openclaw/workspace/automation/fleet-map')

RETIRED_FIELDS = ('slow_last_notif', 'slow_eta', 'slow_next_port')

NOTIF = {'ts': '2026-08-05 03:40', 'text': 'Departed Tanjung Pelepas'}


class SlowNotifRetiredTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = {
            'db': appmod.DATABASE,
            'cfg': appmod.app.config['DATABASE'],
            'map': shared_ns.FLEET_MAP_FILE,
            'ovr': shared_ns.FLEET_OVERRIDE_FILE,
            'aisoff': shared_ns.FLEET_AIS_OFF_FILE,
            'testing': appmod.app.config.get('TESTING'),
            'trmtdb': os.environ.get('TRMTDB_API_KEY'),
        }
        db = os.path.join(self.tmp.name, 'test.db')
        appmod.DATABASE = db
        appmod.app.config['DATABASE'] = db
        appmod.app.config['TESTING'] = True
        shared_ns.FLEET_MAP_FILE = os.path.join(self.tmp.name, 'fleet_map.json')
        shared_ns.FLEET_OVERRIDE_FILE = os.path.join(self.tmp.name, 'ovr.json')
        shared_ns.FLEET_AIS_OFF_FILE = os.path.join(self.tmp.name, 'aisoff.json')
        os.environ.pop('TRMTDB_API_KEY', None)   # 실제 upstream 을 안 때리게
        with appmod.app.app_context():
            appmod.init_db(False)
            sid = appmod.execute("INSERT INTO supervisors(name) VALUES(?)", ('손유석',))
            vid = appmod.execute("INSERT INTO vessels(name) VALUES(?)", (VESSEL,))
            appmod.execute(
                "INSERT INTO supervisor_vessels(supervisor_id,vessel_id) VALUES(?,?)",
                (sid, vid))
            appmod.execute("CREATE TABLE IF NOT EXISTS api_settings (k TEXT PRIMARY KEY, v TEXT)")
            appmod.execute("INSERT OR REPLACE INTO api_settings (k, v) VALUES ('api_key', ?)",
                           (API_KEY,))
        self.client = appmod.app.test_client()
        with self.client.session_transaction() as s:
            s['user_id'] = 1
            s['username'] = 'slow-notif-retire-test'
            s['role'] = 'admin'

    def tearDown(self):
        appmod.DATABASE = self.old['db']
        appmod.app.config['DATABASE'] = self.old['cfg']
        appmod.app.config['TESTING'] = self.old['testing']
        shared_ns.FLEET_MAP_FILE = self.old['map']
        shared_ns.FLEET_OVERRIDE_FILE = self.old['ovr']
        shared_ns.FLEET_AIS_OFF_FILE = self.old['aisoff']
        if self.old['trmtdb'] is None:
            os.environ.pop('TRMTDB_API_KEY', None)
        else:
            os.environ['TRMTDB_API_KEY'] = self.old['trmtdb']
        self.tmp.cleanup()

    # ── fixtures ──────────────────────────────────────────────
    def push(self, item_extra, expect=200):
        item = {'name': VESSEL, 'imo': '9111111', 'lat': 1.0, 'lng': 2.0}
        item.update(item_extra)
        r = self.client.post('/api/ext/fleet-map/push',
                             json={'generated_at': '2026-08-18 12:00', 'fleet': [item]},
                             headers={'X-API-Key': API_KEY})
        self.assertEqual(r.status_code, expect, r.get_data(as_text=True))
        return r.get_json()

    def stored(self):
        with open(shared_ns.FLEET_MAP_FILE, encoding='utf-8') as f:
            return json.load(f)

    # ── 계약 1: push 가 폐기 필드를 저장하지 않는다 ─────────────
    def test_push_drops_slow_last_notif(self):
        """형이 스크린샷으로 지적한 그 필드 — 저장본에 남으면 화면에 되살아난다."""
        self.push({'slow_last_notif': NOTIF})
        item = self.stored()['fleet'][0]
        self.assertNotIn('slow_last_notif', item)

    def test_push_drops_all_retired_fields(self):
        """옛 캐시에 남아 있던 `slow_eta`/`slow_next_port` 까지 같이 버린다."""
        self.push({'slow_last_notif': NOTIF, 'slow_eta': '2026-08-20 06:00',
                   'slow_next_port': 'SINGAPORE'})
        item = self.stored()['fleet'][0]
        for k in RETIRED_FIELDS:
            self.assertNotIn(k, item)

    def test_retired_fields_absent_from_stored_payload_text(self):
        """필드명 자체가 저장본 어디에도 안 남는다(중첩 어딘가에 살아있지 않은지)."""
        self.push({'slow_last_notif': NOTIF, 'slow_eta': 'x', 'slow_next_port': 'y'})
        blob = json.dumps(self.stored(), ensure_ascii=False)
        for k in RETIRED_FIELDS:
            self.assertNotIn(k, blob)
        self.assertNotIn('Tanjung Pelepas', blob)   # 값도 안 남았다

    def test_retired_fields_absent_from_data_api(self):
        """대시보드가 읽는 경로(`/api/fleet-map/data`) 응답에도 없다."""
        self.push({'slow_last_notif': NOTIF})
        r = self.client.get('/api/fleet-map/data')
        self.assertEqual(r.status_code, 200)
        blob = json.dumps(r.get_json(), ensure_ascii=False)
        for k in RETIRED_FIELDS:
            self.assertNotIn(k, blob)
        self.assertNotIn('Tanjung Pelepas', blob)

    # ── negative control: 다른 필드는 그대로 살아야 한다 ────────
    def test_push_keeps_non_retired_fields(self):
        """폐기 필드만 버린다 — 옆 필드까지 날리면 지도가 조용히 빈다."""
        res = self.push({'slow_last_notif': NOTIF, 'status': 'UNDER WAY',
                         'course': 123, 'speed': 11.4,
                         'next_port': {'name': 'SINGAPORE', 'cd': 'SGSIN'}})
        self.assertEqual(res['count'], 1)
        item = self.stored()['fleet'][0]
        self.assertEqual(item['status'], 'UNDER WAY')
        self.assertEqual(item['course'], 123)
        self.assertEqual(item['speed'], 11.4)
        self.assertEqual(item['next_port']['name'], 'SINGAPORE')
        self.assertEqual(item['lat'], 1.0)
        self.assertEqual(item['lng'], 2.0)

    def test_push_without_retired_fields_still_works(self):
        """폐기 필드가 아예 없는 정상 payload(현재 맥 러너)도 그대로 통과."""
        res = self.push({'status': 'MOORED'})
        self.assertEqual(res['count'], 1)
        self.assertEqual(self.stored()['fleet'][0]['status'], 'MOORED')

    # ── 계약 3: 렌더가 제거됐다 ────────────────────────────────
    def test_dashboard_template_does_not_render_slow_notif(self):
        """템플릿 가드 — 서버가 필드를 버려도 렌더 코드가 남아 있으면 부활 통로가 남는다."""
        with open(os.path.join(REPO, 'templates', 'dashboard.html'), encoding='utf-8') as f:
            html = f.read()
        for k in RETIRED_FIELDS:
            self.assertNotIn('v.' + k, html)
        self.assertNotIn('slowSec', html)
        self.assertNotIn('Slow · 최신 동정', html)

    # ── 계약 4: 맥 파이프라인 wiring ───────────────────────────
    @unittest.skipUnless(os.path.isdir(WORKSPACE_FLEET_MAP),
                         'workspace 없음(서버/CI) — 맥 파이프라인 가드는 SKIP')
    def test_mac_pipeline_no_longer_runs_slow_overlay(self):
        """run.sh·crawl.sh 가 폐기 스크립트를 **실행**하지 않는다(주석 언급은 허용)."""
        for name in ('run.sh', 'crawl.sh'):
            with open(os.path.join(WORKSPACE_FLEET_MAP, name), encoding='utf-8') as f:
                lines = [ln for ln in f.read().splitlines()
                         if ln.strip() and not ln.strip().startswith('#')]
            body = '\n'.join(lines)
            with self.subTest(script=name):
                self.assertNotIn('slow_overlay.py', body)
                self.assertNotIn('slowspace', body)

    @unittest.skipUnless(os.path.isdir(WORKSPACE_FLEET_MAP),
                         'workspace 없음(서버/CI) — 맥 파이프라인 가드는 SKIP')
    def test_mac_pipeline_still_prunes_before_push(self):
        """🔴 push 안전망 이관 확인 — 이게 빠지면 선위 없는 배 1척에 payload 전체가 400 거부된다.

        `slow_overlay.py` 가 겸하던 no-position drop / count 재계산을 `prune_push.py` 가
        받았다. 두 진입점(30분 run.sh, 4회/일 crawl.sh) 모두 **push 전에** 돌아야 한다.
        """
        prune = os.path.join(WORKSPACE_FLEET_MAP, 'prune_push.py')
        self.assertTrue(os.path.exists(prune), 'prune_push.py 가 없다 — 안전망 소실')
        src = open(prune, encoding='utf-8').read()
        self.assertIn('payload["count"] = len(keep)', src)
        for name in ('run.sh', 'crawl.sh'):
            with open(os.path.join(WORKSPACE_FLEET_MAP, name), encoding='utf-8') as f:
                lines = [ln for ln in f.read().splitlines()
                         if ln.strip() and not ln.strip().startswith('#')]
            with self.subTest(script=name):
                idx_prune = next((i for i, ln in enumerate(lines) if 'prune_push.py' in ln), None)
                idx_push = next((i for i, ln in enumerate(lines)
                                 if re.search(r'fleet-map/push', ln)), None)
                self.assertIsNotNone(idx_prune, f'{name} 이 prune_push.py 를 안 돌린다')
                self.assertIsNotNone(idx_push, f'{name} 에서 push 호출을 못 찾았다')
                self.assertLess(idx_prune, idx_push, f'{name}: prune 이 push 뒤에 있다')
                # 🔴 fail-closed(올마이트 지적) — 안전망 실패를 삼키면 정리 안 된 payload 가
                #    그대로 push 돼 서버에서 payload 전체가 400 거부된다.
                self.assertNotIn('||', lines[idx_prune],
                                 f'{name}: prune 실패가 삼켜진다(fail-open)')
                self.assertTrue(any(ln.strip() == 'set -e' for ln in lines),
                                f'{name}: set -e 가 없어 prune 실패가 push 를 못 막는다')

    @unittest.skipUnless(os.path.isdir(WORKSPACE_FLEET_MAP),
                         'workspace 없음(서버/CI) — 맥 파이프라인 가드는 SKIP')
    def test_prune_drop_condition_matches_vt_overlay_rescue_condition(self):
        """🔴 두 조건의 **의미**가 같은지 진리표로 검증(문자열 존재 검사로는 부족 — 올마이트 지적).

        `vt_overlay.py` 는 묵은 AIS 라도 '선위가 아예 없는 선박'은 최종수신 좌표로 구제한다
        (Cyprus Prosperity 실사고). 그 구제 조건(`no_pos`)과 prune 의 drop 조건이 어긋나면
        구제된 배가 여기서 도로 잘려 지도에서 사라진다.
        """
        vt = open(os.path.join(WORKSPACE_FLEET_MAP, 'vt_overlay.py'), encoding='utf-8').read()
        m = re.search(r'^\s*no_pos = (.+)$', vt, re.M)
        self.assertIsNotNone(m, 'vt_overlay.py 에서 no_pos 조건을 못 찾았다')
        no_pos_expr = m.group(1).strip()
        prune = open(os.path.join(WORKSPACE_FLEET_MAP, 'prune_push.py'), encoding='utf-8').read()
        k = re.search(r'^\s*keep = \[v for v in fl if isinstance\(v, dict\)\s*$\n'
                      r'\s*and (.+?)\]\s*$', prune, re.M)
        self.assertIsNotNone(k, 'prune_push.py 에서 keep 조건을 못 찾았다')
        keep_expr = k.group(1).strip()
        for lat in (1.0, None):
            for lng in (2.0, None):
                vsl = {'lat': lat, 'lng': lng}
                with self.subTest(lat=lat, lng=lng):
                    no_pos = eval(no_pos_expr, {}, {'vsl': vsl})       # noqa: S307 (테스트 전용)
                    keep = eval(keep_expr, {}, {'v': vsl})             # noqa: S307
                    self.assertEqual(no_pos, not keep,
                                     f'구제조건과 drop 조건 불일치: {vsl}')

    # ── 계약 5: prune_push.py 실동작 (subprocess 로 실제 실행) ──
    def run_prune(self, payload):
        """prune_push.py 사본을 격리 디렉터리에서 실제 실행 → (rc, 결과 payload)."""
        d = tempfile.mkdtemp(dir=self.tmp.name)
        os.makedirs(os.path.join(d, 'out'))
        shutil.copy(os.path.join(WORKSPACE_FLEET_MAP, 'prune_push.py'), d)
        target = os.path.join(d, 'out', 'fleet_enriched.json')
        if payload is not None:
            with open(target, 'w', encoding='utf-8') as f:
                f.write(payload if isinstance(payload, str)
                        else json.dumps(payload, ensure_ascii=False))
        py = '/usr/bin/python3' if os.path.exists('/usr/bin/python3') else sys.executable
        p = subprocess.run([py, os.path.join(d, 'prune_push.py')],
                           capture_output=True, text=True)
        out = None
        if os.path.exists(target):
            with open(target, encoding='utf-8') as f:
                raw = f.read()
            try:
                out = json.loads(raw)
            except ValueError:
                out = raw            # 손상 payload 는 원문 그대로 돌려준다(안 건드렸는지 비교용)
        return p.returncode, out, (p.stdout + p.stderr)

    @unittest.skipUnless(os.path.isdir(WORKSPACE_FLEET_MAP), 'workspace 없음 — SKIP')
    def test_prune_drops_no_position_vessels_and_retired_fields(self):
        rc, out, log = self.run_prune({'count': 99, 'slow_overlay_at': '2026-08-14 09:00', 'fleet': [
            {'name': 'A', 'lat': 1.0, 'lng': 2.0, 'slow_last_notif': NOTIF, 'status': 'UNDER WAY'},
            {'name': 'B', 'lat': None, 'lng': 3.0, 'slow_eta': 'x'},
            {'name': 'C', 'lat': 4.0, 'lng': None},
            {'name': 'D', 'lat': 5.0, 'lng': 6.0, 'slow_next_port': 'SGSIN'},
        ]})
        self.assertEqual(rc, 0, log)
        self.assertEqual([v['name'] for v in out['fleet']], ['A', 'D'])
        self.assertEqual(out['count'], 2)                    # count 재계산
        self.assertEqual(out['fleet'][0]['status'], 'UNDER WAY')   # 옆 필드 보존
        self.assertIn('pruned_at', out)
        blob = json.dumps(out, ensure_ascii=False)
        for k in RETIRED_FIELDS + ('slow_overlay_at',):
            self.assertNotIn(k, blob)
        self.assertNotIn('Tanjung Pelepas', blob)

    @unittest.skipUnless(os.path.isdir(WORKSPACE_FLEET_MAP), 'workspace 없음 — SKIP')
    def test_prune_fails_closed_on_malformed_json(self):
        """🔴 정리 못 한 payload 를 통과시키면 서버가 전체 400 → 비0 종료여야 한다."""
        rc, out, log = self.run_prune('{"fleet": [ this is not json')
        self.assertNotEqual(rc, 0, log)
        self.assertEqual(out, '{"fleet": [ this is not json')    # 원본 안 건드림

    @unittest.skipUnless(os.path.isdir(WORKSPACE_FLEET_MAP), 'workspace 없음 — SKIP')
    def test_prune_fails_closed_on_invalid_fleet(self):
        for bad in ({'fleet': None}, {'fleet': {'a': 1}}, {'generated_at': 'x'}, [1, 2, 3]):
            with self.subTest(payload=bad):
                rc, _, log = self.run_prune(bad)
                self.assertNotEqual(rc, 0, log)

    @unittest.skipUnless(os.path.isdir(WORKSPACE_FLEET_MAP), 'workspace 없음 — SKIP')
    def test_prune_exits_zero_when_payload_file_absent(self):
        """파일이 없으면 정리할 대상도 없다 — 하위 단계(curl / `if -f`)가 스스로 막는다."""
        rc, out, log = self.run_prune(None)
        self.assertEqual(rc, 0, log)
        self.assertIsNone(out)

    @unittest.skipUnless(os.path.isdir(WORKSPACE_FLEET_MAP), 'workspace 없음 — SKIP')
    def test_prune_keeps_all_when_every_vessel_has_position(self):
        """negative control — 정상 payload 에서 아무것도 잘리지 않는다."""
        rc, out, log = self.run_prune({'fleet': [
            {'name': 'A', 'lat': 1.0, 'lng': 2.0},
            {'name': 'B', 'lat': 0.0, 'lng': 0.0},      # 0.0 은 falsy 지만 유효 좌표다
        ]})
        self.assertEqual(rc, 0, log)
        self.assertEqual([v['name'] for v in out['fleet']], ['A', 'B'])
        self.assertEqual(out['count'], 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
