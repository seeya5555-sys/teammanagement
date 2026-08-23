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

  /* ── 엑셀 표 붙여넣기 (형 지시 2026-08-23 "표 첫칸에 엑셀표 복사 붙여넣기하면 그대로") ──
   *
   * 앱 `DockDailyTablePaste` 와 **같은 규칙**이다. 서버는 이 경로에 관여하지 않는다 --
   * 붙여넣기는 화면 조작이고, 서버에는 이미 만들어진 grid 만 간다.
   *
   * 🔴 한 표의 상한을 여기서 잡는다. 서버 `_table_grid` 에는 표 크기 상한이 **없어서**
   *    시트를 통째로 붙이면 칸 수천 개가 카드에 들어가고, 그 표가 그대로 메일 한 통과
   *    SVMS `RMK`(4000 byte) 로 나가려다 `over_limit` 으로 막힌다. 막히는 건 옳지만
   *    그때는 이미 형이 적어둔 표가 화면에서 밀려 있다.
   * 🔴 잘라낸 건 **반드시 말한다**(`pasteNote`). 조용히 자르면 엑셀에 있던 행이 어디로
   *    갔는지 알 방법이 없다. */
  var PASTE_MAX_ROWS = 200;
  var PASTE_MAX_COLUMNS = 30;

  /** 클립보드 글을 표로 읽는다(엑셀·구글시트·넘버스 공통 TSV).
   *
   * 🔴 `null` 은 "표가 아니다" 는 뜻이고, 호출부는 그때 **평소 붙여넣기를 막지 않는다**.
   *    탭도 줄바꿈도 없는 글까지 가로채면 셀 안 글자 일부를 골라 붙이는 보통 붙여넣기가
   *    통째로 망가진다(붙인 값이 칸 전체를 덮어쓴다).
   * 🔴 엑셀은 탭·줄바꿈·인용부호가 든 칸을 `"` 로 감싸고 안쪽 `"` 를 두 번 쓴다. 그걸
   *    안 풀면 줄바꿈 든 칸 하나가 엉뚱한 행 두 개로 갈라진다.
   */
  /* 한 번 훑기. `quotes=false` 면 인용을 글자로 보고 탭·줄바꿈만 본다. `open` 은 인용이
   * 닫히지 않고 끝났다는 표시다. */
  function scanRows(text, quotes) {
    var rows = [], row = [], cell = '', quoted = false, i = 0;
    while (i < text.length) {
      var ch = text.charAt(i);
      if (quoted) {
        if (ch === '"') {
          if (text.charAt(i + 1) === '"') { cell += '"'; i += 2; continue; }
          quoted = false; i += 1; continue;
        }
        cell += ch; i += 1; continue;
      }
      // 인용은 칸의 **처음에만** 열린다(엑셀도 칸 전체만 감싼다). 값 중간의 `"` 는 글자다.
      if (quotes && ch === '"' && cell === '') { quoted = true; i += 1; continue; }
      if (ch === '\t') { row.push(cell); cell = ''; i += 1; continue; }
      if (ch === '\n') { row.push(cell); rows.push(row); row = []; cell = ''; i += 1; continue; }
      cell += ch; i += 1;
    }
    row.push(cell); rows.push(row);
    return {rows: rows, open: quoted};
  }

  function parseClipboard(raw) {
    if (typeof raw !== 'string' || !raw) return null;
    // 🔴 줄 구분을 LF 하나로 맞춘다. 앱은 Swift 에서 `"\r\n"` 이 **글자 하나**(확장 자소군)라
    //    이 정규화가 없으면 CRLF 를 아예 못 읽는다. 웹만 CR 를 살려두면 인용된 칸 안의
    //    줄바꿈이 웹은 CRLF, 앱은 LF 로 저장돼 **같은 붙여넣기가 기기마다 다른 표**가 된다
    //    (올마이트 지적 2026-08-23).
    var text = raw.replace(/\r\n/g, '\n').replace(/\r/g, '\n');
    if (text.indexOf('\t') < 0 && text.indexOf('\n') < 0) return null;
    var scan = scanRows(text, true);
    // 🔴 인용이 끝까지 안 닫혔으면 인용 해석이 틀린 것이다(엑셀이 아닌 곳에서 온 TSV,
    //    예: `"인용 으로 시작하는 메모`). 그대로 두면 남은 줄 전부가 한 칸으로 먹힌다 --
    //    인용 없이 다시 읽어 표를 구한다(값은 하나도 안 버린다).
    var rows = scan.open ? scanRows(text, false).rows : scan.rows;
    // 🔴 엑셀은 끝에 줄바꿈을 하나 붙인다. 그걸 살리면 붙여넣기마다 빈 행이 하나 늘어난다.
    //    중간의 빈 행은 형이 비워 둔 행이므로 **뒤에서만** 떨어낸다.
    while (rows.length && rows[rows.length - 1].every(function (v) { return v === ''; })) rows.pop();
    if (!rows.length) return null;
    // 🔴 여기서는 **상한을 걸지 않는다.** 잘라내기는 붙인 자리(anchor)를 알아야 정확해서
    //    `pasteInto` 한 곳에서만 한다 -- 두 곳에서 세면 같은 초과분을 두 번 세거나
    //    (150행 표의 끝에 붙일 때처럼) 결과가 상한을 넘는데 아무 말도 안 하게 된다.
    var width = 0;
    rows.forEach(function (r) { if (r.length > width) width = r.length; });
    return {rows: rows, width: width};
  }

  /** 붙여넣기를 표에 얹는다. 표가 아니면 `null`(= 호출부는 평소 붙여넣기에 맡긴다).
   *
   * `anchor.row` 가 `null`/`undefined` 면 **열 이름 칸**에 붙인 것이다 -- 그때만 첫 줄이
   * 열 이름이 되고 남은 줄이 본문 첫 행부터 들어간다. 형이 말한 "표 첫칸" 이 이 자리다.
   *
   * 🔴 **넓히기만 하고 줄이지 않는다.** 붙인 사각형 밖의 칸은 그대로 남는다 -- 표를
   *    통째로 갈아치우면 이미 적어둔 옆 열·아래 행이 붙여넣기 한 번에 사라진다.
   * 🔴 사각형 맞추기는 `normalize` 가 한다(서버 `_table_grid`·앱과 같은 규칙). 그래서
   *    붙인 표가 기존 표보다 넓으면 열 이름이 빈 열이 늘어나고, 값은 하나도 안 잘린다.
   */
  function pasteInto(grid0, anchor, raw) {
    var parsed = parseClipboard(raw);
    if (!parsed) return null;
    var g = normalize(grid0);
    var a = (anchor && typeof anchor === 'object') ? anchor : {};
    var col = Math.max(0, Number(a.col) || 0);
    var toHeader = (a.row === null || a.row === undefined);
    var lines = parsed.rows.slice();
    var start = toHeader ? 0 : Math.max(0, Number(a.row) || 0);
    // 🔴 상한은 **붙인 원본이 아니라 결과 표**를 기준으로 잡는다(올마이트 지적 2026-08-23).
    //    150행짜리 표의 마지막 행에 200행을 붙이면 원본만 재던 옛 계산은 상한을 통과시키고
    //    결과가 350행이 됐다. 대신 **이미 있던 행·열은 절대 지우지 않는다** -- 붙여넣기가
    //    형이 적어둔 줄을 잘라내면 그게 더 큰 손실이다.
    var roomRows = Math.max(0, PASTE_MAX_ROWS - start);
    var roomCols = Math.max(0, PASTE_MAX_COLUMNS - col);
    var head = toHeader ? lines.shift() : null;
    // 넘친 열은 붙인 줄 중 **가장 넓은 줄** 기준으로 센다(줄마다 세면 같은 초과를 여러 번 센다).
    var wide = head ? head.length : 0;
    lines.forEach(function (line) { if (line.length > wide) wide = line.length; });
    var droppedColumns = Math.max(0, wide - roomCols);
    var droppedRows = Math.max(0, lines.length - roomRows);
    if (droppedRows) lines = lines.slice(0, roomRows);
    if (droppedColumns) {
      if (head) head = head.slice(0, roomCols);
      lines = lines.map(function (line) { return line.slice(0, roomCols); });
    }
    var columns = g.columns.slice();
    if (head) for (var c = 0; c < head.length; c++) columns[col + c] = head[c];
    var body = g.rows.map(function (r) { return r.slice(); });
    lines.forEach(function (line, n) {
      var ri = start + n;
      while (body.length <= ri) body.push([]);
      var target = body[ri].slice();
      for (var k = 0; k < line.length; k++) target[col + k] = line[k];
      body[ri] = target;
    });
    var out = normalize({columns: columns, rows: body});
    return {grid: out, usedHeader: toHeader, addedRows: lines.length,
            droppedRows: droppedRows, droppedColumns: droppedColumns};
  }

  /** 붙여넣기 결과를 형에게 말할 문장. 앱과 같은 문장이어야 한다. */
  function pasteNote(result) {
    if (!result) return '';
    var g = result.grid;
    // 🔴 형이 세는 방식으로 쓴다(올마이트 지적 2026-08-23). 형은 5줄짜리 붙여넣기를
    //    "5x4 표" 라고 부르는데 그 첫 줄은 열 이름이다 -- 그냥 `4행` 이라고만 쓰면 한 줄이
    //    사라진 것으로 읽힌다. 열 이름을 먹었을 때는 그 사실을 **숫자 안에** 넣는다.
    var out = ['붙여넣기로 ' + (result.usedHeader ? '열 이름 + ' : '')
               + g.rows.length + '행 × ' + g.columns.length + '열이 되었습니다'];
    if (result.droppedRows) {
      out.push('⚠ ' + result.droppedRows + '행은 상한을 넘어 빠졌습니다(표 하나에 '
               + PASTE_MAX_ROWS + '행까지)');
    }
    if (result.droppedColumns) {
      out.push('⚠ ' + result.droppedColumns + '열은 상한을 넘어 빠졌습니다(표 하나에 '
               + PASTE_MAX_COLUMNS + '열까지)');
    }
    out.push('저장을 눌러야 반영됩니다');
    return out.join(' · ');
  }

  var api = {DEFAULT_COLUMNS: DEFAULT_COLUMNS, normalize: normalize, grid: grid, read: read, empty: empty,
             canAddTable: canAddTable,
             PASTE_MAX_ROWS: PASTE_MAX_ROWS, PASTE_MAX_COLUMNS: PASTE_MAX_COLUMNS,
             parseClipboard: parseClipboard, pasteInto: pasteInto, pasteNote: pasteNote,
             setCell: setCell, setColumn: setColumn, addRow: addRow, addColumn: addColumn,
             canRemoveRow: canRemoveRow, canRemoveColumn: canRemoveColumn,
             removeRow: removeRow, removeColumn: removeColumn};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else window.DockDailyTable = api;
})();
