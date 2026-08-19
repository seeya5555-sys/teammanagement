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

print('\n# 4) 잠기지 않은 프리셋 — 필수값 검사 후 그대로 저장')
chk(upload(profile='generic', exp_cd='010101').status_code == 400, 'Vendor 없으면 400')
chk(upload(profile='generic', vendor_cd='V99999').status_code == 400, 'Expense 없으면 400')
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

print('\n' + ('❌ 실패 %d건: %s' % (len(fails), fails) if fails else '✅ 전부 통과'))
for r in A.query("SELECT id FROM liscr_job"):
    p = os.path.join(PDF_DIR, '%d.pdf' % r['id'])
    if os.path.exists(p):
        os.unlink(p)
os.path.exists(DB) and os.unlink(DB)
sys.exit(1 if fails else 0)
