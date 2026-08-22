/* 입거 Daily Report 표 내용 규칙 실행형 테스트 (node --test).
 * 같은 계약이 서버 `_table_grid`(tests/test_dock_daily.py) 와 아이폰
 * DockDailyBlockContentTests 에도 있다. 한쪽만 고치면 같은 표가 기기마다 다른
 * 모양으로 저장된다. */
'use strict';
const test = require('node:test');
const assert = require('node:assert');
const T = require('../static/js/dock_daily_table.js');

test('빈 값에서 기본 골격을 만든다 — 앱과 같은 첫 모양', () => {
  assert.deepStrictEqual(T.empty(), {columns: ['항목', '내용'], rows: [['', '']]});
  assert.deepStrictEqual(T.read(null), T.empty());
  assert.deepStrictEqual(T.read({}), T.empty());
  assert.deepStrictEqual(T.read({columns: [], rows: []}), T.empty());
});

// 🔴 확정본 표시는 서버 `_table_grid` 와 한 글자도 달라선 안 된다. 편집기용 기본 골격을
// 거기에 쓰면 메일이 통째로 버리는 빈 표가 화면에만 `항목|내용` 으로 뜬다.
test('🔴 grid() 는 빈 표에 기본 골격을 넣지 않는다 (서버 _table_grid 와 동일)', () => {
  assert.deepStrictEqual(T.grid(null), {columns: [], rows: []});
  assert.deepStrictEqual(T.grid({columns: [], rows: []}), {columns: [], rows: []});
  // 열이 있으면 행 0 개도 그대로 0 개다 -- 서버도 빈 <tbody> 를 만든다.
  assert.deepStrictEqual(T.grid({columns: ['A'], rows: []}), {columns: ['A'], rows: []});
  // 값이 있는 표는 read() 와 같은 사각형을 낸다.
  assert.deepStrictEqual(T.grid({columns: ['A'], rows: [['1', '2']]}),
                         T.read({columns: ['A'], rows: [['1', '2']]}));
});

// 🔴 서버·앱과 같은 규칙. 자르면 화면에 열고 저장하는 것만으로 칸이 사라진다.
test('🔴 헤더보다 긴 행은 자르지 않고 열을 넓힌다', () => {
  const g = T.read({columns: ['A'], rows: [['1', '2', '3']]});
  assert.deepStrictEqual(g.columns, ['A', '', '']);
  assert.deepStrictEqual(g.rows, [['1', '2', '3']]);
});

test('짧은 행은 빈 칸으로 채운다', () => {
  const g = T.read({columns: ['A', 'B', 'C'], rows: [['1'], ['1', '2', '3']]});
  assert.deepStrictEqual(g.rows, [['1', '', ''], ['1', '2', '3']]);
});

test('배열이 아닌 행은 버리지 않고 한 칸 행으로 살린다', () => {
  const g = T.read({columns: ['A', 'B'], rows: ['통짜문장', ['1', '2']]});
  assert.deepStrictEqual(g.rows, [['통짜문장', ''], ['1', '2']]);
});

test('null·숫자 셀은 서버 렌더와 같게 문자열로 읽는다', () => {
  const g = T.read({columns: ['A', 'B'], rows: [[null, 12]]});
  assert.deepStrictEqual(g.rows, [['', '12']]);
  // 문자열 columns 는 글자 단위로 쪼개지 않는다(서버 `isinstance(list, tuple)` 게이트).
  assert.deepStrictEqual(T.read({columns: 'AB', rows: []}), T.empty());
});

test('행이 하나도 없으면 빈 행 한 줄을 넣는다 — 서버가 빈 표를 메일에서 버린다', () => {
  assert.deepStrictEqual(T.read({columns: ['A', 'B'], rows: []}).rows, [['', '']]);
});

test('셀·열 이름 수정', () => {
  const base = T.empty();
  assert.deepStrictEqual(T.setCell(base, 0, 1, '값').rows, [['', '값']]);
  assert.deepStrictEqual(T.setColumn(base, 0, '구분').columns, ['구분', '내용']);
  // 범위 밖은 조용히 무시한다(화면과 state 가 한 틱 어긋날 수 있다).
  assert.deepStrictEqual(T.setCell(base, 9, 0, 'x'), base);
  assert.deepStrictEqual(T.setColumn(base, 9, 'x'), base);
});

test('🔴 원본을 건드리지 않는다', () => {
  const base = T.empty();
  T.setCell(base, 0, 0, '값'); T.addRow(base); T.addColumn(base); T.removeRow(base, 0);
  assert.deepStrictEqual(base, {columns: ['항목', '내용'], rows: [['', '']]});
});

test('행·열 추가는 사각형을 유지한다', () => {
  const g = T.addColumn(T.addRow(T.empty()));
  assert.deepStrictEqual(g.columns, ['항목', '내용', '']);
  assert.deepStrictEqual(g.rows, [['', '', ''], ['', '', '']]);
});

test('열을 지우면 모든 행에서 같은 칸이 빠진다', () => {
  const g = T.read({columns: ['A', 'B', 'C'], rows: [['1', '2', '3'], ['4', '5', '6']]});
  const cut = T.removeColumn(g, 1);
  assert.deepStrictEqual(cut.columns, ['A', 'C']);
  assert.deepStrictEqual(cut.rows, [['1', '3'], ['4', '6']]);
});

test('행 삭제', () => {
  const g = T.read({columns: ['A'], rows: [['1'], ['2'], ['3']]});
  assert.deepStrictEqual(T.removeRow(g, 1).rows, [['1'], ['3']]);
});

// 🔴 0 이 되면 normalize 가 기본 골격을 되살려서, 버튼이 먹은 것도 안 먹은 것도 아닌
// 화면이 된다. 마지막 한 줄/한 칸은 못 지우게 막고 버튼도 disabled 로 그린다.
test('🔴 마지막 행·열은 지울 수 없다', () => {
  const g = T.empty();
  assert.strictEqual(T.canRemoveRow(g), false);
  assert.strictEqual(T.canRemoveColumn(g), true);
  assert.deepStrictEqual(T.removeRow(g, 0), g);
  const one = T.read({columns: ['A'], rows: [['1']]});
  assert.strictEqual(T.canRemoveColumn(one), false);
  assert.deepStrictEqual(T.removeColumn(one, 0), one);
  assert.strictEqual(T.canRemoveRow(T.addRow(g)), true);
});

test('범위 밖 삭제는 아무 일도 하지 않는다', () => {
  const g = T.read({columns: ['A', 'B'], rows: [['1', '2'], ['3', '4']]});
  assert.deepStrictEqual(T.removeRow(g, 9), g);
  assert.deepStrictEqual(T.removeColumn(g, 9), g);
  assert.deepStrictEqual(T.removeRow(g, -1), g);
});
