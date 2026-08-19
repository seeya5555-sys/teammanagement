#!/usr/bin/env python3
"""`/liscr` 목록 삭제 계약 테스트 — 카드별 [삭제] · [완료된 카드 치우기].

형 지시(2026-08-19): "리스트 삭제 버튼도 필요함(올리고나면 TRMT웹에 리스트 삭제)".

핵심 계약(여기가 깨지면 인보이스가 두 번 생기거나, 확인할 것이 조용히 사라진다):
  · 🔴 **러너가 잡고 있거나 곧 잡을 상태는 못 지운다** — queued/parsing/approved/creating.
        creating 을 지우면 러너가 결과를 회신할 행이 사라지고, 그 사이 SVMS 에는
        인보이스가 이미 만들어져 있을 수 있다(create 단계엔 lease 회수가 없다).
  · 🔴 **created 는 지울 수 있다.** 한때 `inv_cd IS NULL` 로 막아뒀는데 근거가 틀렸다 —
        재업로드 중복의 진짜 방어선은 sha256 행이 아니라 SVMS 자체 중복검사
        (`PKG_CO.SP_GET_CHK_INV_NO`)이고, 그건 우리 큐와 무관하게 살아 있다.
        여기서 막으면 형이 완료 카드를 영영 못 치운다(= 이 기능의 목적 자체).
  · 🔴 **일괄 버튼은 `created`/`rejected` 만 먹는다.** failed/hold/parsed 는 사람이 아직
        봐야 할 건이다. 쓸어담는 버튼이 그것들까지 먹으면 확인할 것이 조용히 사라진다.
  · 🔴 **PDF 삭제 실패를 성공이라고 답하지 않는다.** 화면이 "PDF도 함께 삭제됩니다" 라고
        약속하므로, 못 지웠으면 `pdf_removed=False` / `pdf_failed=[...]` 로 올려보낸다.
  · 없는 번호는 404(= "그런 건 없음"), 처리중은 409 — 한 코드로 뭉치면 원인을 못 가린다.

실행: ~/.venvs/trmt-test/bin/python tests/test_liscr_delete.py
"""
import os, sys, tempfile

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


def mkuser(username, role):
    A.execute("INSERT INTO users (username, password_hash, display_name, supervisor_id, "
              "role, active) VALUES (?,'x',?,1,?,1)", (username, username, role))
    return A.query("SELECT id FROM users WHERE username=?", (username,), one=True)['id']


U_ADMIN = mkuser('admin1', 'admin')
U_MEM = mkuser('member1', 'member')


def login(uid, role):
    with c.session_transaction() as s:
        s['user_id'] = uid
        s['role'] = role


import app_core  # noqa: E402
PDF_DIR = app_core.LISCR_PDF_DIR
os.makedirs(PDF_DIR, exist_ok=True)


def mkjob(status, inv_cd=None, sha=None, with_pdf=True):
    """잡 1건 + (원하면) 실제 PDF 파일까지 만든다."""
    jid = A.execute("INSERT INTO liscr_job (filename, sha256, status, inv_cd) VALUES (?,?,?,?)",
                    ('x.pdf', sha, status, inv_cd))
    if with_pdf:
        with open(os.path.join(PDF_DIR, '%d.pdf' % jid), 'wb') as f:
            f.write(b'%PDF-1.4 test')
    return jid


def status_of(jid):
    r = A.query("SELECT status FROM liscr_job WHERE id=?", (jid,), one=True)
    return r['status'] if r else None


def pdf_exists(jid):
    return os.path.exists(os.path.join(PDF_DIR, '%d.pdf' % jid))


login(U_ADMIN, 'admin')

print('# 1) 상태별 삭제 가부 — 러너 소유 상태만 막힌다')
BLOCKED = ['uploading', 'queued', 'parsing', 'approved', 'creating']
ALLOWED = ['created', 'failed', 'rejected', 'hold', 'parsed']
for st in BLOCKED:
    jid = mkjob(st)
    resp = c.delete('/api/liscr/jobs/%d' % jid)
    chk(resp.status_code == 409, '%s 삭제 = 409(차단)' % st, resp.status_code)
    chk(status_of(jid) == st, '%s 행은 그대로' % st, status_of(jid))
    chk(pdf_exists(jid), '%s PDF 도 그대로' % st)
for st in ALLOWED:
    jid = mkjob(st)
    resp = c.delete('/api/liscr/jobs/%d' % jid)
    j = resp.get_json()
    chk(resp.status_code == 200 and j.get('deleted') is True, '%s 삭제 = 200' % st, j)
    chk(status_of(jid) is None, '%s 행 사라짐' % st)
    chk(j.get('pdf_removed') is True and not pdf_exists(jid), '%s PDF 도 삭제됨' % st, j)

print('\n# 2) 🔴 created 는 inv_cd 가 박혀 있어도 지울 수 있다(과거 가드 되돌림)')
jid = mkjob('created', inv_cd='BGBBCI2608190001')
resp = c.delete('/api/liscr/jobs/%d' % jid)
chk(resp.status_code == 200, 'inv_cd 있는 created 삭제 = 200', resp.status_code)
chk(status_of(jid) is None, '행 사라짐 — 완료 카드를 치울 수 있어야 이 기능이 의미가 있다')

print('\n# 3) 없는 번호는 404 — "처리중이라 못 지움"(409)과 섞지 않는다')
resp = c.delete('/api/liscr/jobs/999999')
chk(resp.status_code == 404, '없는 jid = 404', resp.status_code)
chk('없는' in (resp.get_json().get('error') or ''), '404 사유가 "없는 건"', resp.get_json())

print('\n# 4) PDF 를 못 지우면 성공이라고 답하지 않는다')
jid = mkjob('created')
_real_remove = A.os.remove


def boom(path):
    if path.endswith('%d.pdf' % jid):
        raise OSError('permission denied (테스트 강제)')
    return _real_remove(path)


A.os.remove = boom
try:
    j = c.delete('/api/liscr/jobs/%d' % jid).get_json()
finally:
    A.os.remove = _real_remove
chk(j.get('deleted') is True, '행 삭제 자체는 성공(되돌리지 않는다)', j)
chk(j.get('pdf_removed') is False, '🔴 pdf_removed=False 로 실패를 올려보냄', j)
chk(status_of(jid) is None, '행은 실제로 사라짐')
_real_remove(os.path.join(PDF_DIR, '%d.pdf' % jid))          # 남은 orphan 정리

print('\n# 5) 일괄 치우기 — created/rejected 만 먹고 나머지는 남는다')
A.execute("DELETE FROM liscr_job")
keep = {st: mkjob(st) for st in ['failed', 'hold', 'parsed', 'queued', 'creating', 'approved']}
gone = {st: mkjob(st) for st in ['created', 'rejected']}
gone['created2'] = mkjob('created', inv_cd='INPSCI2608190001')
j = c.post('/api/liscr/jobs/clear-done').get_json()
chk(j.get('deleted') == 3, '3건 삭제(created 2 + rejected 1)', j)
chk(j.get('pdf_failed') == [], 'PDF 실패 0건', j)
for st, jid in gone.items():
    chk(status_of(jid) is None and not pdf_exists(jid), '%s 는 치워짐' % st)
for st, jid in keep.items():
    chk(status_of(jid) == st and pdf_exists(jid),
        '🔴 %s 는 남는다(사람이 봐야 할 건/러너 소유)' % st, status_of(jid))

print('\n# 6) 치울 게 없을 때 — 에러 없이 0건')
A.execute("DELETE FROM liscr_job")
j = c.post('/api/liscr/jobs/clear-done').get_json()
chk(j.get('deleted') == 0, '빈 상태 clear-done = 0건', j)

print('\n# 7) 삭제 후 같은 PDF 재업로드가 열린다(sha256 중복선이 풀린다)')
import io  # noqa: E402
BODY = b'%PDF-1.4 reupload-test'


def upload():
    return c.post('/api/liscr/upload', data={'file': (io.BytesIO(BODY), 'a.pdf')},
                  content_type='multipart/form-data')


r1 = upload()
jid1 = r1.get_json()['id']
chk(r1.status_code == 200 and status_of(jid1) == 'queued', '1차 업로드 = queued', r1.get_json())
chk(upload().status_code == 409, '처리중 같은 PDF 재업로드 = 409(차단)')
A.execute("UPDATE liscr_job SET status='created', inv_cd='X1' WHERE id=?", (jid1,))
chk(upload().status_code == 409, 'created 상태에서도 같은 PDF 재업로드 = 409')
chk(c.delete('/api/liscr/jobs/%d' % jid1).status_code == 200, '완료 카드 삭제')
r2 = upload()
chk(r2.status_code == 200, '🔴 삭제 후에는 같은 PDF 재업로드 허용', r2.get_json())
# 🔴 이건 "중복 인보이스를 허용한다"는 뜻이 아니다. 우리 큐는 같은 파일을 다시 받을 뿐이고,
#    같은 INV_NO 로 SVMS 에 두 번 쓰는 것은 SP_GET_CHK_INV_NO 가 파싱 때와 쓰기 직전
#    두 지점에서 막는다(automation/svms-liscr-invoice/{create_invoice,runner}.py).

print('\n# 8) 권한 — member 403 · 미로그인 401, 행은 그대로')
A.execute("DELETE FROM liscr_job")
jid = mkjob('created')
login(U_MEM, 'member')
chk(c.delete('/api/liscr/jobs/%d' % jid).status_code == 403, 'member 삭제 = 403')
chk(c.post('/api/liscr/jobs/clear-done').status_code == 403, 'member 일괄삭제 = 403')
with c.session_transaction() as s:
    s.clear()
chk(c.delete('/api/liscr/jobs/%d' % jid).status_code == 401, '미로그인 삭제 = 401')
chk(c.post('/api/liscr/jobs/clear-done').status_code == 401, '미로그인 일괄삭제 = 401')
chk(status_of(jid) == 'created' and pdf_exists(jid), '거부된 요청은 아무것도 안 지움')

print('\n' + ('❌ 실패 %d건: %s' % (len(fails), fails) if fails else '✅ 전부 통과'))
for r in A.query("SELECT id FROM liscr_job"):
    try:
        os.unlink(os.path.join(PDF_DIR, '%d.pdf' % r['id']))
    except OSError:
        pass
try:
    os.unlink(DB)
except OSError:
    pass
sys.exit(1 if fails else 0)
