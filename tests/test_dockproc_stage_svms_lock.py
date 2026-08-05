#!/usr/bin/env python3
"""Dock — **SVMS 정본 행의 단계 하향을 막는 게이트**(`POST /api/dock_procure/<id>/stage`).

실사고(2026-08-05, 형 제보 BGBB S13 = id 148): SVMS 가 `Ordered`(발주서 `BGBBES2608A21A` 발급)인
행을 sync 가 08-04 21:50 에 `[●●●●]` 로 세팅했는데, 08-05 13:38 이 엔드포인트 호출 1건이
'벤더제출' 을 끄면서 cascade 로 `v=f=o=0` → **발주완료 건이 '견적작성' 버킷에 떨어졌다.**
다음 정기 sync(최대 1시간)가 되돌리지만, 되돌아온다는 사실 자체가 그 토글이 무의미하다는 뜻이고
그 사이 화면·필터·상태배지는 틀린 값을 보여준다.

잠그는 것:
  ① SVMS 라벨 rank 아래로 내리는 토글은 **409** 로 거부되고 DB 가 안 변한다.
  ② 올리는 방향은 계속 200 (사람이 sync 보다 앞서 체크하는 정상 사용).
  ③ 기준선은 `min(SVMS rank, 현재 rank)` — 발주근거 fail-closed 로 rank4 가 3 으로 눌린 행
     (`Ordered` + `[●●●○]`)이 통째로 잠기면 안 되고, 사람이 앞서 켠 단계는 스스로 되돌릴 수 있어야 한다.
  ④ SVMS 미연결(라벨 없음·미지 라벨 = rank 0) **수동관리 행은 종전대로 자유롭게 해제**된다.
     라이브 다수가 이 부류라 여기 걸면 수동관리가 통째로 막힌다.

실행: ~/.venvs/trmt-test/bin/python tests/test_dockproc_stage_svms_lock.py
  ⚠️ `/tmp/*venv` 는 죽었다 — 상주 venv 는 `~/.venvs/trmt-test`.
"""
import os, sys, tempfile

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
with c.session_transaction() as s:
    s['user_id'] = 1; s['username'] = 'smoke'; s['role'] = 'admin'

A.execute("INSERT INTO dock_procure_vessel(vsl_nm, vsl_cd) VALUES('TEST VESSEL','TSTV')")


def mkrow(req_no, stages, svms_status):
    """stages = (q, v, f, o)"""
    A.execute("DELETE FROM dock_procure WHERE req_no=? AND vsl_nm='TEST VESSEL'", (req_no,))
    A.execute(
        "INSERT INTO dock_procure(vsl_nm, vsl_cd, req_no, cat_code, subject, "
        "stg_quote, stg_vendor, stg_confirm, stg_order, svms_status) "
        "VALUES('TEST VESSEL','TSTV',?,'S',?,?,?,?,?,?)",
        (req_no, f'[DOCK][TSTV {req_no}]subject') + tuple(stages) + (svms_status,))
    return A.query("SELECT * FROM dock_procure WHERE req_no=? AND vsl_nm='TEST VESSEL'",
                   (req_no,), one=True)['id']


def stages_of(lid):
    r = A.query("SELECT stg_quote, stg_vendor, stg_confirm, stg_order FROM dock_procure WHERE id=?",
                (lid,), one=True)
    return (r['stg_quote'], r['stg_vendor'], r['stg_confirm'], r['stg_order'])


def post(lid, stage, value):
    return c.post(f'/api/dock_procure/{lid}/stage', json={'stage': stage, 'value': value})


print('① SVMS Ordered(rank4) + [●●●●] — 하향 거부')
lid = mkrow('S1', (1, 1, 1, 1), 'Ordered')
r = post(lid, 'vendor', 0)
chk(r.status_code == 409, '벤더제출 해제 = 409', f'got {r.status_code}')
chk(stages_of(lid) == (1, 1, 1, 1), 'DB 단계 무변경(실사고 재현 차단)', str(stages_of(lid)))
chk('Ordered' in (r.get_json() or {}).get('error', ''), '거부 사유에 SVMS 라벨이 들어간다')
r = post(lid, 'order', 0)
chk(r.status_code == 409 and stages_of(lid) == (1, 1, 1, 1), '발주완료 자체 해제도 거부')

print('② 상향은 계속 허용')
lid = mkrow('S2', (1, 0, 0, 0), 'HQ Confirmed')          # rank 1
r = post(lid, 'confirm', 1)
chk(r.status_code == 200, '사람이 sync 보다 앞서 켜는 건 200', f'got {r.status_code}')
chk(stages_of(lid) == (1, 1, 1, 0), 'cascade 로 하위 단계까지 켜짐', str(stages_of(lid)))

print('③ 기준선 = min(SVMS rank, 현재 rank)')
#   발주근거 fail-closed 로 rank4 가 3 으로 눌린 행 — 없는 단계(발주완료)까지 요구하면 안 된다.
lid = mkrow('S3', (1, 1, 1, 0), 'Ordered')
r = post(lid, 'order', 1)
chk(r.status_code == 200 and stages_of(lid) == (1, 1, 1, 1), '눌린 행에서 올리는 건 통과')
lid = mkrow('S4', (1, 1, 1, 0), 'Ordered')
r = post(lid, 'confirm', 0)
chk(r.status_code == 409 and stages_of(lid) == (1, 1, 1, 0),
    'SVMS 가 말한 단계 아래로는 여전히 못 내린다')
#   사람이 SVMS(rank1)보다 앞서 켜 둔 단계는 스스로 되돌릴 수 있어야 한다.
lid = mkrow('S5', (1, 1, 1, 0), 'HQ Confirmed')          # rank 1, 현재 rank 3
r = post(lid, 'confirm', 0)
chk(r.status_code == 200 and stages_of(lid) == (1, 1, 0, 0),
    '수동 선행 체크는 SVMS rank 까지 되돌릴 수 있다', str(stages_of(lid)))
#   `HQ Confirmed` = rank1(견적작성)이므로 벤더제출 해제까지는 정본과 안 부딪힌다 → 허용.
r = post(lid, 'vendor', 0)
chk(r.status_code == 200 and stages_of(lid) == (1, 0, 0, 0),
    'SVMS rank 까지는 계속 되돌릴 수 있다', str(stages_of(lid)))
#   그 아래(견적작성 자체)는 SVMS 가 말한 단계라 못 내린다.
r = post(lid, 'quote', 0)
chk(r.status_code == 409 and stages_of(lid) == (1, 0, 0, 0),
    'SVMS rank(견적작성) 아래로는 못 내린다', f'{r.status_code} {stages_of(lid)}')

print('④ SVMS 미연결 수동관리 행은 종전대로 자유')
for lbl in (None, '', 'HQ Received', 'VSL Approved'):    # 전부 rank 0
    lid = mkrow('S6', (1, 1, 1, 1), lbl)
    r = post(lid, 'vendor', 0)
    chk(r.status_code == 200 and stages_of(lid) == (1, 0, 0, 0),
        f'rank0 라벨({lbl!r})은 해제 허용', f'{r.status_code} {stages_of(lid)}')

print('⑤ SVMS 가 스스로 내린 단계까지는 사람도 내릴 수 있다 (반려·회수 흐름 안 막힘)')
#   결재반려는 sync 가 rank 2 로 되돌린다(단계는 monotonic 아님) — 그 자리까지의 수동 조정은 허용.
lid = mkrow('S7', (1, 1, 1, 0), 'HQ Rejected')           # rank 2
r = post(lid, 'confirm', 0)
chk(r.status_code == 200 and stages_of(lid) == (1, 1, 0, 0),
    'HQ Rejected(rank2) 행은 벤더제출까지 되돌릴 수 있다', f'{r.status_code} {stages_of(lid)}')
r = post(lid, 'vendor', 0)
chk(r.status_code == 409, '그 아래로는 막힌다', f'got {r.status_code}')

print('⑥ 미지·대소문자·공백 라벨의 rank 판정')
chk(A._dockproc_status_rank('ordered') == 4, '소문자도 rank4 (upper 정규화)')
chk(A._dockproc_status_rank('  Ordered  ') == 4, '앞뒤 공백도 rank4 (strip)')
chk(A._dockproc_status_rank('Ordreed') == 0, '오탈자 라벨은 rank0 = fail-open')
chk(A._dockproc_status_rank(None) == 0 and A._dockproc_status_rank('') == 0, 'None/빈값은 rank0')

print('⑦ 낙관적 락 — SELECT 와 UPDATE 사이 sync 가 끼어들면 덮어쓰지 않는다')
#   게이트를 통과하는 요청(상향)이라도, 그 사이 단계가 바뀌었으면 stale 값으로 쓰면 안 된다.
lid = mkrow('S8', (1, 0, 0, 0), 'HQ Confirmed')
_orig_rc = A.execute_rc


def _racing_rc(sql, params=()):
    #   본 UPDATE 직전에 sync 가 [●●●●] 로 올려놓은 상황을 재현한다.
    if 'stg_quote=?' in sql and 'WHERE id=? AND stg_quote=?' in sql:
        A.execute("UPDATE dock_procure SET stg_quote=1, stg_vendor=1, stg_confirm=1, stg_order=1 "
                  "WHERE id=?", (lid,))
    return _orig_rc(sql, params)


A.execute_rc = _racing_rc
try:
    r = post(lid, 'vendor', 1)
finally:
    A.execute_rc = _orig_rc
chk(r.status_code == 409, '스냅샷이 어긋나면 409', f'got {r.status_code}')
chk(stages_of(lid) == (1, 1, 1, 1), '끼어든 sync 결과가 살아남는다(덮어쓰기 없음)', str(stages_of(lid)))

print('⑧ 클라이언트가 거부 메시지를 사람 말로 보여준다')
#   웹은 `j.error` 를 toast 에 붙이고, iOS 는 400 이 아닌 코드에서 `error` 문자열을 그대로 쓴다.
#   이 배선이 빠지면 형은 "저장 실패" 만 보고 이유를 모른다.
web = open('templates/dock_procure.html', encoding='utf-8').read()
chk("d=j&&j.error?(' — '+j.error):''" in web, '웹 writeJson 이 서버 error 를 toast 에 붙인다')
ios = os.path.expanduser('~/.openclaw/workspace/trmt-mobile/ios/TRMT/Sources/Features/More/'
                         'DockProcureViewModel.swift')
vm = open(ios, encoding='utf-8').read()
chk('return code == 400 ? "요청 거부: \\(e)" : e' in vm,
    'iOS 가 409 본문의 error 문자열을 그대로 보여준다')

print()
print(('❌ FAIL: ' + ', '.join(fails)) if fails else '✅ 전부 통과')
sys.exit(1 if fails else 0)
