#!/usr/bin/env python3
"""오늘 일정 요약 푸시(`/api/ext/push/calendar-daily`) 계약 테스트.

형 지시(2026-08-06): "하루에 2번(매일 10시, 14시) 캘린더 일정 푸시알람(완료 제외)".

핵심 계약:
  · **완료(completed=1) 제외** — 이게 형이 명시한 유일한 필터다.
  · 멀티데이 일정은 기간에 걸치면 그날도 포함(입거처럼 진행형인 일정).
  · 스코프는 캘린더 화면과 같다(해당 감독 + 공용). `supervisor_id` 없는 계정은 **공용만** —
        푸시는 능동 발송이라 남의 개인일정을 폰으로 밀어내지 않는다.
  · 0건이면 **안 보낸다**(`skipped_empty`). 빈 알림이 매일 2번 오면 형이 알림을 끈다.
  · `event_key = calendar_daily:<uid>:<날짜>:<슬롯>` — 슬롯이 다르면 다른 알림, 같은 슬롯
        재호출은 dedup. 러너가 실패 재시도해도 중복발송이 안 되는 근거다.
  · 오늘이 아닌 날짜 실발송은 400(백필 한 번 = 알림 폭주). dry 에서만 허용.
  · 유효 시간대는 슬롯 ~ 슬롯+3h. **이른 실행도 막는다** — launchd 는 놓친 잡을 깨어날 때
        즉시 돌리므로, 09시 부팅이 10시판을 선점하면 진짜 10시 실행이 dedup 으로 묻힌다.

실행: ~/.venvs/trmt-test/bin/python tests/test_calendar_daily_push.py
"""
import os, sys, tempfile
from datetime import date, timedelta

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

KEY = 'testkey-cal-push'
A.execute("INSERT OR REPLACE INTO api_settings (k,v) VALUES ('api_key',?)", (KEY,))
H = {'X-API-Key': KEY}

TODAY = date.today().strftime('%Y-%m-%d')
TOM = (date.today() + timedelta(days=1)).strftime('%Y-%m-%d')
YEST = (date.today() - timedelta(days=1)).strftime('%Y-%m-%d')


def mkuser(username, sup_id, active=1):
    A.execute("INSERT INTO users (username, password_hash, display_name, supervisor_id, "
              "role, active) VALUES (?,'x',?,?, 'member', ?)",
              (username, username, sup_id, active))
    return A.query("SELECT id FROM users WHERE username=?", (username,), one=True)['id']


def mkdev(uid, active=1):
    A.execute("INSERT INTO ios_device (user_id, token, env, active) VALUES (?,?,?,?)",
              (uid, f'tok{uid}{active}{A.secrets.token_hex(4)}', 'production', active))


def mkev(title, start, sup_id=None, end=None, completed=0, all_day=1, start_time=None):
    A.execute("INSERT INTO calendar_events (supervisor_id, title, start_date, end_date, "
              "all_day, start_time, completed) VALUES (?,?,?,?,?,?,?)",
              (sup_id, title, start, end, all_day, start_time, completed))


A.execute("INSERT OR IGNORE INTO supervisors (id, name) VALUES (1,'감독A'),(2,'감독B')")

U_SUP = mkuser('sup1', 1)          # 형 자리(감독 1)
U_NONE = mkuser('nosup', None)     # supervisor 없는 계정
mkdev(U_SUP)
mkdev(U_NONE)

mkev('미완료 종일', TODAY, sup_id=1)
mkev('완료된 일정', TODAY, sup_id=1, completed=1)
mkev('시각 일정', TODAY, sup_id=1, all_day=0, start_time='10:00')
mkev('멀티데이 입거', YEST, sup_id=1, end=TOM)
mkev('남의 감독 일정', TODAY, sup_id=2)
mkev('공용 일정', TODAY, sup_id=None)
mkev('내일 일정', TOM, sup_id=1)

print('# 1) 항목 선별 — 완료 제외 · 멀티데이 포함 · 스코프')
items = A._calendar_daily_items(1, TODAY)
titles = [r['title'] for r in items]
chk('완료된 일정' not in titles, '완료(completed=1) 제외', titles)
chk('미완료 종일' in titles, '미완료 포함')
chk('멀티데이 입거' in titles, '멀티데이는 기간 중 매일 포함')
chk('공용 일정' in titles, '공용(supervisor_id IS NULL) 포함')
chk('남의 감독 일정' not in titles, '다른 감독 일정 제외', titles)
chk('내일 일정' not in titles, '오늘 아닌 일정 제외')
chk(titles[0] == '시각 일정', '시각 있는 일정이 맨 앞', titles)

pub = [r['title'] for r in A._calendar_daily_items(None, TODAY)]
chk(pub == ['공용 일정'], 'supervisor 없는 계정 = 공용만(fail-closed)', pub)

print('\n# 2) 본문 조립')
body = A._calendar_daily_body(items)
chk(body.startswith('10:00 시각 일정'), '시각 표기 HH:MM', body)
chk('종일 미완료 종일' in body, '종일 표기', body)
many = A._calendar_daily_items(1, TODAY) * 3          # 12건
b2 = A._calendar_daily_body(many)
chk('외 %d건' % (len(many) - A._CAL_PUSH_MAX_ITEMS) in b2, '상한 초과분은 "외 N건"', b2)
chk(len(b2) <= 300, '본문 300자 이내', len(b2))

print('\n# 3) 라우트 — 인증·슬롯 검증')
chk(c.post('/api/ext/push/calendar-daily', json={'slot': '10'}).status_code in (401, 403),
    'API 키 없으면 거절')
chk(c.post('/api/ext/push/calendar-daily', headers=H,
           json={'slot': '9'}).status_code == 400, '미지 슬롯 400')
chk(c.post('/api/ext/push/calendar-daily', headers=H, json={}).status_code == 400,
    'slot 누락 400')
chk(c.post('/api/ext/push/calendar-daily', headers=H,
           json={'slot': '10', 'date': '20260806'}).status_code == 400, 'date 형식 400')
chk(c.post('/api/ext/push/calendar-daily', headers=H,
           json={'slot': '10', 'date': YEST}).status_code == 400,
    '오늘 아닌 날짜 실발송 400(백필 폭주 차단)')
chk(c.post('/api/ext/push/calendar-daily', headers=H,
           json={'slot': '10', 'date': YEST, 'dry': 1}).status_code == 200,
    '오늘 아닌 날짜도 dry 면 허용')

print('\n# 4) dry — 기기 있는 사용자별 1건, 문구·키')
r = c.post('/api/ext/push/calendar-daily', headers=H, json={'slot': '10', 'dry': 1})
j = r.get_json()
chk(r.status_code == 200 and j['ok'], 'dry 200/ok', j)
by_uid = {x['user_id']: x for x in j['results']}
chk(set(by_uid) == {U_SUP, U_NONE}, '활성 기기 있는 사용자만 대상', list(by_uid))
chk(by_uid[U_SUP]['n'] == 4, '감독1 = 4건(미완료·멀티데이·시각·공용)', by_uid[U_SUP])
chk(by_uid[U_SUP]['title'] == '오늘 일정 4건', '10시 제목', by_uid[U_SUP]['title'])
chk(by_uid[U_SUP]['event_key'] == 'calendar_daily:%s:%s:10' % (U_SUP, TODAY),
    'event_key = kind:uid:날짜:슬롯', by_uid[U_SUP]['event_key'])

r14 = c.post('/api/ext/push/calendar-daily', headers=H, json={'slot': '14', 'dry': 1}).get_json()
h14 = {x['user_id']: x for x in r14['results']}[U_SUP]
chk(h14['title'].startswith('오늘 남은 일정'), '14시 제목은 "남은"', h14['title'])
chk(h14['event_key'].endswith(':14'), '슬롯이 다르면 키도 다름(둘 다 발송)', h14['event_key'])

print('\n# 5) 0건이면 발송 안 함')
A.execute("UPDATE calendar_events SET completed=1")
z = c.post('/api/ext/push/calendar-daily', headers=H, json={'slot': '10', 'dry': 1}).get_json()
chk(all(x['n'] == 0 and x.get('reason') == 'skipped_empty' for x in z['results']),
    '전부 완료면 skipped_empty', z['results'])
chk(z['ok'], '0건은 실패가 아니다(ok)', z)
A.execute("UPDATE calendar_events SET completed=0 WHERE title!='완료된 일정'")

# 실행 시각이 슬롯 창 밖이면(지금이 09시대면) 라우트가 정당하게 skip 한다.
# 6번은 발송 계약을 보는 절이라 창을 열어두고, 창 자체는 6-b 에서 원본 함수로 검증한다.
_REAL_WINDOW = A._cal_slot_window_ok
A._cal_slot_window_ok = lambda slot, now_hour: True

print('\n# 6) 실발송 경로 — 대상·딥링크·중복차단 (APNs 는 스텁, 실제 발송 안 함)')
# 🔴 개발맥엔 진짜 APNs 키가 있다. 스텁을 안 물리면 이 테스트가 애플로 실제 요청을 쏜다.
ap = A._push_module()
assert ap is not None, 'apns_push 모듈 없음'
calls = []
ap.load_conf = lambda: {'stub': True}
ap.send = (lambda device_token, payload, env='production', conf=None, push_type='alert',
           priority='10', collapse_id=None, expiration=None, timeout=15:
           (calls.append({'tok': device_token, 'payload': payload, 'collapse': collapse_id}),
            (True, 200, 'ok'))[1])

res = c.post('/api/ext/push/calendar-daily', headers=H, json={'slot': '10'})
j = res.get_json()
sup = {x['user_id']: x for x in j['results']}[U_SUP]
chk(res.status_code == 200 and sup['ok'] and sup['sent'] == 1, '감독1 기기로 1건 발송', j)
chk(len(calls) == 2, '기기 있는 사용자 2명 = 2콜(사용자별 발송)', len(calls))
aps = calls[0]['payload']
chk(aps.get('link') == 'trmt://calendar', '딥링크는 캘린더 탭', aps)
chk(calls[0]['collapse'] == 'cal-%s-10' % TODAY, 'collapse_id = 날짜+슬롯', calls[0]['collapse'])

n_before = len(calls)
dup = c.post('/api/ext/push/calendar-daily', headers=H, json={'slot': '10'}).get_json()
sup2 = {x['user_id']: x for x in dup['results']}[U_SUP]
chk(sup2['ok'] and sup2['sent'] == 0 and sup2['reason'] == 'already_sent',
    '같은 슬롯 재호출 = dedup(러너 재시도가 중복발송 안 됨)', sup2)
chk(len(calls) == n_before, '중복 호출은 APNs 를 아예 안 부름', len(calls))

r14 = c.post('/api/ext/push/calendar-daily', headers=H, json={'slot': '14'}).get_json()
chk({x['user_id']: x for x in r14['results']}[U_SUP]['sent'] == 1,
    '14시 판은 별개 알림으로 나감', r14)

print('\n# 6-b) 실행 시간대 창 — 이른 실행도 막는다(올마이트 지적)')
chk(_REAL_WINDOW('10', 10) and _REAL_WINDOW('10', 12), '10~12시는 10시판 유효')
chk(not _REAL_WINDOW('10', 9), '🔴 09시 조기실행 차단(부팅 직후 missed job)')
chk(not _REAL_WINDOW('10', 13), '13시(+3h)는 폐기 — 경계 포함')
chk(_REAL_WINDOW('14', 14) and not _REAL_WINDOW('14', 17), '14시판도 같은 규칙')
chk(not _REAL_WINDOW('14', 10), '14시판이 10시에 나가지 않음')
A._cal_slot_window_ok = lambda slot, now_hour: False
lj = c.post('/api/ext/push/calendar-daily', headers=H, json={'slot': '10'}).get_json()
chk(lj.get('skipped') == 'out_of_window' and lj.get('ok'),
    '창 밖 실행은 발송 없이 skip 으로 드러남(실패 아님)', lj)
chk(c.post('/api/ext/push/calendar-daily', headers=H,
           json={'slot': '10', 'dry': 1}).get_json()['ok'], 'dry 는 창 검사 안 함(점검용)')
A._cal_slot_window_ok = lambda slot, now_hour: True

print('\n# 6-c) dry 는 문자열 거짓값을 참으로 보지 않는다')
d0 = c.post('/api/ext/push/calendar-daily', headers=H,
            json={'slot': '10', 'dry': '0', 'date': YEST})
chk(d0.status_code == 400, "dry='0' 은 실발송으로 취급(오늘 아닌 날짜라 400)", d0.get_json())
chk(c.post('/api/ext/push/calendar-daily', headers=H,
           json={'slot': '10', 'dry': 'false', 'date': YEST}).status_code == 400,
    "dry='false' 도 실발송")

print('\n# 6-d) 본문 길이 초과 — 꼬리("외 N건")를 지키고 항목을 줄인다')
long_rows = [{'title': '아주 긴 제목 ' * 12, 'all_day': 1, 'start_time': None}] * 20
lb = A._calendar_daily_body(long_rows)
chk(len(lb) <= 300, '300자 이내', len(lb))
chk('외 ' in lb and lb.endswith('건'), '🔴 꼬리 "외 N건" 이 살아남음', lb)

print('\n# 7) 비활성 기기·비활성 계정은 대상 아님')
A.execute("UPDATE ios_device SET active=0 WHERE user_id=?", (U_NONE,))
j2 = c.post('/api/ext/push/calendar-daily', headers=H, json={'slot': '10', 'dry': 1}).get_json()
chk([x['user_id'] for x in j2['results']] == [U_SUP], '비활성 기기 제외', j2['results'])
A.execute("UPDATE users SET active=0 WHERE id=?", (U_SUP,))
j3 = c.post('/api/ext/push/calendar-daily', headers=H, json={'slot': '10', 'dry': 1}).get_json()
chk(j3['results'] == [], '비활성 계정 제외', j3['results'])

print('\n# 8) 알림 종류 등록 — 앱 설정화면이 이 목록을 그린다')
chk('calendar_daily' in A.PUSH_KIND_KEYS, 'calendar_daily 가 PUSH_KINDS 에 있음')
chk(A._push_dispatch('calendar_daily_x', 'k', 't', 'b')['reason'] == 'unknown_kind',
    '미등록 종류는 발송 거부')

print()
if fails:
    print('❌ 실패 %d건: %s' % (len(fails), ', '.join(fails)))
    sys.exit(1)
print('✅ 전부 통과')
