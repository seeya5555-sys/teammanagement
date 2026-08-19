#!/usr/bin/env python3
"""`/liscr` 등록 유형(프리셋) + FIX 게이트 계약 테스트.

형 지시(2026-08-19): "기국 인보이스로 제한하지 말고 범용으로. 고정값인 Vendor·Expense·통화를
수동변경 가능하게. 드롭박스에서 기국을 고르면 지금처럼 고정값이 박히고, 타 인보이스를 고르면
고정값이 풀리는 방식."

핵심 계약(여기가 깨지면 잘못된 벤더/통화로 돈 문서가 만들어진다):
  · 🔴 **잠긴 필드는 사람이 뭘 보내도 저장되지 않는다.** 기국 업로드에 vendor_cd 를 실어
        보내도 무시돼야 한다 — 안 그러면 "고정" 이 그냥 기본값으로 내려앉는다.
        (최종 강제선은 맥 러너 `profiles.pick()` 이고, 서버는 애초에 받지도 않는다.)
  · 🔴 **마스터가 없으면 generic 업로드를 안 받는다(fail-closed).** 러너가 목록을 밀기
        전에는 화면에서 형이 뭘 고르는지 볼 수 없다. 그때 동작 = 오늘까지의 동작(기국 전용).
  · 🔴 **HOLD 는 사람이 채워도 승인 불가, FIX 는 채우면 승인 가능.** 한 코드로 뭉치면
        중복 인보이스가 사람 손으로 통과한다.
  · 🔴 **빈 값이 남은 채로는 승인되지 않는다.** FIX 카드는 비어 있는 게 정상이라,
        완결성 검사가 없으면 러너가 SVMS 앞에서 터진다(그땐 이미 사람 손을 떠났다).
  · 🔴 **빈 마스터 push 는 기존 스냅샷을 덮지 않는다.** SVMS 조회가 반쯤 실패한 회차가
        멀쩡한 목록을 0건으로 밀면 기능이 조용히 죽는다.
  · 통화는 잠기지 않은 프리셋에서만 카드에서 고칠 수 있고, ISO 3자리만 받는다.

실행: ~/.venvs/trmt-test/bin/python tests/test_liscr_profile.py
"""
import io
import json
import os
import sys
import tempfile

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root (clone 위치 무관)
sys.path.insert(0, os.getcwd())
DB = tempfile.mktemp(suffix='.db')
os.environ['TRMT_DB'] = DB

import app as A  # noqa: E402

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

A.execute("INSERT OR IGNORE INTO supervisors (id, name) VALUES (1,'감독A')")
uid = A.execute("INSERT INTO users (username, password_hash, display_name, supervisor_id, "
                "role, active) VALUES ('admin1','x','admin1',1,'admin',1)")
with c.session_transaction() as s:
    s['user_id'] = uid
    s['role'] = 'admin'

import app_core  # noqa: E402
PDF_DIR = app_core.LISCR_PDF_DIR
os.makedirs(PDF_DIR, exist_ok=True)

A.execute("INSERT OR REPLACE INTO api_settings (k, v) VALUES ('api_key', ?)", ('secret',))
HDR = {'X-API-Key': 'secret'}

# 러너가 밀어주는 것과 같은 모양의 프리셋 레지스트리(맥 profiles.public_list() 산출물).
PROFILES = [
    {'key': 'liscr', 'label': '기국 (LISCR)', 'hint': '고정', 'locked': ['vendor', 'expense', 'currency'],
     'vendor_cd': 'V25081', 'exp_cd': '070205', 'cur_cd': 'USD', 'soft_fill': False},
    {'key': 'generic', 'label': '기타 인보이스', 'hint': '직접 지정', 'locked': [],
     'vendor_cd': None, 'exp_cd': None, 'cur_cd': None, 'soft_fill': True},
]
MASTER = {
    'profiles': PROFILES,
    'vessels': [{'cd': 'V001', 'nm': 'KUWAIT PROSPERITY'}, {'cd': 'V002', 'nm': 'SAP'}],
    'expenses': [{'cd': '070205', 'nm': 'TAX/OTHERS'}, {'cd': '010101', 'nm': 'LUB OIL'}],
    'vendors': [{'cd': 'V25081', 'nm': 'LISCR', 'n': 9}, {'cd': 'V99999', 'nm': 'ACME', 'n': 3}],
    'currencies': [{'cd': 'USD', 'n': 100}, {'cd': 'EUR', 'n': 4}],
}

_n = [0]


def upload(profile=None, **form):
    """PDF 1건 업로드. 매번 내용을 바꿔 sha256 중복선에 걸리지 않게 한다."""
    _n[0] += 1
    data = {'file': (io.BytesIO(b'%%PDF-1.4 profile-test-%d' % _n[0]), 'a.pdf')}
    if profile:
        data['profile'] = profile
    data.update(form)
    return c.post('/api/liscr/upload', data=data, content_type='multipart/form-data')


def job(jid):
    return A.query("SELECT * FROM liscr_job WHERE id=?", (jid,), one=True)


print('# 1) 마스터 없을 때 = 오늘까지의 동작(기국만) — fail-closed')
r = upload()
chk(r.status_code == 200, '프리셋 미지정 업로드 = 200(기국 기본값)', r.get_json())
chk(job(r.get_json()['id'])['profile'] == 'liscr', 'profile 기본값 = liscr')
r = upload(profile='generic', vendor_cd='V99999', exp_cd='010101')
chk(r.status_code == 400, '🔴 마스터 없으면 generic 업로드 = 400', r.status_code)
chk('등록 유형' in (r.get_json().get('error') or ''), '거부 사유가 등록 유형', r.get_json())

j = c.get('/api/liscr/master').get_json()
chk([p['key'] for p in j['profiles']] == ['liscr'],
    '마스터 전에는 화면에도 기국만 노출', j['profiles'])

print('\n# 2) 마스터 push — 러너 전용(api_key)이고 빈 목록은 안 받는다')
chk(c.post('/api/ext/liscr/master', json=MASTER).status_code in (401, 403),
    '키 없는 마스터 push 거부')
r = c.post('/api/ext/liscr/master', json=MASTER, headers=HDR)
chk(r.status_code == 200, '마스터 push = 200', r.get_json())
chk(r.get_json()['saved'].get('vendors') == 2, '벤더 2건 저장', r.get_json())

r = c.post('/api/ext/liscr/master',
           json={'profiles': [], 'vessels': [], 'expenses': [], 'vendors': [], 'currencies': []},
           headers=HDR)
chk(r.get_json()['saved'] == {}, '🔴 빈 목록은 하나도 저장 안 함', r.get_json())
chk(sorted(r.get_json()['skipped']) == ['currencies', 'expenses', 'profiles', 'vendors', 'vessels'],
    '건너뛴 종류를 알려줌', r.get_json())
j = c.get('/api/liscr/master').get_json()
chk(len(j['vendors']) == 2 and len(j['profiles']) == 2,
    '🔴 기존 스냅샷이 빈 push 로 안 지워짐', {k: len(v) for k, v in j.items() if isinstance(v, list)})

print('\n# 3) 잠긴 값은 사람이 보내도 무시된다 — "고정" 의 의미')
r = upload(profile='liscr', vendor_cd='V99999', exp_cd='010101', cur_cd='EUR')
row = job(r.get_json()['id'])
chk(r.status_code == 200, '기국 업로드 = 200', r.get_json())
chk(row['vndr_cd'] is None and row['exp_cd'] is None and row['cur_cd'] is None,
    '🔴 잠긴 필드는 저장 자체가 안 됨(러너가 고정값을 박는다)',
    dict(vndr=row['vndr_cd'], exp=row['exp_cd'], cur=row['cur_cd']))

print('\n# 4) 잠기지 않은 프리셋 — 형식 검사 후 그대로 저장')
# 🔴 Vendor·Expense 는 **선택**이다(2026-08-19 형 지시, §14). 업로드에서 필수로 되돌리면
#    벤더가 섞인 묶음을 한 번에 올리는 길이 도로 닫힌다.
chk(upload(profile='generic', exp_cd='010101').status_code == 200, 'Vendor 없어도 업로드됨')
chk(upload(profile='generic', vendor_cd='V99999').status_code == 200, 'Expense 없어도 업로드됨')
chk(upload(profile='generic', vendor_cd='V9 99', exp_cd='010101').status_code == 400,
    '코드 형식 이상하면 400')
chk(upload(profile='nosuch', vendor_cd='V99999', exp_cd='010101').status_code == 400,
    '모르는 프리셋 = 400(기본값으로 안 때움)')
r = upload(profile='generic', vendor_cd='V99999', exp_cd='010101', cur_cd='AUTO', vsl_cd='V001')
row = job(r.get_json()['id'])
chk(r.status_code == 200, 'generic 업로드 = 200', r.get_json())
chk((row['vndr_cd'], row['exp_cd'], row['cur_cd'], row['vsl_cd'], row['profile'])
    == ('V99999', '010101', 'AUTO', 'V001', 'generic'), '고른 값이 그대로 저장됨', dict(row))

print('\n# 5) 러너 회신 — FIX 는 승인 대기줄, HOLD 는 보류')
GJ = r.get_json()['id']


def claim_and_report(jid, gate, header, lines, hard=(), reasons=()):
    A.execute("UPDATE liscr_job SET status='parsing', claim_token='tok%d' WHERE id=?" % jid, (jid,))
    return c.post('/api/ext/liscr/jobs/%d/parsed' % jid, headers=HDR, json={
        'claim_token': 'tok%d' % jid, 'gate': gate, 'header': header, 'lines': lines,
        'hard': list(hard), 'reasons': list(reasons or hard), 'parsed': {}})


FULL = {'VSL_CD': 'V001', 'VSL_NM': 'KUWAIT PROSPERITY', 'VNDR_CD': 'V99999', 'VNDR_NM': 'ACME',
        'SUP_USER_ID': 'SS0094', 'INV_NO': 'ACME-1', 'INV_DT': '20260801', 'CUR_CD': 'EUR',
        'AMT': 100.0, 'PAY_DT': '20260930'}
LINES = [{'EXP_CD': '010101', 'EXP_NM': 'LUB OIL', 'SUBJ': 'lube', 'AMT': 100.0}]

r = claim_and_report(GJ, 'FIX', dict(FULL, CUR_CD=None), LINES, reasons=['통화 미입력'])
chk(r.status_code == 200 and r.get_json()['gate'] == 'FIX', 'FIX 회신 = 200', r.get_json())
row = job(GJ)
chk(row['status'] == 'parsed', '🔴 FIX 는 hold 가 아니라 승인 대기줄(parsed)', row['status'])
chk(row['vndr_cd'] == 'V99999' and row['vndr_nm'] == 'ACME' and row['exp_nm'] == 'LUB OIL',
    '벤더·Expense 이름이 카드용으로 저장됨', dict(row))

HJ = upload(profile='generic', vendor_cd='V99999', exp_cd='010101').get_json()['id']
claim_and_report(HJ, 'HOLD', FULL, LINES, hard=['인보이스 번호 중복: ACME-1'])
chk(job(HJ)['status'] == 'hold', 'HOLD 는 보류', job(HJ)['status'])
chk(json.loads(job(HJ)['hard_json']) == ['인보이스 번호 중복: ACME-1'],
    'hard 사유가 따로 저장됨', job(HJ)['hard_json'])

print('\n# 6) 승인 게이트 — HOLD 는 못 넘고, FIX 는 빈칸을 채워야 넘는다')
r = c.post('/api/liscr/jobs/%d/approve' % HJ, json={})
chk(r.status_code == 409, '🔴 HOLD 승인 = 409', r.status_code)
chk('중복' in (r.get_json().get('error') or ''), '거부 사유에 hard 이유가 실림', r.get_json())
chk(job(HJ)['status'] == 'hold', 'HOLD 행은 그대로')

r = c.post('/api/liscr/jobs/%d/approve' % GJ, json={})
chk(r.status_code == 400, '🔴 통화 빈 채로 승인 = 400', r.status_code)
chk('통화' in ' '.join(r.get_json().get('missing') or []), '뭘 채워야 하는지 알려줌', r.get_json())
chk(job(GJ)['status'] == 'parsed', '거부돼도 승인 대기줄에 남음')

r = c.post('/api/liscr/jobs/%d/approve' % GJ, json={'cur_cd': 'eur'})
chk(r.status_code == 200, '통화를 채우면 승인 = 200', r.get_json())
row = job(GJ)
chk(row['status'] == 'approved' and row['cur_cd'] == 'EUR', '통화가 대문자로 저장됨', dict(row))
chk(json.loads(row['header_json'])['CUR_CD'] == 'EUR', '헤더에도 반영 — 러너가 이걸 쓴다')

print('\n# 7) 통화 — 형식 검사, 그리고 기국에서는 아예 안 열린다')
G2 = upload(profile='generic', vendor_cd='V99999', exp_cd='010101').get_json()['id']
claim_and_report(G2, 'FIX', dict(FULL, CUR_CD=None), LINES, reasons=['통화 미입력'])
chk(c.post('/api/liscr/jobs/%d/approve' % G2, json={'cur_cd': 'AUTO'}).status_code == 400,
    "'AUTO' 는 통화가 아님 = 400")
chk(c.post('/api/liscr/jobs/%d/approve' % G2, json={'cur_cd': 'US'}).status_code == 400,
    '2자리 통화 = 400')

L2 = upload(profile='liscr').get_json()['id']
claim_and_report(L2, 'READY', dict(FULL, VNDR_CD='V25081', CUR_CD='USD'),
                 [dict(LINES[0], EXP_CD='070205')])
r = c.post('/api/liscr/jobs/%d/approve' % L2, json={'cur_cd': 'EUR'})
chk(r.status_code == 200, '기국 승인 자체는 통과', r.get_json())
chk(job(L2)['cur_cd'] == 'USD' and json.loads(job(L2)['header_json'])['CUR_CD'] == 'USD',
    '🔴 기국은 통화 수정을 무시한다(고정)', job(L2)['cur_cd'])

print('\n# 8) 선박 — 기국은 못 고치고, generic 은 마스터에 있는 코드만')
G3 = upload(profile='generic', vendor_cd='V99999', exp_cd='010101').get_json()['id']
claim_and_report(G3, 'READY', FULL, LINES)
chk(c.post('/api/liscr/jobs/%d/approve' % G3, json={'vsl_cd': 'ZZZZ'}).status_code == 400,
    '마스터에 없는 선박코드 = 400')
r = c.post('/api/liscr/jobs/%d/approve' % G3, json={'vsl_cd': 'V002'})
chk(r.status_code == 200 and job(G3)['vsl_nm'] == 'SAP',
    '선박 바꾸면 이름도 마스터에서 같이 채움', dict(job(G3)))

L3 = upload(profile='liscr').get_json()['id']
claim_and_report(L3, 'READY', dict(FULL, VNDR_CD='V25081', CUR_CD='USD'),
                 [dict(LINES[0], EXP_CD='070205')])
c.post('/api/liscr/jobs/%d/approve' % L3, json={'vsl_cd': 'V002'})
chk(job(L3)['vsl_cd'] == 'V001', '🔴 기국은 선박 수정을 무시한다(PDF 가 권위)', job(L3)['vsl_cd'])

print('\n# 10) 올마이트 지적 반영(2026-08-19) — 통화 실재검증 · 프리셋만 밀린 상태')
r = upload(profile='generic', vendor_cd='V99999', exp_cd='010101', cur_cd='XXX')
chk(r.status_code == 400 and '최근 사용 목록' in (r.get_json().get('error') or ''),
    '🔴 마스터에 없는 통화는 업로드에서 거부(오타 3자리 차단)', r.get_json())
chk(upload(profile='generic', vendor_cd='V99999', exp_cd='010101',
           cur_cd='eur').status_code == 200, '소문자 통화는 대문자로 정규화되어 통과')

L10 = upload(profile='generic', vendor_cd='V99999', exp_cd='010101', cur_cd='AUTO',
             vsl_cd='V001').get_json()['id']
claim_and_report(L10, 'FIX', dict(FULL, CUR_CD=None), LINES, hard=[], reasons=['통화 미판독'])
r = c.post('/api/liscr/jobs/%d/approve' % L10, json={'cur_cd': 'XXX'})
chk(r.status_code == 400 and '최근 사용 목록' in (r.get_json().get('error') or ''),
    '🔴 승인에서도 마스터에 없는 통화 거부', r.get_json())
r = c.post('/api/liscr/jobs/%d/approve' % L10, json={'cur_cd': 'AUTO'})
chk(r.status_code == 400, "🔴 'AUTO' 는 지시어지 통화가 아니다 — 승인에서는 거부", r.get_json())
r = c.post('/api/liscr/jobs/%d/approve' % L10, json={'cur_cd': 'EUR'})
chk(r.status_code == 200 and job(L10)['cur_cd'] == 'EUR', 'FIX 카드에서 통화를 채워 승인',
    r.get_json())

# 프리셋은 SVMS 호출 없이 만들어지는 순수 데이터라, SVMS 조회가 통째로 실패한 회차에도
# 혼자 저장될 수 있다. 그 상태에서 generic 을 열면 목록 없는 화면에서 코드를 손으로 쳐야 한다.
A.execute("DELETE FROM liscr_master WHERE kind='expenses'")
r = upload(profile='generic', vendor_cd='V99999', exp_cd='010101')
chk(r.status_code == 400, '🔴 데이터 마스터 한 종류라도 비면 generic 다시 잠김(fail-closed)',
    r.status_code)
chk([p['key'] for p in c.get('/api/liscr/master').get_json()['profiles']] == ['liscr'],
    '그때 화면에도 기국만 노출')
chk(upload(profile='liscr').status_code == 200, '그 상태에서도 기국 업로드는 된다')
c.post('/api/ext/liscr/master', json=MASTER, headers=HDR)   # 원상복구

print('\n# 9) 마스터 age — 러너가 다시 뜰지 판단하는 신호')
r = c.get('/api/ext/liscr/master/age', headers=HDR)
chk(r.status_code == 200 and r.get_json()['age'] is not None, 'age 조회 = 숫자', r.get_json())
A.execute("DELETE FROM liscr_master WHERE kind='vendors'")
chk(c.get('/api/ext/liscr/master/age', headers=HDR).get_json()['age'] is None,
    '🔴 한 종류라도 없으면 age=None(러너가 무조건 다시 뜬다)')
chk(c.get('/api/ext/liscr/master/age').status_code in (401, 403), '키 없는 age 조회 거부')
c.post('/api/ext/liscr/master', json=MASTER, headers=HDR)   # 원상복구

print('\n# 11) 전부 못 읽은 카드를 손으로 채워 승인 (2026-08-19 형 지시)')
# 형 지시: "진짜 못읽어오는거면 모든 정보를 수동으로 입력 가능하게 해주면 되지(읽어올수 있는거만 입력)".
# 자유서식에서 파서가 금액 말고 아무것도 못 읽은 상태 = 잡 #16(뷰로베리타스) 의 모습이다.
# 🔴 그 벤더는 SVMS 마스터에 PAY_TERM 이 아예 없어(실측) Pay Date 가 자동으로 안 나온다.
#    러너가 이걸 HOLD 로 올리던 동안에는, 형이 화면에서 뭘 채워도 승인이 409 로 막혔다.
BLANK = {'VSL_CD': None, 'VSL_NM': None, 'VNDR_CD': 'V99999', 'VNDR_NM': 'ACME',
         'SUP_USER_ID': None, 'INV_NO': None, 'INV_DT': None, 'CUR_CD': 'USD',
         'AMT': 9184.0, 'PAY_DT': None, 'PAY_TERM': None}
EMPTY_LINE = [{'EXP_CD': '010101', 'EXP_NM': 'LUB OIL', 'SUBJ': '', 'AMT': 9184.0}]
B1 = upload(profile='generic', vendor_cd='V99999', exp_cd='010101').get_json()['id']
claim_and_report(B1, 'FIX', BLANK, EMPTY_LINE, reasons=[
    'Vendor V99999 에 PAY_TERM 이 없어 Pay Date 를 자동 산출할 수 없음 — Remit 날짜를 직접 입력할 것',
    'Invoice No 미판독', 'Invoice Date 미판독', '적요(Subject) 미판독 — 화면에서 입력 필요'])
chk(job(B1)['status'] == 'parsed', '🔴 다 못 읽은 자유서식도 승인 대기줄에 선다', job(B1)['status'])

r = c.post('/api/liscr/jobs/%d/approve' % B1, json={})
miss = r.get_json().get('missing') or []
chk(r.status_code == 400, '빈 채로 승인은 여전히 거부', r.get_json())
chk(all(x in ' '.join(miss) for x in ('선박', 'Superintendent', 'Invoice No', 'Invoice Date',
                                      'Pay Date', '적요')),
    '🔴 못 채운 항목을 **전부** 알려준다(하나씩 되풀이하지 않게)', miss)

FILLED = {'vsl_cd': 'V001', 'sup_user_id': 'SS0094', 'inv_no': '26010243 RI 00058',
          'inv_dt': '20260812', 'pay_dt': '20260911', 'amt': '9184.00', 'subject': 'BV 검사비'}
chk(c.post('/api/liscr/jobs/%d/approve' % B1,
           json=dict(FILLED, inv_dt='2026-08-12')).status_code == 400,
    '🔴 YYYYMMDD 아닌 날짜 = 400(러너 앞에서 터지기 전에 막는다)')
chk(c.post('/api/liscr/jobs/%d/approve' % B1,
           json=dict(FILLED, pay_dt='20260231')).status_code == 400,
    '🔴 2월 31일 같은 없는 날짜 = 400')
chk(job(B1)['status'] == 'parsed', '거부돼도 승인 대기줄에 남음', job(B1)['status'])

r = c.post('/api/liscr/jobs/%d/approve' % B1, json=FILLED)
chk(r.status_code == 200, '모든 값을 손으로 채우면 승인 = 200', r.get_json())
row, hdr = job(B1), json.loads(job(B1)['header_json'])
chk(row['status'] == 'approved', '승인됨', row['status'])
chk((hdr['VSL_CD'], hdr['VSL_NM'], hdr['SUP_USER_ID'], hdr['INV_NO'], hdr['INV_DT'],
     hdr['PAY_DT'], hdr['AMT']) ==
    ('V001', 'KUWAIT PROSPERITY', 'SS0094', '26010243 RI 00058', '20260812', '20260911', 9184.0),
    '🔴 손으로 채운 값이 러너가 쓸 헤더에 그대로 들어감', hdr)
chk(json.loads(row['lines_json'])[0]['SUBJ'] == 'BV 검사비', '적요는 라인에 실린다',
    row['lines_json'])
chk(hdr.get('PAY_TERM') is None,
    '🔴 PAY_TERM 은 없는 채로 둔다 — 서버는 SVMS 공식을 모른다(러너가 Remit 에서 역산한다)',
    hdr.get('PAY_TERM'))

print('\n# 12) 날짜 검증은 편집값이 아니라 **최종 헤더**에 걸린다 (올마이트 지적 반영)')
# 🔴 요청에 실린 값만 보면, 러너가 이미 이상한 날짜를 올려둔 카드는 그 필드를 안 고치고
#    승인하는 것만으로 통과한다. 그러면 러너가 SVMS 앞에서 터지는데 그땐 사람 손을 떠난 뒤다.
B2 = upload(profile='generic', vendor_cd='V99999', exp_cd='010101').get_json()['id']
claim_and_report(B2, 'READY', dict(FULL, INV_DT='2026-08-01'), LINES)
r = c.post('/api/liscr/jobs/%d/approve' % B2, json={})
chk(r.status_code == 400 and 'Invoice Date' in ' '.join(r.get_json().get('missing') or []),
    '🔴 안 고친 필드의 잘못된 날짜도 단건 승인에서 걸린다', r.get_json())
row = job(B2)
rb = c.post('/api/liscr/jobs/approve-bulk',
            json={'items': [{'id': B2, 'inv_no': row['inv_no'], 'amt': row['amt']}]})
chk(rb.status_code == 200 and rb.get_json()['approved'] == [],
    '🔴 일괄 승인도 같은 검사를 통과 못 한다(한쪽만 느슨해지지 않는다)', rb.get_json())
chk('Invoice Date' in (rb.get_json()['skipped'][0]['reason'] if rb.get_json()['skipped'] else ''),
    '건너뛴 사유에 어느 값이 문제인지 실림', rb.get_json())
r = c.post('/api/liscr/jobs/%d/approve' % B2, json={'inv_dt': '20260801'})
chk(r.status_code == 200 and job(B2)['status'] == 'approved',
    '형이 제대로 고쳐 넣으면 승인된다', r.get_json())

B3 = upload(profile='generic', vendor_cd='V99999', exp_cd='010101').get_json()['id']
claim_and_report(B3, 'READY', FULL, LINES)
chk(c.post('/api/liscr/jobs/%d/approve' % B3,
           json={'inv_dt': 20260801}).status_code == 200,
    'JSON 숫자로 온 날짜도 같은 값이면 통과(500 나지 않음)')
B4 = upload(profile='generic', vendor_cd='V99999', exp_cd='010101').get_json()['id']
claim_and_report(B4, 'READY', FULL, LINES)
for bad in (None, True, [], {'a': 1}, '20260800', '00000000'):
    chk(c.post('/api/liscr/jobs/%d/approve' % B4,
               json={'inv_dt': bad}).status_code == 400,
        '🔴 날짜 %r 거부(400) — 500 이 아니라 사유를 돌려준다' % (bad,))
chk(job(B4)['status'] == 'parsed', '거부되는 동안 상태는 그대로', job(B4)['status'])

print('\n# 13) 통화 빈 카드는 **일괄 승인에서도** 못 나간다')
# 화면에서 통화를 드롭다운으로 바꾼 뒤라(2026-08-19), 통화 없는 카드는 형이 골라야 하는 건이다.
# 일괄 버튼은 아무 값도 채워줄 수 없으므로 여기서 반드시 걸러져야 한다(단건과 같은 함수).
B5 = upload(profile='generic', vendor_cd='V99999', exp_cd='010101').get_json()['id']
claim_and_report(B5, 'FIX', dict(FULL, CUR_CD=None), LINES, reasons=['통화 미판독'])
row = job(B5)
rb = c.post('/api/liscr/jobs/approve-bulk',
            json={'items': [{'id': B5, 'inv_no': row['inv_no'], 'amt': row['amt']}]})
chk(rb.status_code == 200 and rb.get_json()['approved'] == [],
    '🔴 통화 빈 채로 일괄 승인 불가', rb.get_json())
chk('통화' in (rb.get_json()['skipped'][0]['reason'] if rb.get_json()['skipped'] else ''),
    '건너뛴 사유가 통화라고 말해줌', rb.get_json())
chk(job(B5)['status'] == 'parsed', '거부돼도 승인 대기줄에 남음', job(B5)['status'])

print('\n# 14) Vendor·Expense 를 **카드마다** 고른다 (2026-08-19 형 지시)')
# 형: "기타 인보이스를 선택하면 Vendor랑 Expense 는 타이틀이 아니라 각 인보이스 카드마다
#      선택하게 해줘야 여러 벤더가 섞여있어도 한번에 등록가능하잖아."
# → 업로드 폼에서 비울 수 있어야 하고(안 그러면 묶음 전체가 한 벤더로 박힌다),
#   승인에서 카드값을 받아 헤더·라인·컬럼에 반영해야 한다.
r = upload(profile='generic')
chk(r.status_code == 200, '🔴 Vendor·Expense 없이 generic 업로드 = 200', r.get_json())
CJ = r.get_json()['id']
chk(job(CJ)['vndr_cd'] is None and job(CJ)['exp_cd'] is None,
    '안 고른 값은 빈 채로 큐에 들어감(러너가 FIX 로 내린다)', dict(job(CJ)))

# 러너가 값 없이 올린 FIX 카드 = 형이 카드에서 채울 상태.
claim_and_report(CJ, 'FIX', dict(FULL, VNDR_CD=None, VNDR_NM=None, PAY_TERM=1),
                 [dict(LINES[0], EXP_CD=None, EXP_NM=None)],
                 reasons=['Vendor 미지정 — 카드에서 고를 것', 'Expense 미지정 — 카드에서 고를 것'])
r = c.post('/api/liscr/jobs/%d/approve' % CJ, json={})
chk(r.status_code == 400, '🔴 안 고른 채 승인 = 400', r.status_code)
miss = r.get_json().get('missing') or []
chk('Vendor' in miss and 'Expense' in miss,
    '🔴 무엇을 안 골랐는지 이름으로 말해줌(Expense 는 헤더가 아니라 라인이라 놓치기 쉽다)', miss)

for bad, lab in (({'vndr_cd': 'NOPE'}, 'Vendor'), ({'exp_cd': '999999'}, 'Expense')):
    rr = c.post('/api/liscr/jobs/%d/approve' % CJ, json=dict(bad, pay_dt='20260930'))
    chk(rr.status_code == 400 and '마스터에 없음' in (rr.get_json().get('error') or ''),
        '🔴 마스터에 없는 %s 코드는 거부(코드만 믿고 SVMS 로 보내지 않는다)' % lab, rr.get_json())

# 🔴 벤더를 고르면 Remit 을 다시 확인받는다 — PAY_TERM 은 벤더마다 다르고(실측: LISCR=1,
#    뷰로베리타스=없음) 그 계산은 러너만 한다. 앞 벤더 기준 날짜가 조용히 남으면 형이 본 적
#    없는 송금일이 SVMS 로 나간다.
r = c.post('/api/liscr/jobs/%d/approve' % CJ, json={'vndr_cd': 'V25081', 'exp_cd': '070205'})
chk(r.status_code == 400 and 'Pay Date' in (r.get_json().get('error') or ''),
    '🔴 벤더를 고쳤는데 Remit 재확인이 없으면 400', r.get_json())
chk(job(CJ)['status'] == 'parsed', '거부되는 동안 상태는 그대로', job(CJ)['status'])

r = c.post('/api/liscr/jobs/%d/approve' % CJ,
           json={'vndr_cd': 'V25081', 'exp_cd': '070205', 'pay_dt': '20261031'})
chk(r.status_code == 200, '카드에서 고르고 Remit 까지 확인하면 승인', r.get_json())
row = job(CJ)
chk(row['vndr_cd'] == 'V25081' and row['vndr_nm'] == 'LISCR',
    '🔴 고른 벤더가 **컬럼에도** 저장됨(카드 화면은 컬럼을 읽는다)', dict(row))
chk(row['exp_cd'] == '070205' and row['exp_nm'] == 'TAX/OTHERS',
    '🔴 고른 Expense 도 컬럼에 저장됨', dict(row))
hdr = json.loads(row['header_json'])
chk(hdr['VNDR_CD'] == 'V25081' and hdr['VNDR_NM'] == 'LISCR',
    '헤더에도 코드+이름이 같이 실림(러너가 이름을 다시 조회하지 않는다)', hdr)
chk(hdr.get('PAY_TERM') is None,
    '🔴 앞 벤더의 PAY_TERM 은 버려짐 — 러너가 확인받은 Remit 에서 역산한다', hdr)
chk(hdr['PAY_DT'] == '20261031', '확인받은 Remit 이 헤더 PAY_DT', hdr)
ln = json.loads(row['lines_json'])
chk(all(x['EXP_CD'] == '070205' and x['EXP_NM'] == 'TAX/OTHERS' for x in ln),
    '🔴 Expense 는 Invoice List **행 전체**에 박힌다(한 줄만 고치면 나머지가 빈 코드로 나간다)', ln)

# 🔴 카드에서 열렸다고 해서 기국의 고정이 풀리면 안 된다 — "고정" 이 기본값으로 내려앉는다.
LJ = upload(profile='liscr').get_json()['id']
claim_and_report(LJ, 'READY', dict(FULL, VNDR_CD='V25081', VNDR_NM='LISCR'),
                 [dict(LINES[0], EXP_CD='070205', EXP_NM='TAX/OTHERS')])
r = c.post('/api/liscr/jobs/%d/approve' % LJ,
           json={'vndr_cd': 'V99999', 'exp_cd': '010101', 'pay_dt': '20260930'})
chk(r.status_code == 200, '기국 카드 승인 자체는 된다', r.get_json())
row = job(LJ)
chk(row['vndr_cd'] == 'V25081' and row['exp_cd'] == '070205',
    '🔴 기국은 카드에서 보낸 Vendor·Expense 를 무시(고정값 유지)', dict(row))
chk(json.loads(row['lines_json'])[0]['EXP_CD'] == '070205', '기국 라인 Expense 도 고정값 그대로')

print('\n# 15) 카드에서 **지운** 값이 옛 값으로 되살아나지 않는다 (2026-08-19 올마이트 지적)')
# 🔴 실제로 났던 구멍: 화면은 빈칸을 요청에서 아예 빼고, 서버는 안 온 필드를 "안 고침" 으로
#    보고 **저장된 옛 값**을 그대로 승인했다. 그래서 형이 Vendor 를 지우고 새로 안 고른 채
#    승인을 누르면, 카드도 확인창도 비어 있는데 **앞 벤더로 돈이 나갔다**.
#    이제 빈칸은 '비움' 으로 전달되고, 비면 완결성 검사가 막는다.
BJ = upload(profile='generic').get_json()['id']
claim_and_report(BJ, 'READY', dict(FULL, VNDR_CD='V25081', VNDR_NM='LISCR'),
                 [dict(LINES[0], EXP_CD='070205', EXP_NM='TAX/OTHERS')])
for f, lab in (('vndr_cd', 'Vendor'), ('exp_cd', 'Expense'), ('amt', '금액'),
               ('inv_no', 'Invoice No'), ('subject', '적요(Subject)')):
    rr = c.post('/api/liscr/jobs/%d/approve' % BJ, json={f: ''})
    miss = (rr.get_json() or {}).get('missing') or []
    chk(rr.status_code == 400 and lab in miss,
        '🔴 %s 를 지우고 승인 = 400(옛 값으로 통과 안 됨)' % lab, (rr.status_code, rr.get_json()))
chk(job(BJ)['status'] == 'parsed' and job(BJ)['vndr_cd'] == 'V25081',
    '거부되는 동안 저장값은 그대로(빈 요청이 컬럼을 지우지도 않는다)', dict(job(BJ)))
rr = c.post('/api/liscr/jobs/%d/approve' % BJ, json={'amt': ''})
chk('라인이 2줄' not in ((rr.get_json() or {}).get('error') or ''),
    '금액을 비운 건 "라인이 2줄" 이 아니라 "안 채운 값" 으로 답함', rr.get_json())

# 🔴 같은 벤더를 다시 보낸 것은 '변경' 이 아니다 — 매번 Remit 재확인을 요구하면
#    안 바뀐 카드도 승인이 막힌다(올마이트가 의심한 지점, 실측으로는 안 걸린다).
r = c.post('/api/liscr/jobs/%d/approve' % BJ, json={'vndr_cd': 'V25081', 'exp_cd': '070205'})
chk(r.status_code == 200, '벤더를 그대로 다시 보내면 Remit 재확인 없이 승인됨', r.get_json())
chk(json.loads(job(BJ)['header_json']).get('PAY_TERM') == FULL.get('PAY_TERM'),
    '안 바뀐 벤더의 PAY_TERM 은 버리지 않음', json.loads(job(BJ)['header_json']))

print('\n' +('❌ 실패 %d건: %s' % (len(fails), fails) if fails else '✅ 전부 통과'))
for r in A.query("SELECT id FROM liscr_job"):
    p = os.path.join(PDF_DIR, '%d.pdf' % r['id'])
    if os.path.exists(p):
        os.unlink(p)
os.path.exists(DB) and os.unlink(DB)
sys.exit(1 if fails else 0)
