#!/usr/bin/env python3
"""iOS 푸시알림(APNs) 계약 테스트 — 디바이스 등록 · dedup · 실패 시 재시도 가능성.

배경(2026-08-06 형 요청): Dock 발주현황에서 견적제출/발주완료 때 폰 알림을 받고 싶다.
카나리 단계 = 발송 인프라 + 테스트 발송. 이벤트 배선은 다음 단계.

핵심 계약(깨지면 알림이 조용히 사라지거나 두 번 온다):
  · 키 미설정 → 'not_configured' 로 정직하게 실패하고 push_log 를 더럽히지 않음
  · event_key 중복 → 두 번째는 dup(발송 0)
  · 🔴 디바이스가 있었는데 **전부 실패** → claim 해제(다음 폴링 재시도 가능)
       안 풀면 일시적 네트워크 오류 1회가 그 이벤트를 영구 미탐으로 만든다(BV 감시 교훈)
  · 디바이스 0대 → claim 유지(배달할 곳 없는 과거 이벤트가 나중에 폭주하지 않게)
  · 영구 사망 사유(410/BadDeviceToken)만 디바이스 비활성 — 일시 실패로는 안 끔
  · 종류별 off → 그 종류는 발송 대상에서 제외
  · 토큰 upsert — 같은 폰이 재등록해도 행이 늘지 않음

실행: ~/.venvs/trmt-test/bin/python tests/test_ios_push.py
"""
import os, sys, json, tempfile

os.chdir(os.path.expanduser('~/projects/teammanagement'))
sys.path.insert(0, os.getcwd())
DB = tempfile.mktemp(suffix='.db')
os.environ['TRMT_DB'] = DB

import app as A

A.DATABASE = DB
A.app.config['DATABASE'] = DB
A.app.config['TESTING'] = True
A.init_db(drop=False)
A._auto_migrate()

fails = []


def chk(cond, name, extra=''):
    print(('  ok  ' if cond else '  ❌  ') + name + (f' — {extra}' if extra and not cond else ''))
    if not cond:
        fails.append(name)


A.app.app_context().push()
c = A.app.test_client()

KEY = 'testkey-ios-push'
A._ensure_api_table()
A.execute("INSERT OR REPLACE INTO api_settings(k, v) VALUES('api_key', ?)", (KEY,))
HDR = {'X-API-Key': KEY}

UID = A.query("SELECT id FROM users ORDER BY id LIMIT 1", one=True)['id']
with c.session_transaction() as s:
    s['user_id'] = UID
    s['username'] = 'admin'
    s['role'] = 'admin'

TOK = 'a' * 64
TOK2 = 'b' * 64


# ── APNs 스텁: 실제 발송 없이 결과를 마음대로 지정 ──
class Stub:
    def __init__(self):
        self.result = (True, 200, '')
        self.calls = []

    def load_conf(self):
        return {'APNS_KEY_ID': 'K', 'APNS_TEAM_ID': 'T', 'APNS_TOPIC': 'kr.co.sinokor.trmt'}

    def configured(self):
        return True

    def alert_payload(self, title, body, link=None, kind=None, **kw):
        return {'aps': {'alert': {'title': title, 'body': body}}, 'link': link, 'kind': kind}

    def send(self, token, payload, **kw):
        self.calls.append((token, payload, kw))
        return self.result

    def is_dead(self, status, reason):
        # 실제 모듈과 같은 계약(410 만) — 스텁이 더 넓으면 테스트가 틀린 계약을 굳힌다.
        return status == 410


stub = Stub()
_real_module = A._push_module


def use_stub():
    A._push_module = lambda: stub


def use_missing():
    A._push_module = lambda: None


def logrow(ekey):
    return A.query("SELECT * FROM push_log WHERE event_key=?", (ekey,), one=True)


print('# 1) 키 미설정 — 정직하게 실패하고 원장을 더럽히지 않음')
A._push_module = lambda: type('X', (), {
    'load_conf': staticmethod(lambda: (_ for _ in ()).throw(RuntimeError('no key'))),
    'configured': staticmethod(lambda: False)})()
r = A._push_dispatch('test', 'ek-noconf', 'T', 'B')
chk(r['ok'] is False and r['reason'] == 'not_configured', '미설정은 not_configured', r)
chk(logrow('ek-noconf') is None, '미설정이면 push_log claim 안 함')

use_missing()
r = A._push_dispatch('test', 'ek-nomod', 'T', 'B')
chk(r['ok'] is False and r['reason'] == 'module_missing', '모듈 없으면 module_missing', r)

print('# 2) 등록된 종류만 허용')
use_stub()
r = A._push_dispatch('nope_kind', 'ek-badkind', 'T', 'B')
chk(r['ok'] is False and r['reason'] == 'unknown_kind', '미등록 kind 거부', r)

print('# 3) 디바이스 등록 — 검증 · upsert')
r = c.post('/api/ios/device', json={'token': 'zzz'})
chk(r.status_code == 400, 'hex 아닌 토큰 400', r.status_code)
r = c.post('/api/ios/device', json={'token': TOK, 'env': 'weird', 'app_ver': '191'})
chk(r.status_code == 200, '정상 등록 200', r.status_code)
row = A.query("SELECT * FROM ios_device WHERE token=?", (TOK,), one=True)
chk(row is not None and row['env'] == 'production', '이상한 env 는 production 으로 강제', row['env'] if row else None)
chk(row['user_id'] == UID and row['active'] == 1, 'user_id·active 세팅')
c.post('/api/ios/device', json={'token': TOK.upper(), 'env': 'sandbox'})
n = A.query("SELECT COUNT(*) n FROM ios_device", one=True)['n']
chk(n == 1, '같은 토큰 재등록은 행이 안 늘어남(대소문자 정규화)', n)
chk(A.query("SELECT env FROM ios_device WHERE token=?", (TOK,), one=True)['env'] == 'sandbox',
    'env 는 마지막 등록값으로 갱신')

print('# 4) dedup — 같은 event_key 는 1회만')
stub.result = (True, 200, '')
r1 = A._push_dispatch('test', 'ek-dup', 'T', 'B', user_ids=[UID])
r2 = A._push_dispatch('test', 'ek-dup', 'T', 'B', user_ids=[UID])
chk(r1['ok'] and r1['sent'] == 1, '1회차 발송 1건', r1)
chk(r2['ok'] and r2.get('dup') and r2['sent'] == 0, '2회차는 dup·발송 0', r2)
chk(A.query("SELECT COUNT(*) n FROM push_log WHERE event_key='ek-dup'", one=True)['n'] == 1,
    '원장 행도 1개')

print('# 5) 🔴 전부 실패 → claim 해제(재시도 가능)')
stub.result = (False, 503, 'ServiceUnavailable')
r = A._push_dispatch('test', 'ek-fail', 'T', 'B', user_ids=[UID])
chk(r['ok'] is False and r['reason'] == 'all_failed', '전부 실패는 ok=False', r)
chk(logrow('ek-fail') is None, 'claim 이 풀려 다음 폴링에 재시도 가능')
stub.result = (True, 200, '')
r = A._push_dispatch('test', 'ek-fail', 'T', 'B', user_ids=[UID])
chk(r['ok'] and r['sent'] == 1, '같은 event_key 로 재시도하면 이번엔 발송됨', r)
chk(A.query("SELECT active FROM ios_device WHERE token=?", (TOK,), one=True)['active'] == 1,
    '일시 실패(503)로는 디바이스를 끄지 않음')

print('# 6) 디바이스 0대 → claim 유지(과거 이벤트 폭주 방지)')
r = A._push_dispatch('test', 'ek-nodev', 'T', 'B', user_ids=[999999])
chk(r['ok'] and r['sent'] == 0 and r.get('devices') == 0, '대상 0대면 발송 0·ok', r)
chk(logrow('ek-nodev') is not None, '대상 0대면 claim 유지')

print('# 7) 영구 사망 사유만 디바이스 비활성')
stub.result = (False, 410, 'Unregistered')
A._push_dispatch('test', 'ek-dead', 'T', 'B', user_ids=[UID])
chk(A.query("SELECT active, dead_reason FROM ios_device WHERE token=?", (TOK,), one=True)['active'] == 0,
    '410 Unregistered → 비활성')
c.post('/api/ios/device', json={'token': TOK, 'env': 'production'})
row = A.query("SELECT active, dead_reason FROM ios_device WHERE token=?", (TOK,), one=True)
chk(row['active'] == 1 and row['dead_reason'] is None, '재설치 후 재등록하면 되살아남')

print('# 8) 종류별 on/off')
stub.result = (True, 200, '')
r = c.get('/api/ios/notify-prefs')
j = r.get_json()
chk(r.status_code == 200 and j['configured'] is True, 'prefs 조회 200 + configured', j)
chk(all(k['enabled'] for k in j['kinds']), '기본은 전부 on')
chk({k['key'] for k in j['kinds']} == A.PUSH_KIND_KEYS, '종류 목록이 서버 레지스트리와 일치')
r = c.put('/api/ios/notify-prefs', json={'prefs': {'test': False, 'bogus': True}})
chk(r.status_code == 200 and r.get_json()['prefs'] == {'test': 0}, '미등록 키는 버림', r.get_json())
before = len(stub.calls)
r = A._push_dispatch('test', 'ek-off', 'T', 'B', user_ids=[UID])
chk(r['ok'] and r['sent'] == 0 and len(stub.calls) == before, '끈 종류는 발송 안 함', r)
r = A._push_dispatch('dock_ordered', 'ek-on', 'T', 'B', user_ids=[UID])
chk(r['ok'] and r['sent'] == 1, '안 끈 종류는 정상 발송', r)

print('# 9) /api/ext/push — 자동화 창구')
r = c.post('/api/ext/push', json={'kind': 'dock_ordered', 'event_key': 'ek-ext-1',
                                  'title': '발주완료', 'body': 'ATBG R1', 'link': 'trmt://dock'},
           headers=HDR)
chk(r.status_code == 200 and r.get_json()['sent'] == 1, 'ext 발송 200', r.get_json())
r = c.post('/api/ext/push', json={'kind': 'zzz', 'event_key': 'x', 'title': 't'}, headers=HDR)
chk(r.status_code == 400, '미등록 kind 400', r.status_code)
r = c.post('/api/ext/push', json={'kind': 'dock_ordered', 'title': 't'}, headers=HDR)
chk(r.status_code == 400, 'event_key 없으면 400', r.status_code)
r = c.post('/api/ext/push', json={'kind': 'dock_ordered', 'event_key': 'y', 'title': 't'},
           headers={'X-API-Key': 'wrong'})
chk(r.status_code == 401, 'API 키 틀리면 401', r.status_code)

print('# 10) 발송 실패는 502 로 드러남(조용한 성공 위장 금지)')
stub.result = (False, 429, 'TooManyRequests')
r = c.post('/api/ext/push', json={'kind': 'dock_ordered', 'event_key': 'ek-ext-fail',
                                  'title': 't', 'body': 'b'}, headers=HDR)
chk(r.status_code == 502 and r.get_json()['ok'] is False, '전부 실패면 502', r.status_code)

print('# 11) 디바이스 해제')
stub.result = (True, 200, '')
r = c.delete('/api/ios/device', json={'token': TOK})
chk(r.status_code == 200, '해제 200', r.status_code)
chk(A.query("SELECT active FROM ios_device WHERE token=?", (TOK,), one=True)['active'] == 0,
    '해제하면 active=0')

print('# 12) apns_push 모듈 자체 — 미설정 판정 · payload 모양')
A._push_module = _real_module
import apns_push
chk(apns_push.HOSTS['production'] == 'api.push.apple.com', 'production 호스트')
chk(apns_push.HOSTS['sandbox'] == 'api.sandbox.push.apple.com', 'sandbox 호스트')
p = apns_push.alert_payload('제목', '본문', link='trmt://dock', kind='dock_ordered')
chk(p['aps']['alert'] == {'title': '제목', 'body': '본문'} and p['link'] == 'trmt://dock'
    and p['kind'] == 'dock_ordered', 'alert payload 모양', p)
chk(apns_push.is_dead(410, ''), '410 Unregistered = 사망')
# 🔴 400 BadDeviceToken/DeviceTokenNotForTopic 은 "토큰 사망"과 "env/topic 설정 불일치"가 같은 응답이다.
#    설정 문제로 기기를 끄면 형이 눈치채기 전까지 알림이 조용히 끊긴다 → 낭비를 택하고 미탐을 막는다.
chk(not apns_push.is_dead(400, 'BadDeviceToken'), '400 BadDeviceToken 으로는 기기를 끄지 않음')
chk(not apns_push.is_dead(400, 'DeviceTokenNotForTopic'), '400 DeviceTokenNotForTopic 도 안 끔')
chk(not apns_push.is_dead(429, 'TooManyRequests') and not apns_push.is_dead(503, ''),
    '일시 실패는 사망 아님')
chk(apns_push._curl_quote('a"b\\c') == 'a\\"b\\\\c', 'curl config 이스케이프')

print('# 13) 🔴 curl config 주입 — 제어문자는 이스케이프가 아니라 거부')
for bad in ('a\nb', 'a\rb', 'a\x00b', 'a\x7fb'):
    try:
        apns_push._curl_quote(bad)
        chk(False, f'제어문자 거부: {bad!r}')
    except apns_push.APNsBadValue:
        chk(True, f'제어문자 거부: {bad!r}')
# send() 는 예외를 새로 던지지 않고 (False, 0, 사유) 로 돌려준다 — 호출측 계약 유지.
_real_jwt = apns_push.provider_jwt
apns_push.provider_jwt = lambda conf, ttl=2700: 'stub.jwt.sig'
_ran = []
_real_run = apns_push.subprocess.run
apns_push.subprocess.run = lambda *a, **k: (_ran.append(a), _real_run(*a, **k))[1]
CONF = {'APNS_KEY_ID': 'K', 'APNS_TEAM_ID': 'T', 'APNS_TOPIC': 'kr.co.sinokor.trmt'}
ok, st, rs = apns_push.send('a' * 64, {'aps': {}}, conf=CONF, collapse_id='dock\n--output /tmp/pwn')
chk(ok is False and st == 0 and '제어문자' in rs, '주입 시도는 발송 거부', (ok, st, rs))
chk(not _ran, '거부되면 curl 을 아예 실행하지 않음')
ok, st, rs = apns_push.send('zzz', {'aps': {}}, conf=CONF)
chk(ok is False and st == 0 and rs == 'bad device token', 'hex 아닌 토큰은 모듈 단계에서도 거부',
    (ok, st, rs))
chk(not _ran, '토큰 거부도 curl 실행 없음')
apns_push.provider_jwt = _real_jwt
apns_push.subprocess.run = _real_run

print('# 14) 🔴 cap 은 prefs 필터 뒤에 — 상위 N 대가 껐다고 켠 기기가 묻히면 안 됨')
_cap = A._PUSH_DEVICE_CAP
A._PUSH_DEVICE_CAP = 2
CAPTOKS = [('c' * 64, '2026-08-06 10:00:00', '{"dock_quote": 0}'),
           ('d' * 64, '2026-08-06 09:00:00', '{"dock_quote": 0}'),
           ('e' * 64, '2026-08-06 08:00:00', '{"dock_quote": 1}')]
for t, upd, pr in CAPTOKS:
    A.execute("INSERT INTO ios_device (token, user_id, env, active, prefs, updated_at) "
              "VALUES (?,?,'production',1,?,?)", (t, UID, pr, upd))
rows = A._push_devices([UID], kind='dock_quote')
toks = [r['token'] for r in rows]
chk(toks == ['e' * 64], '켜둔 기기가 cap 밖으로 밀려나지 않음(LIMIT 먼저 자르면 0건)', toks)
chk(len(A._push_devices([UID], kind='test')) == 2, 'cap 자체는 여전히 적용됨',
    len(A._push_devices([UID], kind='test')))
A._PUSH_DEVICE_CAP = _cap
A.execute("DELETE FROM ios_device WHERE token IN (?,?,?)", tuple(t for t, _u, _p in CAPTOKS))

print('# 15) 🔴 재설치(토큰 변경) 시 기존 계정 prefs 승계 · 소유자 바뀌면 초기화')
c.put('/api/ios/notify-prefs', json={'prefs': {'dock_quote': False}})
TOK3 = 'f' * 64
r = c.post('/api/ios/device', json={'token': TOK3, 'env': 'production'})
row = A.query("SELECT prefs FROM ios_device WHERE token=?", (TOK3,), one=True)
chk(r.status_code == 200 and json.loads(row['prefs'] or '{}').get('dock_quote') == 0,
    '새 토큰이 껐던 설정을 물려받음(재설치로 조용히 켜지지 않음)', row['prefs'])
UID2 = A.execute("INSERT INTO users (username, password_hash, role) VALUES ('other','x','member')")
UID2 = A.query("SELECT id FROM users WHERE username='other'", one=True)['id']
with c.session_transaction() as s:
    s['user_id'] = UID2
    s['username'] = 'other'
    s['role'] = 'member'
c.post('/api/ios/device', json={'token': TOK3, 'env': 'production'})
row = A.query("SELECT user_id, prefs FROM ios_device WHERE token=?", (TOK3,), one=True)
chk(row['user_id'] == UID2 and not json.loads(row['prefs'] or '{}'),
    '소유자가 바뀌면 이전 사용자 설정이 남지 않음', dict(row))

print('# 16) 🔴 push_log 는 계정 컬럼이 없음 — admin 아니면 title 가림')
r = c.get('/api/ios/push/status')
j = r.get_json()
chk(r.status_code == 200 and j['recent'] and all(x['title'] is None for x in j['recent']),
    'member 에게는 다른 사람 알림 제목이 새지 않음', j.get('recent'))
chk(all(x['kind'] for x in j['recent']), '진단용 kind 는 그대로 남김')
with c.session_transaction() as s:
    s['user_id'] = UID
    s['username'] = 'admin'
    s['role'] = 'admin'
r = c.get('/api/ios/push/status')
chk(any(x['title'] for x in r.get_json()['recent']), 'admin 은 제목까지 봄')

print('# 17) 🔴 /api/ext/push user_ids 3상태 — 빈 배열을 전체발송으로 뒤집지 않음')
r = c.post('/api/ext/push', json={'kind': 'dock_ordered', 'event_key': 'ek-uid-empty',
                                  'title': 't', 'user_ids': []}, headers=HDR)
chk(r.status_code == 400, '빈 배열은 400(전체발송으로 승격 금지)', r.status_code)
chk(logrow('ek-uid-empty') is None, '거절된 요청은 원장을 더럽히지 않음')
r = c.post('/api/ext/push', json={'kind': 'dock_ordered', 'event_key': 'ek-uid-str',
                                  'title': 't', 'user_ids': 'me'}, headers=HDR)
chk(r.status_code == 400, '배열 아니면 400', r.status_code)
r = c.post('/api/ext/push', json={'kind': 'dock_ordered', 'event_key': 'ek-uid-bad',
                                  'title': 't', 'user_ids': ['abc']}, headers=HDR)
chk(r.status_code == 400, '정수 아닌 값 섞이면 500 아니라 400', r.status_code)

print('# 18) 🔴 테스트 발송은 같은 초에 두 번 눌러도 둘 다 나감(난수 event_key)')
use_stub()
stub.result = (True, 200, '')
c.put('/api/ios/notify-prefs', json={'prefs': {}})       # 전 종류 on 으로 복구
c.post('/api/ios/device', json={'token': TOK, 'env': 'production'})
r1 = c.post('/api/ios/push/test')
r2 = c.post('/api/ios/push/test')
j1, j2 = r1.get_json(), r2.get_json()
chk(r1.status_code == 200 and j1.get('sent', 0) >= 1, '1회차 테스트 발송', j1)
chk(r2.status_code == 200 and j2.get('sent', 0) >= 1 and not j2.get('dup'),
    '2회차도 dup 아님(초 단위 키면 dup 으로 묻힘)', j2)
n = A.query("SELECT COUNT(*) n FROM push_log WHERE event_key LIKE 'test:%'", one=True)['n']
chk(n == 2, '원장에 2건', n)

print()
if fails:
    print(f'❌ FAIL {len(fails)}건: {fails}')
    sys.exit(1)
print('✅ 전부 통과')
