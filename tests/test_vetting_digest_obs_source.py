#!/usr/bin/env python3
"""Vetting 요약행 OBS/OPEN 계약 — 소비처 3곳이 같은 값/공란을 내는지 실제 호출한다.

손유석 지시 2026-08-11: "메인 섹션의 OBS(전체/잔여)를 'Next Plan'일 경우, 해당 Next Plan의
OBS 및 OPEN 숫자가 표시되게"였으나, 2026-08-26 지시로 Next Plan 은 수검 전이므로
숫자 0/0 대신 공란/공란으로 정정했다. 직전 Report 폴백은 계속 폐기 상태다.

🔴 여기서 잡으려는 회귀: `_vetting_pick` 단위 테스트만으로는 소비처가 조용히 갈리는 걸 못 잡는다.
   웹(`vt.js`)·앱 위젯(`/api/widget/vetting`)·ext(`_ext_vetting_digests`)가 서로 다른 숫자를
   보여주면 형이 화면을 못 믿는다. 그래서 계획행과 직전 Report 의 수치를 **전부 다르게**
   깔아놓고(0 vs 0 이면 출처가 바뀌어도 티가 안 난다) 실제 응답값을 본다.

실행: ~/.venvs/trmt-test/bin/python tests/test_vetting_digest_obs_source.py
"""
import os, sys, tempfile

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root (clone 위치 무관)
sys.path.insert(0, os.getcwd())
DB = tempfile.mktemp(suffix='.db')
os.environ['TRMT_DB'] = DB

import app as A
from source_bundle import shared_ns

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

VNAME = 'TEST PROSPERITY'
A.execute("INSERT INTO vessels (name, vessel_type, imo, active) VALUES (?, 'VLCC', '9999999', 1)",
          (VNAME,))
VID = A.query('SELECT id FROM vessels WHERE name=?', (VNAME,), one=True)['id']


def add_vetting(valid, date, company, port, obs, open_, close):
    A.execute("INSERT INTO vettings (vessel_id, valid, inspection_date, inspection_company, port,"
              " manual_observation_count, manual_open_count, manual_close_count, overall_remark)"
              " VALUES (?,?,?,?,?,?,?,?,?)",
              (VID, valid, date, company, port, obs, open_, close, f'remark-{valid}-{date}'))


# 직전 Report 와 계획행의 수치를 전부 다르게 — 어느 쪽에서 왔는지 숫자만 보고 판정 가능해야 한다.
add_vetting('Last Result', '2026-05-01', 'SHELL', 'FUJAIRAH', 5, 2, 3)   # 직전 Report
add_vetting('Next Plan',   '',           'BP',    'SINGAPORE', 7, 4, 3)  # 상단(계획)

PLAN = dict(obs=7, open=4, closed=3, company='BP', port='SINGAPORE')
PREV = dict(obs=5, open=2)

# ---- 1) 서버 정본 helper ----
latest, enr = shared_ns._vetting_pick(VID)
chk(latest['valid'] == 'Next Plan', '_vetting_pick: 상단 = Next Plan')
chk(latest['observation_count'] == PLAN['obs'] and latest['open_count'] == PLAN['open'],
    '_vetting_pick: 상단행 자체 수치',
    f"{latest['observation_count']}/{latest['open_count']}")
chk(len(enr) == 2 and enr[0]['id'] == latest['id'], '_vetting_pick: enr[0] == latest')

# ---- 2) ext 요약(/api/ext/vetting-digests 본체) ----
row = next((d for d in shared_ns._ext_vetting_digests() if d['vessel_name'] == VNAME), None)
chk(row is not None, 'ext digest: 대상 선박 존재')
if row:
    chk(row['obs_total'] is None and row['obs_open'] is None,
        'ext digest: Next Plan OBS = 공란(null)', f"{row['obs_total']}/{row['obs_open']}")
    chk(row['status'] == 'Next Plan' and row['oil_major'] == PLAN['company'],
        'ext digest: 상태·오일메이저도 같은 행')
    # 🔴 화면 숫자는 어디서나 obs_*(요약행) 다 — vercel 카드도 fleet-map 도 탭의 미러이므로
    #    탭이 0/0 이면 카드도 0/0(형 지시 2026-08-11 11:07 "페루 5/0 으로 푸시됨, 수정해줘").
    #    report_obs_* 는 이제 **push.py 의 obsNote 클리어 판정 하나**만 쓴다 — "지적이 다 닫혔다"는
    #    실제 수검 결과의 사실이라, 다음 수검이 잡혔다고 형 메모를 지우면 안 되기 때문.
    chk(row['report_obs_total'] == PREV['obs'] and row['report_obs_open'] == PREV['open'],
        'ext digest: report_obs_* = 직전 Report 값',
        f"{row.get('report_obs_total')}/{row.get('report_obs_open')}")

# ---- 3) 위젯(/api/widget/vetting) — iOS 앱/위젯이 그리는 값 ----
UID = A.query('SELECT id FROM users ORDER BY id LIMIT 1', one=True)['id']
with c.session_transaction() as s:
    s['user_id'] = UID
    s['username'] = 'admin'
    s['role'] = 'admin'
w = c.get('/api/widget/vetting')
chk(w.status_code == 200, '위젯 API 200', str(w.status_code))
wrow = next((r for r in w.get_json()['vetting'] if r['vessel'] == VNAME), None)
chk(wrow is not None, '위젯: 대상 선박 존재')
if wrow:
    chk(wrow['obs_total'] is None and wrow['obs_open'] is None and wrow['obs_closed'] is None,
        '위젯: Next Plan obs_* 전부 공란(null)',
        f"{wrow['obs_total']}/{wrow['obs_open']}/{wrow['obs_closed']}")
    # obs_oil_major/obs_date 는 폴백 시절 "수치의 출처"를 따로 알리던 키다. 폴백이 사라졌으니
    # 상단행과 같아야 한다 — 안 그러면 앱 부제에 지난 수검 메타가 섞여 보인다.
    chk(wrow['obs_oil_major'] == wrow['oil_major'] and wrow['obs_date'] == wrow['date'],
        '위젯: obs 메타 == 상단행 메타')

# ---- 4) 계획이 없으면 기존과 동일(직전 Report 가 그대로 상단) ----
A.execute("DELETE FROM vettings WHERE vessel_id=? AND valid='Next Plan'", (VID,))
only, _enr = shared_ns._vetting_pick(VID)
chk(only['valid'] == 'Last Result' and only['observation_count'] == PREV['obs']
    and only['open_count'] == PREV['open'],
    'Next Plan 없으면 Report 수치 그대로(현행 동일)')
chk(shared_ns._vetting_summary_counts(only) == (PREV['obs'], PREV['open'], 3),
    'Last Result 요약은 실제 숫자 유지')

# ---- 5) Report 가 아예 없고 계획만 있을 때 — report_* 는 상단행으로 폴백(구 동작과 동일) ----
A.execute("DELETE FROM vettings WHERE vessel_id=?", (VID,))
add_vetting('Next Plan', '', 'BP', 'SINGAPORE', 7, 4, 3)
row2 = next((d for d in shared_ns._ext_vetting_digests() if d['vessel_name'] == VNAME), None)
chk(row2 is not None and row2['obs_total'] is None and row2['obs_open'] is None,
    '계획만 있을 때 화면/mirror OBS 는 공란')
chk(row2 is not None and row2['report_obs_total'] is None and row2['report_obs_open'] is None,
    '계획만 있을 때 report_* 는 모름(null) — obsNote 보존')

print()
if fails:
    print(f'❌ 실패 {len(fails)}건: ' + ', '.join(fails))
    sys.exit(1)
print('✅ Vetting 요약 OBS — Next Plan 공란 / Last Result 실수치 계약 일치')
