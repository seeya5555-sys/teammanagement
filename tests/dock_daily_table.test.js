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

// 🔴 형 지시 2026-08-22: "기존 섹션에 표추가는 필요 없어(ios랑 똑같이 해줘)".
// 앱 `DockDailySectionEditing.canAddTable` 과 같은 값이어야 한다.
test('🔴 표 추가 버튼은 빈 special 섹션에만 (앱 canAddTable 과 동일)', () => {
  const sp = {kind: 'special'}, fx = {kind: 'fixed'};
  assert.strictEqual(T.canAddTable(sp, 0, false), true);    // 고아 섹션 = 유일한 복구 경로
  assert.strictEqual(T.canAddTable(sp, 1, false), false);   // 이미 뭔가 있으면 안 붙인다
  assert.strictEqual(T.canAddTable(fx, 0, false), false);   // Shipyard/Survey/Vendor/Remark
  assert.strictEqual(T.canAddTable(sp, 0, true), false);    // 확정본
  assert.strictEqual(T.canAddTable(null, 0, false), false);
  // 🔴 개수를 안 넘긴 실수가 "표 버튼이 아무 데나 뜬다" 로 새어 나가면 안 된다.
  assert.strictEqual(T.canAddTable(sp, undefined, false), false);
  assert.strictEqual(T.canAddTable(sp, NaN, false), false);
});

test('범위 밖 삭제는 아무 일도 하지 않는다', () => {
  const g = T.read({columns: ['A', 'B'], rows: [['1', '2'], ['3', '4']]});
  assert.deepStrictEqual(T.removeRow(g, 9), g);
  assert.deepStrictEqual(T.removeColumn(g, 9), g);
  assert.deepStrictEqual(T.removeRow(g, -1), g);
});

/* ── 엑셀 표 붙여넣기 (형 지시 2026-08-23) ────────────────────────────────────
 * 같은 계약이 앱 `DockDailyTablePaste`(DockDailyBlockContentTests) 에도 있다. */

// 🔴 이게 뚫리면 칸 안에서 글자 몇 개 붙이는 평소 붙여넣기가 통째로 망가진다
// (붙인 값이 칸 전체를 덮어쓴다). 그래서 표가 아닌 글은 반드시 null 이다.
test('🔴 탭도 줄바꿈도 없는 글은 표가 아니다 — 평소 붙여넣기에 맡긴다', () => {
  assert.strictEqual(T.parseClipboard('Rope guard'), null);
  assert.strictEqual(T.parseClipboard(''), null);
  assert.strictEqual(T.parseClipboard(null), null);
  assert.strictEqual(T.pasteInto(T.empty(), {row: null, col: 0}, '한 칸 값'), null);
});

// 형이 말한 그 케이스: 2열×(헤더+1행) 표의 **첫 칸**에 5행 4열을 붙인다.
test('🔴 열 이름 칸에 붙이면 첫 줄이 열 이름이 되고 표가 그 크기로 맞춰진다', () => {
  const src = ['No\t항목\t조치\t비고',
               '1\tWBT No.1\tclean\tOK',
               '2\tWBT No.2\tclean\t',
               '3\tWBT No.3\tpainting\t진행',
               '4\tWBT No.4\tpainting\t진행'].join('\r\n') + '\r\n';
  const r = T.pasteInto(T.empty(), {row: null, col: 0}, src);
  assert.deepStrictEqual(r.grid.columns, ['No', '항목', '조치', '비고']);
  assert.strictEqual(r.grid.rows.length, 4);
  assert.deepStrictEqual(r.grid.rows[0], ['1', 'WBT No.1', 'clean', 'OK']);
  assert.deepStrictEqual(r.grid.rows[3], ['4', 'WBT No.4', 'painting', '진행']);
  assert.strictEqual(r.usedHeader, true);
  // 엑셀이 끝에 붙이는 줄바꿈이 빈 행으로 남지 않는다.
  assert.ok(r.grid.rows.every(row => row.some(v => v !== '')));
  assert.match(T.pasteNote(r), /4행 × 4열/);
  assert.match(T.pasteNote(r), /열 이름/);
  assert.match(T.pasteNote(r), /저장/);
});

// 🔴 본문 칸에 붙였을 때 열 이름을 먹으면, 형이 적어둔 열 이름이 데이터로 덮인다.
test('🔴 본문 칸에 붙이면 열 이름은 건드리지 않는다', () => {
  const base = T.read({columns: ['항목', '내용'], rows: [['기존', '값']]});
  const r = T.pasteInto(base, {row: 1, col: 0}, 'a\tb\nc\td\n');
  assert.deepStrictEqual(r.grid.columns, ['항목', '내용']);
  assert.deepStrictEqual(r.grid.rows, [['기존', '값'], ['a', 'b'], ['c', 'd']]);
  assert.strictEqual(r.usedHeader, false);
  assert.ok(!/열 이름/.test(T.pasteNote(r)));
});

// 🔴 통째로 갈아치우면 이미 적어둔 옆 열·아래 행이 붙여넣기 한 번에 사라진다.
test('🔴 붙인 사각형 밖의 칸은 지우지 않는다 (넓히기만)', () => {
  const base = T.read({columns: ['A', 'B', 'C'],
                       rows: [['a1', 'b1', 'c1'], ['a2', 'b2', 'c2'], ['a3', 'b3', 'c3']]});
  const r = T.pasteInto(base, {row: 0, col: 1}, 'X\nY\n');
  assert.deepStrictEqual(r.grid.rows, [['a1', 'X', 'c1'], ['a2', 'Y', 'c2'], ['a3', 'b3', 'c3']]);
  assert.deepStrictEqual(r.grid.columns, ['A', 'B', 'C']);
});

// 🔴 인용을 안 풀면 줄바꿈 든 칸 하나가 엉뚱한 행 두 개로 갈라진다.
test('🔴 엑셀 인용부호(줄바꿈·탭·"" 든 칸)를 한 칸으로 읽는다', () => {
  const p = T.parseClipboard('"두 줄\n한 칸"\t"탭\t포함"\t"큰""따옴표"\nx\ty\tz\n');
  assert.deepStrictEqual(p.rows, [['두 줄\n한 칸', '탭\t포함', '큰"따옴표'], ['x', 'y', 'z']]);
  assert.strictEqual(p.width, 3);
});

test('CR·CRLF·LF 를 모두 같은 행 구분으로 읽는다', () => {
  const want = [['a', 'b'], ['c', 'd']];
  assert.deepStrictEqual(T.parseClipboard('a\tb\r\nc\td').rows, want);
  assert.deepStrictEqual(T.parseClipboard('a\tb\nc\td').rows, want);
  assert.deepStrictEqual(T.parseClipboard('a\tb\rc\td').rows, want);
});

// 🔴 중간의 빈 행은 형이 비워 둔 행이다. 뒤의 빈 행만 떨어낸다.
test('🔴 중간 빈 행은 살리고 끝의 빈 행만 떨어낸다', () => {
  const p = T.parseClipboard('a\tb\n\t\nc\td\n\t\n\n');
  assert.deepStrictEqual(p.rows, [['a', 'b'], ['', ''], ['c', 'd']]);
  // 전부 빈 줄만 붙인 것은 붙일 값이 없다 -- 표를 늘리지 않는다.
  assert.strictEqual(T.parseClipboard('\n\t\n'), null);
});

// 🔴 서버 `_table_grid` 에는 표 크기 상한이 없다. 시트를 통째로 붙이면 칸 수천 개가
// 카드에 들어가고, 잘랐다고 말하지 않으면 엑셀의 행이 어디로 갔는지 알 수 없다.
test('🔴 상한을 넘으면 자르고, 자른 만큼을 말한다', () => {
  const lines = [];
  for (let i = 0; i < T.PASTE_MAX_ROWS + 3; i++) lines.push('r' + i);
  // 읽기 단계는 상한을 걸지 않는다 -- 자르기는 붙인 자리를 아는 `pasteInto` 몫이다.
  assert.strictEqual(T.parseClipboard(lines.join('\n') + '\n').rows.length, T.PASTE_MAX_ROWS + 3);
  const a = T.pasteInto(T.empty(), {row: 0, col: 0}, lines.join('\n') + '\n');
  assert.strictEqual(a.grid.rows.length, T.PASTE_MAX_ROWS);
  assert.strictEqual(a.droppedRows, 3);
  assert.match(T.pasteNote(a), /3행은 상한을 넘어 빠졌습니다/);
  const wide = [];
  for (let i = 0; i < T.PASTE_MAX_COLUMNS + 2; i++) wide.push('c' + i);
  const r = T.pasteInto(T.empty(), {row: null, col: 0}, wide.join('\t') + '\nx\n');
  assert.strictEqual(r.grid.columns.length, T.PASTE_MAX_COLUMNS);
  assert.strictEqual(r.droppedColumns, 2);
  assert.match(T.pasteNote(r), /2열은 상한을 넘어 빠졌습니다/);
});

// 🔴 상한은 **결과 표** 기준이다(올마이트 지적 2026-08-23). 원본 줄 수만 재던 옛 계산은
// 표 뒤쪽에 붙일 때 상한을 통과시켜 결과가 상한의 두 배가 되고, 아무 말도 안 했다.
test('🔴 표 뒤쪽에 붙여도 결과가 상한을 넘지 않는다 — 기존 행은 안 지운다', () => {
  const rows = [];
  for (let i = 0; i < T.PASTE_MAX_ROWS - 2; i++) rows.push(['old' + i]);
  const base = T.read({columns: ['A'], rows: rows});
  const paste = ['n0', 'n1', 'n2', 'n3', 'n4'].join('\n') + '\n';
  const r = T.pasteInto(base, {row: T.PASTE_MAX_ROWS - 2, col: 0}, paste);
  assert.strictEqual(r.grid.rows.length, T.PASTE_MAX_ROWS);
  assert.strictEqual(r.droppedRows, 3);              // 5줄 중 2줄만 들어간다
  assert.strictEqual(r.grid.rows[0][0], 'old0');     // 이미 있던 행은 그대로
  assert.strictEqual(r.grid.rows[T.PASTE_MAX_ROWS - 1][0], 'n1');
  assert.match(T.pasteNote(r), /3행은 상한을 넘어 빠졌습니다/);
  // 오른쪽 끝 열에 붙일 때도 같다.
  const c = T.pasteInto(T.read({columns: ['A'], rows: [['1']]}),
                        {row: 0, col: T.PASTE_MAX_COLUMNS - 1}, 'x\ty\tz\n');
  assert.strictEqual(c.grid.columns.length, T.PASTE_MAX_COLUMNS);
  assert.strictEqual(c.droppedColumns, 2);
});

// 🔴 형은 5줄 붙여넣기를 "5x4 표" 라고 부른다. 그 첫 줄은 열 이름이므로 `4행` 이라고만
// 쓰면 한 줄이 사라진 것으로 읽힌다(올마이트 지적 2026-08-23).
test('🔴 열 이름을 먹었으면 문구가 형의 셈으로 말한다', () => {
  const r = T.pasteInto(T.empty(), {row: null, col: 0},
                        'No\t항목\n1\t세척\n2\t검사\n');
  const note = T.pasteNote(r);
  assert.match(note, /열 이름 \+ 2행 × 2열/);
  assert.doesNotMatch(note, /첫 줄은 열 이름으로/);   // 숫자 안에 이미 들어갔다
  // 본문 칸에 붙였으면 열 이름 얘기를 하지 않는다.
  assert.match(T.pasteNote(T.pasteInto(T.empty(), {row: 0, col: 0}, 'a\tb\n')),
               /^붙여넣기로 1행 × 2열/);
});

// 🔴 엑셀이 아닌 곳에서 온 TSV 는 인용이 안 닫힌 채 온다(`"메모` 로 시작하는 줄). 인용
// 해석을 밀어붙이면 남은 줄 전부가 한 칸으로 먹힌다 -- 인용 없이 다시 읽어 표를 구한다.
test('🔴 인용이 안 닫히면 인용 없이 다시 읽는다 — 줄이 먹히지 않는다', () => {
  const p = T.parseClipboard('"메모\t값\n둘째\t줄\n');
  assert.deepStrictEqual(p.rows, [['"메모', '값'], ['둘째', '줄']]);
});

// ② 트레이드오프 잠금: 앱은 `UIPasteboard` 를 읽지 않아(두 번째 붙여넣기 권한 팝업)
// TextField 값을 파싱한다 -- 그래서 칸에 있던 글이 첫 칸에 붙는다. 값은 하나도 안 버린다.
test('붙인 칸에 있던 글은 첫 칸에 붙는다(값 손실 없음)', () => {
  const r = T.pasteInto(T.read({columns: ['A', 'B'], rows: [['', '']]}),
                        {row: 0, col: 0}, '기존글x\ty\n');
  assert.deepStrictEqual(r.grid.rows[0], ['기존글x', 'y']);
});

test('🔴 붙여넣기도 원본 grid 를 건드리지 않는다', () => {
  const base = T.read({columns: ['A', 'B'], rows: [['1', '2']]});
  T.pasteInto(base, {row: 0, col: 0}, 'x\ty\nz\tw\n');
  assert.deepStrictEqual(base, {columns: ['A', 'B'], rows: [['1', '2']]});
});

// 열 이름 칸에 붙이면서 기존 표보다 넓으면, 값은 하나도 안 잘리고 열이 늘어난다
// (서버 `_table_grid`·앱과 같은 사각형 규칙).
test('붙인 표가 더 넓으면 열이 늘어난다 — 값은 잘리지 않는다', () => {
  const base = T.read({columns: ['A', 'B'], rows: [['1', '2']]});
  const r = T.pasteInto(base, {row: 1, col: 0}, 'x\ty\tz\tw\n');
  assert.strictEqual(r.grid.columns.length, 4);
  assert.deepStrictEqual(r.grid.columns, ['A', 'B', '', '']);
  assert.deepStrictEqual(r.grid.rows, [['1', '2', '', ''], ['x', 'y', 'z', 'w']]);
});
