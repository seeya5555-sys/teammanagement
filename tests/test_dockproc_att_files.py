#!/usr/bin/env python3
"""Dock 발주현황 — 벤더 **견적서 첨부**(att_files + preview cache) 계약 테스트.

배경(2026-07-31 형 요청 Phase ②): 제출견적 금액(Phase ①)에 이어 **견적서 원본**을 웹/앱에서
열어보고 싶다. TRMT 서버는 SVMS/NAS 에 붙을 수 없으므로 흐름이 3단계다.
  폴러 sync → 목록(att_files) 적재 → 폴러가 `pending` 조회 → NAS 에서 받아 업로드 → 웹/앱이 열기

핵심 계약(여기서 깨지면 형이 **다른 업체 견적서를 열게 된다**):
  · `files` 키 미전송 → 기존 목록 유지 / `[]` → 첨부 0건으로 갱신
  · 목록의 **배열 위치(idx)가 캐시 파일명**이다 → 목록이 바뀌면 내용이 달라진 idx 의 캐시만 폐기.
    같은 (sv,nm) 이 같은 자리에 남아 있으면 유지(재다운로드 낭비 방지).
  · 업로드는 확장자 allowlist + magic-byte 일치 + 목록에 있는 idx 만 (inline 서빙 경로라 필수)
  · `pending` 은 이미 캐시된 idx 를 빼고 준다(멱등 — 재실행이 같은 파일을 다시 받지 않음)

실행: /tmp/trmt-test-venv/bin/python tests/test_dockproc_att_files.py
      (시스템 python3.9 는 hashlib.scrypt 없어서 app import 단계에서 죽음)
"""
import os, sys, json, tempfile

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root (clone 위치 무관)
sys.path.insert(0, os.getcwd())
DB = tempfile.mktemp(suffix='.db')
os.environ['TRMT_DB'] = DB

import app as A

A.DATABASE = DB
A.app.config['DATABASE'] = DB
A.app.config['TESTING'] = True
A.DOCKATT_FILE_DIR = tempfile.mkdtemp(prefix='dockatt-')     # 실제 캐시 디렉터리를 건드리지 않는다
A.init_db(drop=False)
A._auto_migrate()

fails = []


def chk(cond, name, extra=''):
    print(('  ok  ' if cond else '  ❌  ') + name + (f' — {extra}' if extra and not cond else ''))
    if not cond:
        fails.append(name)


A.app.app_context().push()
c = A.app.test_client()

KEY = 'testkey-dockproc-attfiles'
A._ensure_api_table()
A.execute("INSERT OR REPLACE INTO api_settings(k, v) VALUES('api_key', ?)", (KEY,))
HDR = {'X-API-Key': KEY}
A.execute("INSERT INTO dock_procure_vessel(vsl_nm, vsl_cd) VALUES('TEST VESSEL','TSTV')")

PDF = b'%PDF-1.4\nquotation\n%%EOF'
PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 24
XLSX = b'PK\x03\x04' + b'\x00' * 40

F1 = {'nm': 'A_quote.pdf', 'kb': 362, 'vndr': 'V1', 'vnm': 'A CO', 'dt': '20260731', 'sv': 's_a.pdf'}
F2 = {'nm': 'B_quote.pdf', 'kb': 120, 'vndr': 'V2', 'vnm': 'B CO', 'dt': '20260731', 'sv': 's_b.pdf'}


def mkrow(req_no, att_files=None, svms_req_no=None):
    A.execute("DELETE FROM dock_procure WHERE req_no=? AND vsl_nm='TEST VESSEL'", (req_no,))
    A.execute(
        "INSERT INTO dock_procure(vsl_nm, vsl_cd, req_no, cat_code, subject, "
        "stg_quote, stg_vendor, stg_order, svms_req_no, att_files) "
        "VALUES('TEST VESSEL','TSTV',?,?,?,0,0,0,?,?)",
        (req_no, 'R', f'[DOCK][TSTV {req_no}]subject', svms_req_no, att_files))
    return A.query("SELECT id FROM dock_procure WHERE req_no=? AND vsl_nm='TEST VESSEL'",
                   (req_no,), one=True)['id']


def sync(req_no, files='OMIT', status='Submit'):
    it = {'vsl_cd': 'TSTV', 'subject': f'[DOCK][TSTV {req_no}]subject', 'status': status,
          'ordered_evidence': False}
    if files != 'OMIT':
        it['files'] = files
    r = c.post('/api/ext/dock_procure/sync', json={'items': [it]}, headers=HDR)
    assert r.status_code == 200, r.status_code
    return r.get_json()


def att(req_no):
    raw = A.query("SELECT att_files FROM dock_procure WHERE req_no=? AND vsl_nm='TEST VESSEL'",
                  (req_no,), one=True)['att_files']
    return (json.loads(raw) if raw else None), raw


def fp_of(rid, idx):
    """서버가 계산할 지문 — 폴러는 pending 으로 이 값을 받아 그대로 되돌려준다."""
    row = A.query("SELECT att_files FROM dock_procure WHERE id=?", (rid,), one=True)
    if not row:                                   # 없는 행 케이스 — 지문을 만들 수 없다
        return 'none'
    files = A._dockproc_files_of(row['att_files'])
    return A._dockatt_fp(files[idx]) if idx < len(files) else 'none'


def upload(rid, idx, data=PDF, ext='pdf', fp=None):
    url = '/api/ext/dock_procure/%d/attachments/%d' % (rid, idx)
    url += '?fp=' + (fp if fp is not None else fp_of(rid, idx))
    if ext:
        url += '&ext=' + ext
    return c.post(url, data=data, headers=HDR)


# ---------------------------------------------------------------- 3상태 계약
print('# files 3상태 — 미전송/[]/실값')
mkrow('R1', att_files=json.dumps([F1], sort_keys=True, separators=(',', ':')))
sync('R1', files='OMIT')
chk(att('R1')[0] == [F1], '키 미전송 → 기존 목록 유지(조회 실패로 화면이 비지 않음)', att('R1')[0])

sync('R1', files=[])
chk(att('R1')[0] is None, '[] → 첨부 0건으로 clear(SVMS 에서 지워진 첨부가 남지 않음)', att('R1')[0])

r = sync('R1', files=[F1, F2])
chk(len(att('R1')[0]) == 2, '실값 → 목록 적재', att('R1')[0])
chk(sync('R1', files=[F1, F2])['updated'] == 0, '같은 패킷 재전송 → updated=0 (멱등)')
chk(sync('R1', files=[F2, F1])['updated'] == 0, '순서만 다른 패킷 → updated=0 (서버가 정렬을 못박음)')

print('# 정규화 — 못 여는 값은 들이지 않음')
sync('R1', files=[{'nm': '', 'sv': 'x'}, F1])
chk(att('R1')[0] == [F1], '파일명 없는 첨부는 제외(열 수도 표시할 수도 없음)', att('R1')[0])
sync('R1', files=[dict(F1, kb='1,234')])
chk(att('R1')[0][0]['kb'] == 1234, 'kb 콤마 문자열 파싱')
sync('R1', files=[dict(F1, kb='oops')])
chk(att('R1')[0][0]['kb'] == 0, 'kb 파싱 실패 → 0')
sync('R1', files=[dict(F1, kb=float('inf'))])
chk(att('R1')[0][0]['kb'] <= 99_999_999, 'inf 방어(int(float(inf))=OverflowError)')
sync('R1', files=[dict(F1, nm='q%d.pdf' % i, sv='s%02d.pdf' % i) for i in range(30)])
chk(len(att('R1')[0]) == 20, '개수 캡 20 (무한 첨부 방어)', len(att('R1')[0]))
sync('R1', files='not-a-list')
chk(len(att('R1')[0]) == 20, '리스트 아님 → 미전송 취급(기존 유지)')

# ---------------------------------------------------------------- 업로드 방어
print('# 업로드 — allowlist·magic-byte·목록 정합')
rid = mkrow('R2', att_files=json.dumps([F1, F2], sort_keys=True, separators=(',', ':')),
            svms_req_no='TSTVME26073101')
chk(upload(rid, 0, PDF, 'pdf').status_code == 200, 'PDF 업로드 200')
chk(upload(rid, 0, PNG, 'pdf').status_code == 400, '확장자 위장(png 바이트를 pdf 로) → 400')
chk(upload(rid, 0, PDF, 'exe').status_code == 200,
    "?ext= 가 이상하면 목록의 파일명으로 폴백(위장은 magic-byte 가 막으므로 안전)")
_z = mkrow('RZ', att_files=json.dumps([{'nm': 'pack.zip', 'kb': 1, 'vndr': '', 'vnm': '',
                                        'dt': '', 'sv': 's_z.zip'}],
                                      sort_keys=True, separators=(',', ':')))
chk(upload(_z, 0, PDF, 'zip').status_code == 400,
    '확장자·파일명 둘 다 허용목록 밖 → 400 (열지도 못하는 파일을 캐시에 쌓지 않음)')
chk(upload(rid, 0, b'', 'pdf').status_code == 400, '빈 body → 400')
chk(upload(rid, 5, PDF, 'pdf').status_code == 409, '목록에 없는 idx → 409 (이름↔내용 어긋남 방지)')
chk(upload(rid, 99, PDF, 'pdf').status_code == 404, 'idx 상한 초과 → 404')
chk(upload(9999, 0, PDF, 'pdf').status_code == 404, '없는 행 → 404')
chk(c.post('/api/ext/dock_procure/%d/attachments/0' % rid, data=PDF).status_code in (401, 403),
    'API 키 없으면 업로드 거부')

print('# 서빙 — 읽기전용·inline·nosniff')
r = c.get('/api/dock_procure/%d/attachments/0' % rid)
chk(r.status_code == 302 or r.status_code == 401, '비로그인은 열 수 없음(login_required)', r.status_code)
r.close()
with c.session_transaction() as s:
    s['user_id'] = 1
    s['role'] = 'admin'
r = c.get('/api/dock_procure/%d/attachments/0' % rid)
chk(r.status_code == 200 and r.mimetype == 'application/pdf', '로그인 후 PDF 200', r.status_code)
chk(r.headers.get('X-Content-Type-Options') == 'nosniff', 'nosniff 헤더')
chk('inline' in r.headers.get('Content-Disposition', ''), 'PDF 는 inline(새 탭에서 바로 보임)')
chk('A_quote.pdf' in r.headers.get('Content-Disposition', ''), '원본 파일명으로 내려감',
    r.headers.get('Content-Disposition'))
r.close()
r = c.get('/api/dock_procure/%d/attachments/1' % rid)
chk(r.status_code == 404, '아직 안 받은 첨부 → 404 (칩은 회색으로 구분 표시됨)')
r.close()
chk(upload(rid, 1, XLSX, 'xlsx').status_code == 200, 'xlsx 견적서도 허용')
r = c.get('/api/dock_procure/%d/attachments/1' % rid)
chk(r.status_code == 200 and 'attachment' in r.headers.get('Content-Disposition', ''),
    'xlsx 는 다운로드로(inline 아님)')
r.close()

# ---------------------------------------------------------------- 캐시 무효화
print('# 캐시 무효화 — 목록이 바뀌면 내용이 달라진 자리만 폐기')
lines = lambda: [l for l in c.get('/api/dock_procure/lines?vsl=TEST VESSEL').get_json()['lines']
                 if l['id'] == rid][0]
chk(lines().get('att_cached') == [0, 1], '목록 API 가 실제 캐시된 idx 를 알려줌', lines().get('att_cached'))

sync('R2', files=[F1, F2])                       # 동일 목록 → 캐시 유지되어야 한다
chk(lines().get('att_cached') == [0, 1], '목록 그대로면 캐시 유지(재다운로드 낭비 없음)')

sync('R2', files=[F2])                           # F1 삭제 → idx0 이 F2 로 밀림
chk(lines().get('att_cached') == [], 'F1 삭제로 자리가 밀리면 그 캐시 폐기(A업체 자리에 B업체 파일 방지)',
    lines().get('att_cached'))

rid3 = mkrow('R3', att_files=json.dumps([F1, F2], sort_keys=True, separators=(',', ':')),
             svms_req_no='TSTVME26073102')
upload(rid3, 0, PDF, 'pdf')
upload(rid3, 1, PDF, 'pdf')
sync('R3', files=[F1, F2, dict(F1, nm='C.pdf', sv='s_c.pdf')])   # 뒤에 추가 → 앞 자리는 그대로
kept = [l for l in c.get('/api/dock_procure/lines?vsl=TEST VESSEL').get_json()['lines']
        if l['id'] == rid3][0].get('att_cached')
chk(kept == [0, 1], '뒤에 첨부가 추가되면 앞 자리 캐시는 유지', kept)

# ---------------------------------------------------------------- pending
print('# pending — 이미 받은 건 다시 안 준다')
pend = lambda **kw: c.get('/api/ext/dock_procure/attachments/pending',
                          query_string=kw, headers=HDR).get_json()['pending']
ids = [(p['id'], p['idx']) for p in pend()]
chk((rid3, 0) not in ids and (rid3, 1) not in ids, '캐시된 idx 는 pending 에서 빠짐(멱등)', ids)
chk((rid3, 2) in ids, '아직 안 받은 idx 는 pending 에 있음', ids)
chk(all(p.get('svms_req_no') for p in pend()), 'pending 전원 svms_req_no 보유(폴러가 SVMS 에서 찾을 키)')
chk(any(p.get('sv') for p in pend()), 'pending 이 SAVE_NM(sv) 을 준다 — 폴러가 순서 대신 이걸로 매칭')

mkrow('R4', att_files=json.dumps([F1], sort_keys=True, separators=(',', ':')), svms_req_no=None)
chk(all(p['req_no'] != 'R4' for p in pend()), 'svms_req_no 없는 행은 pending 제외(찾을 방법이 없음)')
chk(c.get('/api/ext/dock_procure/attachments/pending').status_code in (401, 403),
    'API 키 없으면 pending 거부')
one = c.get('/api/ext/dock_procure/attachments/pending',
            query_string={'limit': 1}, headers=HDR).get_json()
chk(len(one['pending']) == 1 and one['truncated'] is True, 'limit 로 잘리면 truncated=True (조용한 누락 없음)')
chk(pend(vsl_cd='NOPE') == [], '다른 선박 필터 → 0건')
chk(all(p.get('fp') for p in pend()), 'pending 이 지문 토큰(fp)을 준다 — 업로드 때 되돌려줄 값')

# ------------------------------------------- 올마이트 지적 반영분 (race·stale·직접URL)
print('# race — pending 받은 뒤 목록이 바뀌면 옛 파일이 새 자리에 저장되면 안 됨')
rid5 = mkrow('R5', att_files=json.dumps([F1, F2], sort_keys=True, separators=(',', ':')),
             svms_req_no='TSTVME26073105')
stale_fp = fp_of(rid5, 0)                        # 폴러가 pending 으로 받아간 지문(= F1)
sync('R5', files=[F2])                           # 그 사이 F1 이 SVMS 에서 삭제 → idx0 이 F2 로 밀림
r = upload(rid5, 0, PDF, 'pdf', fp=stale_fp)     # 폴러가 뒤늦게 F1 바이트를 올림
chk(r.status_code == 409 and r.get_json().get('error') == 'fingerprint mismatch',
    '옛 지문 업로드 → 409 (B업체 자리에 A업체 파일 저장 차단)', r.status_code)
chk(upload(rid5, 0, PDF, 'pdf', fp='').status_code == 409, '지문 없이 업로드 → 409 (fail-closed)')
chk(upload(rid5, 0, PDF, 'pdf').status_code == 200, '현재 지문으로는 정상 저장')

print('# stale cache — GC 가 실패해도 오열람은 없어야 함')
rid6 = mkrow('R6', att_files=json.dumps([F1], sort_keys=True, separators=(',', ':')),
             svms_req_no='TSTVME26073106')
upload(rid6, 0, PDF, 'pdf')
before = os.listdir(A.DOCKATT_FILE_DIR)
_gc, A._dockatt_gc = A._dockatt_gc, lambda *a, **k: 0     # GC 가 통째로 실패한 상황 재현
sync('R6', files=[F2])                                    # 목록이 F2 로 교체됐지만 옛 파일은 디스크에 남음
A._dockatt_gc = _gc
chk(len(os.listdir(A.DOCKATT_FILE_DIR)) == len(before), '  (전제) 옛 캐시 파일이 실제로 남아있음')
r = c.get('/api/dock_procure/%d/attachments/0' % rid6)
chk(r.status_code == 404, 'stale 파일이 남아도 지문 불일치로 404 (다른 업체 견적서 노출 차단)', r.status_code)
r.close()
cached6 = [l for l in c.get('/api/dock_procure/lines?vsl=TEST VESSEL').get_json()['lines']
           if l['id'] == rid6][0].get('att_cached')
chk(cached6 == [], 'stale 파일을 "수집됨"으로 세지 않음(칩이 회색 유지)', cached6)
chk(any(p['id'] == rid6 and p['idx'] == 0 for p in pend()),
    'pending 이 다시 대상으로 잡아줌(=영영 안 받는 상태로 굳지 않음)')

print('# 목록이 비워진 뒤 직접 URL')
rid7 = mkrow('R7', att_files=json.dumps([F1], sort_keys=True, separators=(',', ':')),
             svms_req_no='TSTVME26073107')
upload(rid7, 0, PDF, 'pdf')
A._dockatt_gc, _gc2 = (lambda *a, **k: 0), A._dockatt_gc
sync('R7', files=[])                                      # 첨부 0건으로 clear
A._dockatt_gc = _gc2
r = c.get('/api/dock_procure/%d/attachments/0' % rid7)
chk(r.status_code == 404, '목록이 비었으면 캐시가 남아도 404 (idx 범위 재검증)', r.status_code)
r.close()

print('# 같은 저장명으로 내용만 바뀐 경우 — 크기가 지문에 들어가 다시 받음')
rid8 = mkrow('R8', att_files=json.dumps([F1], sort_keys=True, separators=(',', ':')),
             svms_req_no='TSTVME26073108')
upload(rid8, 0, PDF, 'pdf')
sync('R8', files=[dict(F1, kb=999)])                      # 같은 sv·nm, 크기만 변경
chk(any(p['id'] == rid8 for p in pend()), '크기가 바뀌면 지문이 달라져 재수집 대상이 됨')
r = c.get('/api/dock_procure/%d/attachments/0' % rid8)
chk(r.status_code == 404, '  옛 내용은 서빙되지 않음', r.status_code)
r.close()

print('# sv 대조 — 화면이 낡은 사이 목록이 바뀌어도 "칩 이름과 다른 파일"이 열리지 않음')
#   지문 검증은 '서버의 현재 목록'과만 맞춘다. 앱/웹 화면이 옛 목록을 들고 있는 동안 그 자리가
#   다른 업체 파일로 교체되고 캐시까지 채워지면, 지문은 맞으므로 200 이 나간다 → 호출자가 자기가
#   본 신원(sv)을 같이 보내면 서버가 그것까지 확인한다.
ridA = mkrow('R11', att_files=json.dumps([F1], sort_keys=True, separators=(',', ':')),
             svms_req_no='TSTVME26073111')
upload(ridA, 0, PDF, 'pdf')
r = c.get('/api/dock_procure/%d/attachments/0?sv=%s' % (ridA, F1['sv']))
chk(r.status_code == 200, '내가 본 첨부와 같은 신원이면 정상 200', r.status_code)
r.close()
r = c.get('/api/dock_procure/%d/attachments/0?sv=%s' % (ridA, F2['sv']))
chk(r.status_code == 404, '옛 화면의 신원과 어긋나면 404 (B업체 파일이 A업체 자리에 안 뜸)', r.status_code)
r.close()
sync('R11', files=[F2])                                    # 목록 교체 후 새 파일을 그 자리에 채움
upload(ridA, 0, PDF, 'pdf')
r = c.get('/api/dock_procure/%d/attachments/0?sv=%s' % (ridA, F1['sv']))
chk(r.status_code == 404, '교체 뒤 옛 sv 로 열면 404 — 지문만으로는 못 막던 창을 닫음', r.status_code)
r.close()
r = c.get('/api/dock_procure/%d/attachments/0' % ridA)
chk(r.status_code == 200, 'sv 미지정(웹 기존 링크)은 종전대로 동작 — 하위호환', r.status_code)
r.close()

print('# GC 는 용량만 담당 — 현재 목록이 참조하는 파일은 남긴다')
rid9 = mkrow('R9', att_files=json.dumps([F1, F2], sort_keys=True, separators=(',', ':')),
             svms_req_no='TSTVME26073109')
upload(rid9, 0, PDF, 'pdf')
upload(rid9, 1, PDF, 'pdf')
n_before = len([x for x in os.listdir(A.DOCKATT_FILE_DIR) if x.startswith('%d_' % rid9)])
sync('R9', files=[F1, F2])
n_after = len([x for x in os.listdir(A.DOCKATT_FILE_DIR) if x.startswith('%d_' % rid9)])
chk(n_before == 2 and n_after == 2, '목록 그대로면 GC 가 아무것도 안 지움', (n_before, n_after))
sync('R9', files=[F1])
n_gc = len([x for x in os.listdir(A.DOCKATT_FILE_DIR) if x.startswith('%d_' % rid9)])
chk(n_gc == 1, '참조 안 되는 파일은 GC 가 회수', n_gc)

print()
if fails:
    print(f'❌ FAIL {len(fails)}건: {fails}')
    sys.exit(1)
print('✅ 전부 통과')
