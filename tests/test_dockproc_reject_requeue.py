#!/usr/bin/env python3
"""Dock — **결재 반려(HQ Rejected)가 '벤더 제출' 큐로 다시 올라오는지** 잠그는 테스트.

형 요청(2026-08-04): "리젝트 된것도 다시 '벤더 제출' 큐에 재적재 … 결재 반려되었을때 다시 검토해서
큐로 올려서 할거니깐(벤더 제출 → 벤더 컨펌 동일 프로세스)". 실측 결과 **이미 되고 있었다**
(S13 `BGBBES2608A21` 반려 후 sync 가 `stg_confirm/order` 를 내리고 `_dock_submit_prior` 가 열렸다).
그래서 새 기능이 아니라 **이 동작이 조용히 깨지지 않게 고정**하는 게 이 파일의 목적이다.

잠그는 것 3개:
  ① `_DOCKPROC_STATUS_RANK['HQ REJECTED'] == 2` → sync 가 단계를 **벤더제출까지 되돌린다**
     (rank 는 monotonic 이 아니라 절대값이다 — 되돌림이 정상 경로).
  ② `_dock_submit_prior` = 상신 이후 라벨(`_DOCK_SUBMIT_POST`)일 때만 재상신 차단.
     'HQ Rejected' 는 그 키워드가 하나도 없으므로 submitted draft 가 남아 있어도 **열린다.**
  ③ 대조군 — 'HQ Progressing'/'Submit'/'HQ Confirmed'/'HQ Ordered' 는 계속 막힌다
     (열려버리면 이미 결재 올라간 건에 2차 상신이 나간다 = 돈경로 사고).

실행: ~/.venvs/trmt-test/bin/python tests/test_dockproc_reject_requeue.py
  ⚠️ `/tmp/*venv` 는 죽었다 — 상주 venv 는 `~/.venvs/trmt-test`.
"""
import os, sys, tempfile

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root (clone 위치 무관)
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

KEY = 'testkey-dockproc-reject'
A._ensure_api_table()
A.execute("INSERT OR REPLACE INTO api_settings(k, v) VALUES('api_key', ?)", (KEY,))
HDR = {'X-API-Key': KEY}

A.execute("INSERT INTO dock_procure_vessel(vsl_nm, vsl_cd) VALUES('TEST VESSEL','TSTV')")


def mkrow(req_no, stages_on=(0, 0, 0, 0), svms_status=None, svms_req_no=None, cat_code='S'):
    A.execute("DELETE FROM dock_procure WHERE req_no=? AND vsl_nm='TEST VESSEL'", (req_no,))
    A.execute(
        "INSERT INTO dock_procure(vsl_nm, vsl_cd, req_no, cat_code, subject, "
        "stg_quote, stg_vendor, stg_confirm, stg_order, svms_status, svms_req_no) "
        "VALUES('TEST VESSEL','TSTV',?,?,?,?,?,?,?,?,?)",
        (req_no, cat_code, f'[DOCK][TSTV {req_no}]subject', *stages_on, svms_status, svms_req_no))
    return A.query("SELECT * FROM dock_procure WHERE req_no=? AND vsl_nm='TEST VESSEL'",
                   (req_no,), one=True)['id']


def sync(req_no, status, inq_no=None):
    it = {'vsl_cd': 'TSTV', 'subject': f'[DOCK][TSTV {req_no}]subject', 'status': status,
          'inq_no': inq_no, 'amt': None, 'cur': None, 'vendor': None}
    r = c.post('/api/ext/dock_procure/sync', json={'items': [it]}, headers=HDR)
    assert r.status_code == 200, r.status_code
    return r.get_json()


def row_of(req_no):
    return A.query("SELECT * FROM dock_procure WHERE req_no=? AND vsl_nm='TEST VESSEL'",
                   (req_no,), one=True)


def stages(req_no):
    r = row_of(req_no)
    return (r['stg_quote'], r['stg_vendor'], r['stg_confirm'], r['stg_order'])


def mkdraft(rid, rep_cd, status='submitted'):
    """실제 상신이 한 번 나간 흔적. 반려 판정이 이 이력을 **이기는지**가 핵심."""
    A.execute("DELETE FROM dock_submit_draft WHERE rep_cd=?", (rep_cd,))
    A.execute(
        "INSERT INTO dock_submit_draft(rid, vsl_nm, vsl_cd, req_no, rep_cd, vndr_cd, vndr_nm, "
        "amt, cur, app_no, app_nm, envelope_json, status, decided_at, decided_by, done_at) "
        "VALUES(?,'TEST VESSEL','TSTV','A2',?,'A1CA8','HD현대마린솔루션',60455700,'KRW',"
        "'AL01','결재라인','{}',?,datetime('now','localtime'),'SS0094',datetime('now','localtime'))",
        (rid, rep_cd, status))


print('# 1) rank 자체 — 반려는 벤더제출(2), 결재진행은 벤더컨펌(3)')
chk(A._DOCKPROC_STATUS_RANK.get('HQ REJECTED') == 2,
    "rank('HQ REJECTED') == 2 → 벤더제출 단계", A._DOCKPROC_STATUS_RANK.get('HQ REJECTED'))
chk(A._DOCKPROC_STATUS_RANK.get('HQ PROGRESSING') == 3,
    "rank('HQ PROGRESSING') == 3 → 벤더컨펌 단계", A._DOCKPROC_STATUS_RANK.get('HQ PROGRESSING'))
chk(A._dockproc_status_rank('HQ Rejected') == 2, "대소문자 무관 rank 2", A._dockproc_status_rank('HQ Rejected'))

print('# 2) 🔴 sync 가 단계를 벤더제출로 되돌린다 (벤더컨펌 켜져 있던 행)')
rid = mkrow('A2', stages_on=(1, 1, 1, 0), svms_status='HQ Progressing', svms_req_no='TSTVES2608A21')
sync('A2', 'HQ Rejected', inq_no='TSTVES2608A21')
chk(stages('A2') == (1, 1, 0, 0), '반려 → 벤더컨펌 해제·벤더제출 유지 = 큐 재적재', stages('A2'))
chk(row_of('A2')['svms_status'] == 'HQ Rejected', '라벨도 반려로 갱신', row_of('A2')['svms_status'])

print('# 3) 🔴 재상신 게이트가 열린다 — submitted draft 가 남아 있어도')
mkdraft(rid, 'TSTVES2608A21')
chk(A._dock_submit_prior('TSTVES2608A21', 'HQ Rejected') is None,
    "'HQ Rejected' → 재상신 허용", A._dock_submit_prior('TSTVES2608A21', 'HQ Rejected'))

print('# 4) 대조군 — 상신 이후 라벨은 계속 막힌다 (2차 상신 = 돈경로 사고)')
for lab in ('HQ Progressing', 'Submit', 'HQ Confirmed', 'HQ Ordered', 'Approval'):
    got = A._dock_submit_prior('TSTVES2608A21', lab)
    chk(got is not None, f"'{lab}' → 재상신 차단 유지", got)

print('# 4-1) 이력이 아예 없으면 라벨과 무관하게 None (첫 상신은 게이트 대상 아님)')
chk(A._dock_submit_prior('NOSUCHREPCD', 'HQ Progressing') is None,
    'draft 없으면 열림', A._dock_submit_prior('NOSUCHREPCD', 'HQ Progressing'))

print('# 4-2) submitted 아닌 이력(failed)은 게이트를 만들지 않는다')
mkdraft(rid, 'TSTVES2608A22', status='failed')
chk(A._dock_submit_prior('TSTVES2608A22', 'HQ Progressing') is None,
    "failed 이력은 '이미 상신됨' 이 아니다", A._dock_submit_prior('TSTVES2608A22', 'HQ Progressing'))

print('# 5) 라벨 없음/빈값은 열림 쪽 — 반려 판정과 같은 계열(닫을 근거가 없다)')
for lab in (None, '', '   '):
    chk(A._dock_submit_prior('TSTVES2608A21', lab) is None,
        f'라벨 {lab!r} → 재상신 허용', A._dock_submit_prior('TSTVES2608A21', lab))

print('# 6) 🔴 e2e — 반려 행은 상신 큐가 실제로 다시 만들어진다 (게이트 함수만이 아니라 라우트까지)')
#   올마이트 지적 수용: `_dock_submit_prior` 단위확인만으론 재적재를 보장 못 한다 → 생성 라우트를 태운다.
#   SVMS write 는 맥 워커가 하고 이 테스트는 임시 DB 라 실제 write 는 0건이다.
SUBMITTED = {'cd': 'A1CA8', 'nm': 'HD현대마린솔루션', 'amt': 60455700, 'cur': 'KRW',
             'gross_amt': 60455700, 'final_amt': 60455700, 'st': 'Submitted', 'best': 1}
with c.session_transaction() as s:
    s['user_id'] = 1; s['username'] = 'smoke'; s['role'] = 'admin'
c.post('/api/ext/svms/app_lines', headers=HDR,
       json={'lines': [{'app_no': '0002', 'app_nm': 'Dock 결재', 'user_id': 'SS0094',
                        'approvers': [{'seq': 1, 'id': 'SS0100', 'nm': 'A'}]}]})


def mkq(req_no, svms_status, rep_cd):
    """상신 가능 조건을 갖춘 행 + '이미 한 번 상신했다' 는 이력."""
    A.execute("DELETE FROM dock_procure WHERE req_no=? AND vsl_nm='TEST VESSEL'", (req_no,))
    A.execute(
        "INSERT INTO dock_procure(vsl_nm, vsl_cd, req_no, cat_code, subject, svms_req_no, "
        "stg_quote, stg_vendor, stg_confirm, stg_order, svms_status, sub_quotes) "
        "VALUES('TEST VESSEL','TSTV',?,'S',?,?,1,1,0,0,?,?)",
        (req_no, f'[DOCK][TSTV {req_no}]subject', rep_cd, svms_status,
         __import__('json').dumps([SUBMITTED])))
    r = A.query("SELECT id FROM dock_procure WHERE req_no=? AND vsl_nm='TEST VESSEL'",
                (req_no,), one=True)['id']
    mkdraft(r, rep_cd)
    return r


rid_ok = mkq('A5', 'HQ Rejected', 'TSTVES2608A51')
pv = c.get(f'/api/dock_submit/preview?rid={rid_ok}').get_json()
chk(not (pv or {}).get('blocked'), '반려 행 preview 가 막지 않음', pv)
r = c.post('/api/dock_submit/drafts', json={'rid': rid_ok, 'vndr_cd': 'A1CA8', 'app_no': '0002'})
chk(r.status_code == 201, '반려 행은 재상신 큐 생성 성공 = 큐 재적재', (r.status_code, r.get_json()))
chk(A.query("SELECT COUNT(*) n FROM dock_submit_draft WHERE rep_cd='TSTVES2608A51' "
            "AND status IN('approved','submitting')", one=True)['n'] == 1,
    '새 큐 1건만 생김')

print('# 6-1) 🔴 대조군 e2e — 결재 진행중 행은 라우트에서도 막힌다 (2차 상신 = 돈경로 사고)')
rid_no = mkq('A6', 'HQ Progressing', 'TSTVES2608A61')
pv2 = c.get(f'/api/dock_submit/preview?rid={rid_no}').get_json()
chk(bool((pv2 or {}).get('blocked')), '진행중 행 preview 가 사유와 함께 차단', pv2)
r2 = c.post('/api/dock_submit/drafts', json={'rid': rid_no, 'vndr_cd': 'A1CA8', 'app_no': '0002'})
chk(r2.status_code != 201, '진행중 행은 생성 라우트도 거부(fail-closed)', (r2.status_code, r2.get_json()))
chk(A.query("SELECT COUNT(*) n FROM dock_submit_draft WHERE rep_cd='TSTVES2608A61' "
            "AND status IN('approved','submitting')", one=True)['n'] == 0,
    '거부된 시도는 큐를 남기지 않음')

print('# 7) 화면 반려 판정 — 서버 rank 맵과 **정확일치** (올마이트 지적 수용: 부분일치 금지)')
#   부분일치면 rank 맵에 없는 미지 reject 라벨까지 "벤더제출로 되돌아옴" 이라 표시하는데,
#   그 라벨은 sync 가 단계를 되돌리지도 않아 화면이 거짓말이 된다 → 미지 라벨은 침묵(fail-closed).
tpl = open('templates/dock_procure.html', encoding='utf-8').read()
chk('sbmRejected' in tpl, '웹 템플릿에 반려 판정 헬퍼 존재')
chk('dp-sbmtag rej' in tpl, '반려 배지 클래스 사용')
chk("SBM_REJ_LABELS = ['hq rejected']" in tpl, '웹 판정 = 라벨 allowlist 정확일치', 'allowlist 없음')
chk('/reject/i' not in tpl, '부분일치 정규식은 제거됨')
ios = os.path.expanduser('~/.openclaw/workspace/trmt-mobile/ios/TRMT/Sources/Features/More/')
vmsrc = open(ios + 'DockProcureViewModel.swift', encoding='utf-8').read()
vwsrc = open(ios + 'DockProcureView.swift', encoding='utf-8').read()
chk('rejectedLabels: Set<String> = ["hq rejected"]' in vmsrc, 'iOS 도 같은 allowlist', 'allowlist 없음')
chk('.contains("reject")' not in vmsrc, 'iOS 부분일치 제거됨')
chk('submitRejected' in vwsrc and 'rejected: vm.isRejected(line)' in vwsrc,
    'iOS 카드가 vm 판정을 그대로 쓴다(두 화면 어긋남 방지)')
#   🔴 rank 맵에 있는 reject 라벨이 새로 생기면 화면 allowlist 도 같이 늘려야 한다 — 그걸 여기서 잡는다.
rank_rej = {k for k in A._DOCKPROC_STATUS_RANK if 'REJECT' in k}
chk(rank_rej == {'HQ REJECTED'},
    "서버 rank 맵의 reject 라벨은 'HQ REJECTED' 하나 — 늘었으면 웹/iOS allowlist 도 갱신", rank_rej)

print('# 7-1) 미지/유사 라벨은 반려로 보지 않는다 (rank 도 0 이라 단계를 안 건드림)')
for lab in ('Rejected', 'HQ Reject', 'Approval Rejected', 'VSL Rejected'):
    chk(A._dockproc_status_rank(lab) != 2,
        f"'{lab}' 은 rank 2 가 아니다 → 화면도 반려라고 말하면 안 됨", A._dockproc_status_rank(lab))

print()
print(('❌ FAIL: ' + ', '.join(fails)) if fails else '✅ 전부 통과')
sys.exit(1 if fails else 0)
