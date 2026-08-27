#!/usr/bin/env python3
"""/liscr 카드 화면 계약 — "못 읽은 값을 손으로 채운다" 가 **화면에서 실제로 되는가**.

2026-08-19 형 스크린샷으로 드러난 것들이라, 코드가 아니라 **형이 본 화면**이 기준이다.
서버 테스트로는 못 잡는 종류(전부 카드가 브라우저에서 그려지는 부분)라 템플릿 소스를 건다.

지키는 계약:
  · 🔴 **iOS Safari 는 값이 빈 `input[type=date]` 를 접어버린다.** 형 화면에서 INV_DT·Remit
        칸이 아예 안 보였던 이유고(다른 칸은 다 상자가 보였다), 그러면 "달력으로 고르라" 는
        말이 성립하지 않는다. appearance 해제 + 최소 크기가 그 방어선이다.
  · 🔴 **통화는 드롭다운이고, 지금 값이 목록에 없으면 그 값이 첫 옵션으로 남아야 한다.**
        안 그러면 select 가 조용히 첫 통화로 내려앉아 **형이 본 통화와 다른 통화로 승인**된다.
  · 🔴 **7초 폴링이 손입력을 지우면 안 된다.** 자유서식은 손입력이 정상 경로라 네 칸 채우는
        동안 화면이 두 번 갈아엎인다. 입력 중엔 안 그리고, 그릴 땐 담아둔 값을 되돌린다.

실행: python3 tests/test_liscr_card_ui.py
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = open(os.path.join(ROOT, 'templates', 'liscr.html'), encoding='utf-8').read()

fails = []


def chk(cond, name, extra=''):
    print(('  ok  ' if cond else '  ❌  ') + name + (f' — {extra}' if extra and not cond else ''))
    if not cond:
        fails.append(name)


print('# 1) 날짜칸이 iOS 에서 접히지 않는다 (형 화면에서 안 보이던 그 칸)')
m = re.search(r'\.lz-in-date\{([^}]*)\}', SRC)
rule = m.group(1) if m else ''
chk(bool(m), '.lz-in-date 규칙이 있음')
chk('-webkit-appearance:none' in rule,
    '🔴 appearance 해제 — 이게 없으면 빈 날짜칸이 폭 0 으로 접힌다', rule)
chk(not re.search(r'(?<!-)\bappearance:none', rule),
    '표준 appearance 는 안 건드림(증상 없는 브라우저의 날짜칸까지 바꾸지 않게)', rule)
chk('min-width' in rule and 'min-height' in rule,
    '🔴 최소 폭·높이를 직접 박음(값이 비어도 상자가 남는다)', rule)
chk('::-webkit-calendar-picker-indicator' in SRC,
    '데스크톱 달력 아이콘은 다시 켜둠(appearance 해제의 부작용 차단)')
chk(SRC.count('type="date"') >= 1 and 'lz-in-date' in SRC, '날짜칸은 여전히 캘린더 입력')
chk('is-empty' in SRC and '달력에서 선택' in SRC,
    '아직 안 고른 날짜는 표시가 남는다(빈칸이 정상인 카드라 안내가 필요)')

print('\n# 2) 통화는 드롭다운 — 자유입력 칸이 아니다')
chk('<select class="lz-in-cur" data-f="cur_cd">' in SRC,
    '🔴 카드의 통화가 select', 'input 이면 형이 직접 타이핑해야 한다')
chk('<input class="lz-in-cur"' not in SRC, '옛 자유입력 칸이 남아 있지 않음')
chk('<select id="op-cur">' in SRC, '업로드 폼의 통화도 select')
chk('dl-cur' not in SRC, '쓰지 않는 통화 datalist 는 치웠음')

print('\n# 3) 폴링이 손입력을 지우지 않는다')
chk(re.search(r'setInterval\(\(\)=>\{if\(document\.hidden\) return; if\(editing\(\)\)\{paused\(true\);return;\} load\(\);\}', SRC) is not None,
    '🔴 입력 중(목록 안에 포커스)이면 다시 그리지 않음', '없으면 타이핑 중 칸이 사라진다')
chk('if(editing()){paused(true);return;}' in SRC.split('list.innerHTML=jobs.length')[0].split('async function load()')[-1],
    '🔴 응답을 받은 **뒤에도** 한 번 더 본다(요청↔응답 사이에 손댄 칸이 갈아엎이는 경합 차단)')
chk('restoreDirty();' in SRC and 'list.innerHTML=jobs.length' in SRC,
    '🔴 다시 그린 뒤 담아둔 손입력을 되돌림')
chk("addEventListener('focusout'" in SRC and 'if(STALE&&!editing()) load();' in SRC,
    '입력이 끝나면 미뤄둔 갱신을 바로 따라잡음(멈춤이 최대 7초로 늘어지지 않게)')
chk('id="lz-paused"' in SRC and '자동 갱신을 잠시 멈췄습니다' in SRC and 'function paused(on)' in SRC,
    '🔴 멈춘 사실이 화면에 보임(조용한 멈춤은 버그로 보인다)')
chk('if(res.ok) delete DIRTY[id];' in SRC,
    '카드를 떠나보내면 담아둔 값도 버림(다시 읽기가 옛 입력에 덮이지 않게)')
chk("el.tagName==='SELECT'&&!Array.from(el.options).some(o=>o.value===rec.v)" in SRC,
    '🔴 되돌릴 때 select 에 없는 값은 넣지 않음(첫 옵션으로 내려앉는 조용한 치환 차단)')
chk('if(srvVal(el)!==rec.base){delete DIRTY[id][f];return;}' in SRC,
    '🔴 그 사이 서버값이 바뀌었으면 손입력을 버림(러너가 새로 읽은 값을 옛 입력이 덮지 않게)')
chk('const srvVal=el=>' in SRC and "el.querySelector('option[selected]')" in SRC and 'el.defaultValue' in SRC,
    '기준값은 서버가 그려준 값에서 뽑음(input=value 속성 / select=selected 옵션)')
chk('AUTOINCREMENT' in open(os.path.join(ROOT, 'app.py'), encoding='utf-8').read().split('liscr_job')[1][:400],
    '🔴 id 재사용 없음이 전제 — DIRTY 를 id 로만 걸어도 남의 카드에 값이 들어가지 않는다')

print('\n# 3-b) Vendor·Expense 는 카드마다 고른다 (2026-08-19 형 지시)')
chk("code('vndr_cd','Vendor','lz-in-vndr','dl-vendor'" in SRC,
    '🔴 카드에 Vendor 입력칸이 있음', '없으면 벤더가 섞인 묶음을 한 번에 못 올린다')
chk("code('exp_cd','Expense','lz-in-exp','dl-exp'" in SRC, '🔴 카드에 Expense 입력칸이 있음')
chk('data-f="${f}"' in SRC and 'list="${dl}"' in SRC and '+vndrEd' in SRC and '+expEd' in SRC,
    '그 칸이 실제로 카드 편집줄에 붙는다(승인이 읽는 건 [data-f] 뿐이다)')
chk("!isLocked(prof,'vendor')" in SRC and "!isLocked(prof,'expense')" in SRC,
    '잠긴 프리셋(기국)에서는 칸 자체를 안 만듦 — "고정" 이 기본값으로 내려앉지 않게')
chk('비우면 카드마다' in SRC and 'Vendor (선택)' in SRC,
    '업로드 폼의 Vendor·Expense 는 선택 — 여기서 하나로 정하면 묶음 전체가 한 벤더가 된다')
chk("alert('Vendor 코드를 지정해야 합니다.')" not in SRC,
    '🔴 업로드에서 Vendor 를 다시 필수로 막지 않음(그 순간 이 기능이 무의미해진다)')
chk('function vendorChanged' in SRC and "cardEl.querySelector('[data-f=\"pay_dt\"]')" in SRC,
    '🔴 벤더를 바꾸면 Remit 을 비움 — PAY_TERM 은 벤더마다 다르고 계산은 러너만 한다')
chk("ev==='change'&&el.classList.contains('lz-in-vndr')" in SRC,
    "Remit 비우기는 'change' 에서만(타이핑 한 글자마다 지우면 화면이 흔들린다)")
chk('function showName' in SRC and 'SVMS 생성 직전 확인' in SRC,
    '🔴 최근사용 표본 밖 Vendor 는 SVMS 실조회 전임을 코드 옆에 표시')
chk('lz-in-vndr.is-empty' in SRC and 'lz-in-exp.is-empty' in SRC,
    '아직 안 고른 칸은 날짜칸과 같은 표시(승인 눌러 400 보고 알 일이 아니다)')
chk('Vendor ${body.vndr_cd}' in SRC and 'Remit ${body.pay_dt' in SRC,
    '🔴 승인 확인창에 벤더와 Remit 이 적힘(금전 경로 — 무엇으로 나가는지 보여주고 받는다)')
chk("if(v!=='') body[f]=v;" not in SRC and re.search(r'\n\s*body\[f\]=v;', SRC) is not None,
    '🔴 빈칸도 그대로 보냄 — 안 보내면 서버가 **저장된 옛 벤더**로 승인한다(화면은 빈칸인데)')
chk('채워야 하는 카드' in SRC,
    'FIX 카드는 전체 승인 대상이 아니라는 사실을 숫자로 말해줌(버튼이 그냥 사라지지 않게)')

print('\n# 4) curOptions 실동작 — 통화가 조용히 바뀌지 않는가 (node 로 실행)')
node = shutil.which('node')
if not node:
    print('  --  node 없음 — 이 절만 건너뜀(위 소스 계약은 이미 검사됨)')
else:
    fn = re.search(r'  function curOptions\(cur,withAuto\)\{.*?\n  \}\n', SRC, re.S)
    chk(fn is not None, 'curOptions 함수를 찾음')
    if fn:
        harness = """
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let MASTER={currencies:[{cd:'USD',n:9836},{cd:'KRW',n:9186},{cd:'EUR',n:161}]};
%s
const out=[];
const sel=h=>(h.match(/ selected/g)||[]).length;
const val=h=>{const m=h.match(/<option value="([^"]*)" selected/)||h.match(/value="([^"]*)"[^>]* selected/);return m?m[1]:null;};
// 형이 본 통화 그대로 선택돼 있어야 한다.
out.push(['마스터에 있는 통화가 선택됨', sel(curOptions('USD',false))===1 && val(curOptions('USD',false))==='USD']);
// 🔴 마스터에 없는 통화 — 사라지면 첫 통화(USD)로 승인이 나간다.
const unk=curOptions('XYZ',false);
out.push(['🔴 마스터에 없는 통화도 선택된 채 남음', sel(unk)===1 && val(unk)==='XYZ' && unk.includes('마스터에 없음')]);
out.push(['AUTO 는 업로드 폼에서만 뜸', curOptions('AUTO',true).includes('AUTO') && !curOptions('',false).includes('AUTO')]);
const blank=curOptions('',false);
out.push(['값이 없으면 빈 선택이 앞에 옴', sel(blank)===1 && val(blank)==='' && blank.indexOf('value=""')<blank.indexOf('USD')]);
out.push(['사용빈도 순서가 유지됨', curOptions('',false).indexOf('USD')<curOptions('',false).indexOf('EUR')]);
console.log(JSON.stringify(out));
""" % fn.group(0)
        with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, encoding='utf-8') as f:
            f.write(harness)
            path = f.name
        try:
            r = subprocess.run([node, path], capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                chk(False, 'node 실행', (r.stderr or '')[-400:])
            else:
                import json
                for name, ok in json.loads(r.stdout.strip().splitlines()[-1]):
                    chk(ok, name)
        finally:
            os.unlink(path)

print('\n' + ('❌ 실패 %d건: %s' % (len(fails), fails) if fails else '✅ 전부 통과'))
sys.exit(1 if fails else 0)
