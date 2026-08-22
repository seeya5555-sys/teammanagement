/* 입거 Daily Report 표 내용 규칙(웹).
 *
 * 같은 계약이 서버 `routes_dock_daily._table_grid` 와 아이폰 `DockDailyTableContent`
 * 에도 있다. 세 곳이 갈리면 같은 표가 기기마다 다른 모양으로 저장되고, 메일에서만
 * 칸이 어긋난다. 한쪽을 고치면 나머지 둘도 같이 고쳐야 한다.
 *
 * 🔴 모든 함수는 **새 값을 돌려준다**. 넘긴 grid 를 제자리에서 고치면 화면이 아직
 * 옛 값을 들고 있는 동안 state 만 바뀌어, 저장 여부(`_edit`)를 못 붙인 수정이 생긴다.
 */
(function () {
  'use strict';
  // 앱 `DockDailyTableContent.defaultColumns` 와 같은 값이어야 한다 -- 웹에서 만든 표와
  // 앱에서 만든 표의 첫 모양이 다르면 형이 기기마다 열 이름을 다시 고치게 된다.
  var DEFAULT_COLUMNS = ['항목', '내용'];

  function text(v) { return (v === null || v === undefined) ? '' : String(v); }

  /** 표를 사각형으로 맞춘다.
   *
   * 🔴 **헤더보다 긴 행을 자르지 않는다**(서버·앱과 같은 규칙). 그 칸도 형이 적은 값이라
   * 자르면 화면에 열고 저장하는 것만으로 값이 사라진다 -- 대신 **열을 넓혀서** 맞춘다.
   * 값은 전부 남고 열 이름만 비어 있어 형이 채울 수 있다.
   *
   * 🔴 배열이 아닌 행도 버리지 않고 한 칸 행으로 살린다(서버 `_table_grid` 와 동일).
   */
  function normalize(grid) { return rectangle(grid, true); }

  /** 서버 `_table_grid` **그대로**: 빈 표에 기본 골격을 넣지 않는다.
   *
   * 🔴 보여주기만 하는 자리(확정본)는 이쪽을 쓴다. 편집기용 `normalize` 를 쓰면 서버가
   * 메일에서 통째로 버리는 빈 표를 화면에만 `항목|내용` 으로 그려서, 형이 보는 화면과
   * 실제로 나가는 메일이 갈린다.
   */
  function grid(content) {
    var c = (content && typeof content === 'object') ? content : {};
    return rectangle({columns: c.columns, rows: c.rows}, false);
  }

  function rectangle(src0, fillEmpty) {
    var src = (src0 && typeof src0 === 'object') ? src0 : {};
    var columns = Array.isArray(src.columns) ? src.columns.map(text) : [];
    var rows = [];
    (Array.isArray(src.rows) ? src.rows : []).forEach(function (row) {
      if (Array.isArray(row)) rows.push(row.map(text));
      else if (row !== null && row !== undefined && row !== '') rows.push([text(row)]);
    });
    var width = columns.length;
    rows.forEach(function (row) { if (row.length > width) width = row.length; });
    // 🔴 빈 표는 만들지 않는다(앱 normalize 와 동일). 열이 0 이면 채울 칸이 아예 없고,
    // 행이 0 이면 서버 `_mail_entries` 가 그 표를 통째로 버려서 형에게는 "표 추가가
    // 안 된다" 로 보인다.
    if (!width) {
      if (!fillEmpty) return {columns: [], rows: []};   // 서버 `_table_grid` 와 같은 값
      columns = DEFAULT_COLUMNS.slice(); width = columns.length;
    }
    while (columns.length < width) columns.push('');
    if (!rows.length && fillEmpty) rows.push([]);
    rows = rows.map(function (row) {
      var out = row.slice();
      while (out.length < width) out.push('');
      return out;
    });
    return {columns: columns, rows: rows};
  }

  /** 서버가 준 block content 를 읽는다. 숫자로 저장된 셀도 서버가 `str(v)` 로 렌더하므로
   * 여기서도 문자열로 받아들인다. */
  function read(content) {
    var c = (content && typeof content === 'object') ? content : {};
    return normalize({columns: c.columns, rows: c.rows});
  }

  /** 새 표 한 개. 앱 `defaultContent(for: .table)` 과 같은 모양이다. */
  function empty() { return normalize({columns: DEFAULT_COLUMNS.slice(), rows: []}); }

  function setCell(grid, rowIndex, colIndex, value) {
    var g = normalize(grid);
    if (!g.rows[rowIndex] || g.rows[rowIndex][colIndex] === undefined) return g;
    g.rows[rowIndex] = g.rows[rowIndex].slice();
    g.rows[rowIndex][colIndex] = text(value);
    return g;
  }

  function setColumn(grid, index, value) {
    var g = normalize(grid);
    if (g.columns[index] === undefined) return g;
    g.columns = g.columns.slice();
    g.columns[index] = text(value);
    return g;
  }

  function addRow(grid) {
    var g = normalize(grid);
    var row = [];
    for (var i = 0; i < g.columns.length; i++) row.push('');
    g.rows = g.rows.concat([row]);
    return g;
  }

  function addColumn(grid) {
    var g = normalize(grid);
    return normalize({columns: g.columns.concat(['']), rows: g.rows});
  }

  // 마지막 행/열은 지우지 않는다 -- 0 이 되면 normalize 가 기본 골격을 되살려서
  // 버튼이 아무 일도 안 한 것처럼 보인다.
  function canRemoveRow(grid) { return normalize(grid).rows.length > 1; }
  function canRemoveColumn(grid) { return normalize(grid).columns.length > 1; }

  function removeRow(grid, index) {
    var g = normalize(grid);
    if (g.rows.length <= 1 || !g.rows[index]) return g;
    g.rows = g.rows.filter(function (_, i) { return i !== index; });
    return g;
  }

  function removeColumn(grid, index) {
    var g = normalize(grid);
    if (g.columns.length <= 1 || g.columns[index] === undefined) return g;
    var keep = function (_, i) { return i !== index; };
    return normalize({columns: g.columns.filter(keep),
                      rows: g.rows.map(function (row) { return row.filter(keep); })});
  }

  /** 이 섹션 카드에서 표를 직접 만들 수 있는가. 앱 `DockDailySectionEditing.canAddTable`
   * 과 **같은 규칙**이다.
   *
   * 🔴 표는 원칙적으로 "표 섹션 추가" 로만 만든다(형 지시 2026-08-22). 아무 카드에나
   * 버튼을 두면 남의 섹션 제목 아래로 표가 딸려 들어가 무슨 표인지 적을 데가 없어진다.
   * 유일한 예외는 **블록이 하나도 없는 special 섹션** — 섹션 생성은 됐는데 표 삽입이
   * 실패하면 그런 고아 카드가 남고, 여기서 못 채우면 형에게 복구 수단이 없다.
   *
   * `blockCount` 는 **서버에 있는** 블록 수만 센다. 아직 저장 안 한 글칸 초안은 앱에서도
   * `report.blocks(in:)` 에 없으므로(별도 `needsTextDraft` UI 상태), 초안에 글을 적는
   * 동안 버튼이 남아 있는 것도 앱과 같은 동작이다.
   *
   * 🔴 `!blockCount` 가 아니라 `=== 0` 이다. `undefined`/`NaN` 을 빈 섹션으로 읽으면
   * 호출부가 값을 안 넘긴 실수가 "표 버튼이 아무 데나 뜬다" 로 조용히 새어 나온다.
   */
  function canAddTable(section, blockCount, locked) {
    return !locked && !!section && section.kind === 'special' && blockCount === 0;
  }

  var api = {DEFAULT_COLUMNS: DEFAULT_COLUMNS, normalize: normalize, grid: grid, read: read, empty: empty,
             canAddTable: canAddTable,
             setCell: setCell, setColumn: setColumn, addRow: addRow, addColumn: addColumn,
             canRemoveRow: canRemoveRow, canRemoveColumn: canRemoveColumn,
             removeRow: removeRow, removeColumn: removeColumn};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else window.DockDailyTable = api;
})();
