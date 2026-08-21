/* 입거 Daily Report 일자 목록 필터 실행형 테스트 (node --test).
 * 문자열 존재 확인이 아니라 규칙을 실제로 실행한다. 같은 계약이 아이폰 앱
 * DockDailyDateEditContractTests 에도 있으니 한쪽만 고치면 안 된다. */
'use strict';
const test = require('node:test');
const assert = require('node:assert');
const F = require('../static/js/dock_daily_filter.js');

const row = (date, status = 'auto_draft', id = 1) => ({id, report_date: date, status});

test('빈 검색어는 전부 통과한다', () => {
  for (const q of [undefined, null, '', '   ']) {
    assert.strictEqual(F.matches(row('2026-09-01'), q), true, JSON.stringify(q));
  }
});

test('날짜 원문과 숫자만 남긴 검색어가 모두 걸린다', () => {
  // 숫자 키패드로 구분자를 넣기 번거로우므로 숫자만 남긴 비교도 허용한다.
  for (const q of ['2026-09-01', '2026-09', '09-01', '0901', '20260901', '2026']) {
    assert.strictEqual(F.matches(row('2026-09-01'), q), true, q);
  }
});

test('🔴 엉뚱한 검색어는 걸리지 않는다 — 9/1 은 숫자열이 91 이라 의도적으로 제외', () => {
  for (const q of ['9/1', '2025', '08-20', '0820', 'abc']) {
    assert.strictEqual(F.matches(row('2026-09-01'), q), false, q);
  }
});

test('상태와 날짜는 AND 이고 서버가 준 순서를 유지한다', () => {
  const rows = [row('2026-09-01', 'editing', 3), row('2026-08-30', 'auto_draft', 2),
                row('2026-08-20', 'final', 1)];
  const ids = (q, s) => F.apply(rows, q, s).map(r => r.id);
  assert.deepStrictEqual(ids('', ''), [3, 2, 1], '상태 없음 = 전체');
  assert.deepStrictEqual(ids('', 'auto_draft'), [2]);
  assert.deepStrictEqual(ids('2026-08', ''), [2, 1]);
  assert.deepStrictEqual(ids('2026-08', 'editing'), [], '상태와 날짜는 AND');
  // 🔴 여기서 재정렬하면 사이드바(날짜 역순)와 앱 순서가 갈린다.
  assert.deepStrictEqual(ids('2026', ''), [3, 2, 1]);
});

test('report_date 가 없는 행에서도 죽지 않는다', () => {
  assert.strictEqual(F.matches({}, '0901'), false);
  assert.strictEqual(F.matches(null, ''), true);
  assert.deepStrictEqual(F.apply(null, '', ''), []);
});
