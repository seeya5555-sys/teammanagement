#!/usr/bin/env python3
"""Dock — **발주 주체(OWNER↔MANAGER) 태그 토글**을 잠그는 테스트.

형 요청(2026-08-04): "trmt 앱 발주 주체 owner/manager 태그를 색구분해줘 / 지금 모달에서만 바꿀수
있게 되어있는데 해당 오너/매니저 태그를 버튼으로 스위칭 할 수 있는 기능 (자재,수리,페인트,등등
**모든 항목**)". 서버 토글(`POST /api/dock_procure/<id>/prep`)은 이미 모든 cat_code 에 열려 있어서
이번 변경은 iOS 표시·조작뿐이다 — 그래서 이 파일은 ①서버 계약이 조용히 좁아지지 않게 ②두 화면
(웹/iOS)이 어긋나지 않게 고정한다.

잠그는 것:
  ① 모든 cat_code(R·S·ST·P·SY)에서 토글이 200 이고 값이 실제로 반전된다 — 카테고리 제한 0.
  ② `source` 는 항상 `_dockproc_source` 규칙을 따른다: P·SY=MAIL 고정 / MANAGER=AOR / OWNER=SVMS.
     (화면이 자체판정하면 안 되는 값 — 서버가 내려주는 걸 그대로 쓴다.)
  ③ 명시값(OWNER/MANAGER)은 그대로, 쓰레기값은 반전으로 떨어진다. 없는 id 는 404.
  ④ 웹은 색 CSS + 클릭 토글을 계속 갖고 있다. iOS 는 같은 색 매핑(OWNER=green/MANAGER=amber)의
     `DPPrepBadge` 를 **카드에서** 쓰고, 카드 호출부가 `vm.togglePrep` 에 배선돼 있다
     (배선이 빠지면 배지는 그려지는데 눌러도 안 바뀐다 = 형이 바로 만나는 버그).

실행: ~/.venvs/trmt-test/bin/python tests/test_dockproc_prep_toggle.py
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


def mkrow(req_no, cat_code, prepared_by):
    A.execute("DELETE FROM dock_procure WHERE req_no=? AND vsl_nm='TEST VESSEL'", (req_no,))
    A.execute(
        "INSERT INTO dock_procure(vsl_nm, vsl_cd, req_no, cat_code, subject, prepared_by, source) "
        "VALUES('TEST VESSEL','TSTV',?,?,?,?,?)",
        (req_no, cat_code, f'[DOCK][TSTV {req_no}]subject', prepared_by,
         A._dockproc_source(cat_code, prepared_by)))
    return A.query("SELECT * FROM dock_procure WHERE req_no=? AND vsl_nm='TEST VESSEL'",
                   (req_no,), one=True)['id']


def dbrow(lid):
    return A.query("SELECT prepared_by, source FROM dock_procure WHERE id=?", (lid,), one=True)


print('# 1) 🔴 모든 카테고리에서 토글된다 (형 "자재,수리,페인트,등등 모든 항목")')
#   R=수리 S=자재 ST=선용품 P=페인트 SY=조선소 — 하나라도 막히면 형 요청이 반쪽이 된다.
for i, cat in enumerate(('R', 'S', 'ST', 'P', 'SY')):
    lid = mkrow(f'T1{i}', cat, 'OWNER')
    r = c.post(f'/api/dock_procure/{lid}/prep')
    j = r.get_json() if r.status_code == 200 else {}
    row = dbrow(lid)
    chk(r.status_code == 200, f'[{cat}] 토글 200', r.status_code)
    chk(j.get('prepared_by') == 'MANAGER', f'[{cat}] OWNER→MANAGER 응답', j)
    chk(row['prepared_by'] == 'MANAGER', f'[{cat}] DB 에도 반영', dict(row))
    # 되돌리기도 같은 라우트로 — 한 방향만 되면 화면에서 못 빠져나온다.
    r2 = c.post(f'/api/dock_procure/{lid}/prep')
    chk(r2.get_json().get('prepared_by') == 'OWNER', f'[{cat}] MANAGER→OWNER 복귀', r2.get_json())

print()
print('# 2) 🔴 source 는 서버 규칙(_dockproc_source)을 그대로 따른다 — 화면 자체판정 금지')
#   P·SY 는 메일견적이라 담당이 누구든 MAIL 고정. R/S/ST 는 MANAGER=AOR / OWNER=SVMS.
EXPECT = {('R', 'MANAGER'): 'AOR', ('R', 'OWNER'): 'SVMS',
          ('S', 'MANAGER'): 'AOR', ('S', 'OWNER'): 'SVMS',
          ('ST', 'MANAGER'): 'AOR', ('ST', 'OWNER'): 'SVMS',
          ('P', 'MANAGER'): 'MAIL', ('P', 'OWNER'): 'MAIL',
          ('SY', 'MANAGER'): 'MAIL', ('SY', 'OWNER'): 'MAIL'}
for i, ((cat, nv), src) in enumerate(EXPECT.items()):
    lid = mkrow(f'T2{i}', cat, 'MANAGER' if nv == 'OWNER' else 'OWNER')
    j = c.post(f'/api/dock_procure/{lid}/prep', json={'prepared_by': nv}).get_json()
    row = dbrow(lid)
    chk(j.get('source') == src and row['source'] == src,
        f'[{cat}] {nv} → source={src}', f"resp={j} db={dict(row)}")

print()
print('# 3) 명시값·쓰레기값·없는 행')
lid = mkrow('T30', 'S', 'OWNER')
chk(c.post(f'/api/dock_procure/{lid}/prep', json={'prepared_by': 'owner'}).get_json()
    .get('prepared_by') == 'OWNER', '소문자 명시값도 OWNER 로 정규화(반전 아님)')
chk(dbrow(lid)['prepared_by'] == 'OWNER', '같은 값 재지정은 그대로 유지')
j = c.post(f'/api/dock_procure/{lid}/prep', json={'prepared_by': 'ADMIN'}).get_json()
chk(j.get('prepared_by') == 'MANAGER', '허용 밖 값은 무시하고 반전으로 떨어진다', j)
chk(c.post('/api/dock_procure/99999999/prep').status_code == 404, '없는 행은 404')

print()
print('# 3-1) 로그인만 있으면 되고 admin 게이트는 없다 (웹과 같은 조건 — 감독도 담당을 바꾼다)')
#   담당 표기는 돈경로가 아니라 화면 분류값이다. 여기에 admin 게이트가 생기면 형이 폰에서 못 바꾼다.
lid = mkrow('T31', 'S', 'OWNER')
with c.session_transaction() as s:
    s['user_id'] = 2; s['username'] = 'super'; s['role'] = 'user'
chk(c.post(f'/api/dock_procure/{lid}/prep').status_code == 200, '비-admin 도 토글 200')
anon = A.app.test_client()
chk(anon.post(f'/api/dock_procure/{lid}/prep').status_code in (302, 401, 403),
    '로그인 없으면 막힌다(@login_required)', anon.post(f'/api/dock_procure/{lid}/prep').status_code)
with c.session_transaction() as s:
    s['user_id'] = 1; s['username'] = 'smoke'; s['role'] = 'admin'

print()
print('# 4) 화면 계약 — 웹/iOS 가 같은 색 매핑과 같은 토글을 갖는다')
tpl = open('templates/dock_procure.html', encoding='utf-8').read()
chk('.dp-prep.OWNER{background:var(--green-bg)' in tpl, '웹 OWNER=green CSS')
chk('.dp-prep.MANAGER{background:var(--amber-bg)' in tpl, '웹 MANAGER=amber CSS')
chk(".querySelectorAll('.dp-prep')" in tpl, '웹 태그 클릭 토글 유지')

#   ⚠️ iOS 정본은 이 repo 밖(ws repo)에 있다 — 다른 머신엔 없을 수 있어 없으면 크게 SKIP 을 찍고
#      웹 검사만 한다. 있는데 내용이 틀리면 정상적으로 FAIL 한다(조용히 통과시키지 않는다).
ios = os.path.expanduser('~/.openclaw/workspace/trmt-mobile/ios/TRMT/Sources/Features/More/')
if not os.path.isdir(ios):
    print('  ⚠️ SKIP — iOS 소스 경로 없음(%s). 이 머신에선 웹 계약만 검사함.' % ios)
    print()
    print(('❌ FAIL: ' + ', '.join(fails)) if fails else '✅ 전부 통과(iOS 검사 SKIP)')
    sys.exit(1 if fails else 0)
vwsrc = open(ios + 'DockProcureView.swift', encoding='utf-8').read()
chk('struct DPPrepBadge' in vwsrc, 'iOS 발주주체 배지 컴포넌트 존재')
#   🔴 미지값을 초록(OWNER 색)으로 칠하면 이상 데이터가 정상처럼 보인다 — 회색 폴백을 고정한다.
chk('default:        return (Theme.surface3, Theme.textSecondary, Theme.borderSubtle)' in vwsrc,
    'OWNER/MANAGER 아닌 값은 중립 회색(초록 오독 방지)')
#   🔴 배지는 작게 두고 탭 영역만 넓힌다 — contentShape 가 빠지면 히트영역이 글자 크기로 줄어든다.
chk('.contentShape(Rectangle())' in vwsrc, '배지 탭 영역 확장(contentShape) 유지')
#   🔴 웹과 같은 매핑이어야 한다 — 색이 반대면 형이 화면 두 개를 반대로 읽는다.
chk('Theme.okBG, Theme.ok, Theme.okBorder' in vwsrc and 'Theme.warnBG, Theme.warn, Theme.warnBorder' in vwsrc,
    'iOS OWNER=green / MANAGER=amber (웹과 같은 매핑)')
chk('DPPrepBadge(value: line.prepared_by ?? "OWNER", busy: busy, action: onTogglePrep)' in vwsrc,
    '카드 태그가 배지 컴포넌트로 교체됨(회색 비활성 텍스트 아님)')
chk('Theme.surface3, fg: Theme.textSecondary, border: Theme.borderSubtle)\n                if let src'
    not in vwsrc, '카드에 옛 회색 prepared_by 텍스트가 남아있지 않다')
#   🔴 배선 확인 — 배지만 있고 이게 없으면 눌러도 아무 일도 안 난다.
chk('onTogglePrep: { Task { await vm.togglePrep(line) } }' in vwsrc,
    '카드 호출부가 vm.togglePrep 에 배선됨')
chk('let onTogglePrep: () -> Void' in vwsrc, 'DPLineCard 가 토글 콜백을 받는다')
vmsrc = open(ios + 'DockProcureViewModel.swift', encoding='utf-8').read()
chk('func togglePrep' in vmsrc and '$0.source = r.source' in vmsrc,
    'iOS 는 서버가 준 prepared_by·source 를 그대로 반영(자체 계산 금지)')

print()
print(('❌ FAIL: ' + ', '.join(fails)) if fails else '✅ 전부 통과')
sys.exit(1 if fails else 0)
