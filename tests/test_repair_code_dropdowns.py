#!/usr/bin/env python3
"""수리신청서 — **Reason Code / Department Code 드롭다운**을 잠그는 테스트.

형 요청(2026-08-23): "trmt 웹의 '수리신청서' 탭에서 reason code & Department code를 드롭다운으로
선택 가능하게 해줘. 이거 trmt ios는 해당 기능 있는데 웹만 안되었음". 실제로 웹은
`<input value="P">` 자유입력이었고 iOS 는 Picker 였다 — 서버는 두 값을 whitelist 하지 않고
`.upper()` 만 해서 저장하므로, 웹에서 오타를 치면 그대로 SVMS 로 나갔다.

잠그는 것:
  ① 웹은 두 칸 모두 `<select required>` 다(자유입력 `<input id="rr-reason">` 는 없다).
  ② 웹 목록이 iOS `RepairRequestView` 의 `reasonOptions`/`departmentOptions` 와 **코드·이름·순서까지
     같다**. 한쪽만 늘리면 같은 신청서를 다른 쪽에서 열었을 때 그 코드가 목록 밖 값이 된다.
  ③ 🔴 목록 밖 값 보존 / 빈 값 기본값 복귀 — 규칙은 `static/js/repair_code_select.js` 가 정본이고
     동작 검증은 `tests/repair_code_select.test.js`(node 실행형)가 한다. 여기서는 화면이 그 규칙을
     실제로 쓰고 있는지(배선)만 본다 — 규칙 파일이 있어도 template 이 안 부르면 아무 효과가 없다.
  ④ 서버는 여전히 whitelist 하지 않는다(이번 변경은 UI 편의이지 검증 강화가 아니다). ③의 전제라
     여기서 같이 못박는다 — 서버가 조용히 목록을 강제하면 ③이 무의미해지고 옛 초안 편집이 막힌다.

실행: ~/.venvs/trmt-test/bin/python tests/test_repair_code_dropdowns.py
"""
import os, re, sys, tempfile

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root (clone 위치 무관)
sys.path.insert(0, os.getcwd())
DB = tempfile.mktemp(suffix='.db')
os.environ['TRMT_DB'] = DB

import app as A  # noqa: E402
A.DATABASE = DB
A.app.config['DATABASE'] = DB
A.app.config['TESTING'] = True
A.app.config['SECRET_KEY'] = 'test'
A.init_db(drop=False)
A._auto_migrate()

fails = []


def chk(cond, name, extra=''):
    print(('  ok  ' if cond else '  ❌  ') + name + (f' — {extra}' if extra and not cond else ''))
    if not cond:
        fails.append(name)


tpl = open('templates/repair_requests.html', encoding='utf-8').read()

# ① 자유입력 → select
chk('<select id="rr-reason" required></select>' in tpl, '웹 Reason Code = select')
chk('<select id="rr-dept" required></select>' in tpl, '웹 Department Code = select')
chk('<input id="rr-reason"' not in tpl and '<input id="rr-dept"' not in tpl,
    '옛 자유입력 input 이 남아있지 않다')
chk("js/repair_code_select.js" in tpl, '규칙 파일을 template 이 실제로 불러온다')
chk('RepairCodeSelect.build(kind,value)' in tpl and 'RepairCodeSelect.label(o)' in tpl,
    'setCode() 가 규칙 파일의 결과를 그대로 그린다')
# 🔴 목록을 template 에 다시 적으면 두 벌이 되어 한쪽만 늘어난다.
chk('RR_REASONS' not in tpl and "['P','PMS']" not in tpl, 'template 에 코드 목록 사본이 없다')
chk("setCode($('#rr-reason'),'');setCode($('#rr-dept'),'')" in tpl,
    '초기화·reset() 이 두 칸을 기본값으로 다시 그린다')
chk(tpl.count("setCode($('#rr-reason'),'')") >= 2, '초기 렌더와 reset() 양쪽에 있다')
chk("setCode($('#rr-reason'),r.reason_cd);setCode($('#rr-dept'),r.dept_cd)" in tpl,
    'edit() 이 두 코드를 setCode 로 넣는다')
chk('reason:r.reason_cd' not in tpl and 'dept:r.dept_cd' not in tpl,
    'edit() 의 일괄 .value 대입 루프에서 두 코드가 빠졌다(select 에 .value 직접 대입 금지)')
chk("reason_cd:$('#rr-reason').value" in tpl and "dept_cd:$('#rr-dept').value" in tpl,
    'payload() 는 그대로 .value 를 보낸다')
chk('sel.value=built.selected' in tpl, '그린 뒤 선택값까지 명시로 맞춘다')

# ② 웹 ↔ iOS 목록 파리티 — 규칙 파일이 정본
js = open('static/js/repair_code_select.js', encoding='utf-8').read()


def js_list(name):
    m = re.search(r'var ' + name + r' = \[(.*?)\];', js, re.S)
    return re.findall(r"\['([^']+)', '([^']*)'\]", m.group(1)) if m else []


web_reason, web_dept = js_list('REASONS'), js_list('DEPTS')
chk(len(web_reason) == 14, '웹 Reason 14개', len(web_reason))
chk(web_reason[0] == ('P', 'PMS'), '웹 Reason 첫 항목 P=PMS', web_reason[:1])
chk(web_dept == [('D', 'Deck'), ('E', 'Engine')], '웹 Department = Deck/Engine', web_dept)
chk("DEFAULTS = { reason: 'P', dept: 'E' }" in js, '기본값 P/E')

ios_file = os.path.expanduser('~/.openclaw/workspace/trmt-mobile/ios/TRMT/Sources/'
                              'Features/More/RepairRequestView.swift')
if not os.path.exists(ios_file):
    print('  ⚠️ SKIP — iOS 소스 경로 없음(%s). 이 머신에선 웹 계약만 검사함.' % ios_file)
else:
    swift = open(ios_file, encoding='utf-8').read()

    def ios_list(name):
        m = re.search(r'let ' + name + r' = \[(.*?)\n    \]', swift, re.S)
        return re.findall(r'CodeOption\(code: "([^"]+)", name: "([^"]*)"\)', m.group(1)) if m else []

    chk(ios_list('reasonOptions') == web_reason, '웹↔iOS Reason 목록 동일(코드·이름·순서)',
        f"ios={ios_list('reasonOptions')} web={web_reason}")
    chk(ios_list('departmentOptions') == web_dept, '웹↔iOS Department 목록 동일',
        f"ios={ios_list('departmentOptions')} web={web_dept}")

# ④ 서버는 whitelist 하지 않는다 — ③의 전제
A.app.app_context().push()
with A.app.app_context():
    A.execute("INSERT INTO vessels(name,vsl_cd,active) VALUES('CODE VESSEL','CDVL',1)")
    vid = A.query("SELECT id FROM vessels WHERE vsl_cd='CDVL'", one=True)['id']
c = A.app.test_client()
with c.session_transaction() as s:
    s['user_id'] = 1; s['username'] = 'smoke'; s['role'] = 'admin'
body = dict(client_request_id='code-dropdown-1', vessel_id=vid, subject='code test',
            category='M/E', equipment='M/E', maker='', type_nm='', app_voy='001E',
            app_port_cd='KRPUS', app_dt='2026-08-23', cause='c', inspection='i', detail='d',
            stock='vendor', reason_cd='z9', dept_cd='q', dock_yn=False, urgent_yn=False,
            critical_yn=False)
r = c.post('/api/repair-requests', json=body)
chk(r.status_code == 201, '목록 밖 코드도 서버가 받는다(whitelist 없음)', r.get_data(as_text=True))
if r.status_code == 201:
    rid = r.get_json()['id']
    with A.app.app_context():
        row = A.query('SELECT reason_cd,dept_cd FROM repair_request WHERE id=?', (rid,), one=True)
    chk((row['reason_cd'], row['dept_cd']) == ('Z9', 'Q'),
        '목록 밖 코드는 대문자로 그대로 저장된다(편집 화면이 지켜야 하는 값)',
        (row['reason_cd'], row['dept_cd']))
    listed = c.get('/api/repair-requests').get_json()['requests']
    hit = [x for x in listed if x['id'] == rid]
    chk(bool(hit) and hit[0]['reason_cd'] == 'Z9' and hit[0]['dept_cd'] == 'Q',
        '목록 API 도 목록 밖 코드를 그대로 내려준다(edit() 이 받는 값)', hit[:1])

print()
print(('❌ FAIL: ' + ', '.join(fails)) if fails else '✅ 전부 통과')
sys.exit(1 if fails else 0)
