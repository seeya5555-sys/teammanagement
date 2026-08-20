/* 입거 Daily Report 작업항목 자동번호 — DOM 의존 없는 순수 로직.
 *
 * 카드(textarea)가 자기 번호를 들고 있어야 형이 보는 화면과 Outlook 메일 본문이
 * 같아진다. 서버는 저장된 번호를 지운 뒤 다시 번호를 붙이므로 중복되지 않는다.
 * 브라우저와 node 테스트가 같은 코드를 쓰도록 별도 파일로 분리했다.
 */
(function () {
  'use strict';
  var ITEM_NO = /^\s*\d+\s*\)\s*/;
  var LEADING_WS = /^\s+/;

  /* 줄에서 번호와 들여쓰기를 걷어낸 순수 작업내용. */
  function itemBody(line) { return line.replace(ITEM_NO, '').replace(LEADING_WS, ''); }

  /* value 안의 모든 작업항목 줄을 1) 2) 3) 으로 다시 매긴다.
   * 빈 줄은 목록에서 통째로 빠지므로 고아 번호나 빈 항목이 남지 않는다.
   * keepCaretLine=true 이면 caret 이 있는 빈 줄만 예외로 번호를 받는다
   * (엔터 직후 새 줄이 곧바로 `2) ` 로 보여야 하기 때문). */
  function renumber(value, caret, keepCaretLine) {
    var safeCaret = Math.max(0, Math.min(Number(caret) || 0, value.length));
    var head = value.slice(0, safeCaret);
    var caretLine = head.split('\n').length - 1;
    var caretCol = safeCaret - (head.lastIndexOf('\n') + 1);
    var lines = value.split('\n');
    var out = [];
    var no = 0, offset = 0, newCaret = safeCaret;
    for (var i = 0; i < lines.length; i++) {
      var raw = lines[i];
      var body = itemBody(raw);
      var keep = body.trim() !== '' || (keepCaretLine === true && i === caretLine);
      var line = keep ? (no + 1) + ') ' + body : null;
      if (i === caretLine) {
        var bodyCol = Math.max(0, caretCol - (raw.length - body.length));
        newCaret = keep ? offset + Math.min(line.length, (line.length - body.length) + bodyCol) : offset;
      }
      if (keep) { no += 1; out.push(line); offset += line.length + 1; }
    }
    var next = out.join('\n');
    return { value: next, caret: Math.max(0, Math.min(newCaret, next.length)) };
  }

  /* 엔터: 선택영역을 지우고 줄바꿈을 넣은 뒤 새 줄에 다음 번호를 붙인다. */
  function breakLine(value, start, end) {
    var from = Math.max(0, Math.min(Number(start) || 0, value.length));
    var to = Math.max(from, Math.min(Number(end) || from, value.length));
    var next = value.slice(0, from) + '\n' + value.slice(to);
    return renumber(next, from + 1, true);
  }

  /* 이미 번호가 붙은 첫 줄은 건드리지 않는다. 번호 없는 옛 카드만 정규화 대상. */
  function needsNumbering(value) {
    return value.trim() !== '' && !ITEM_NO.test(value.split('\n')[0] || '');
  }

  var api = { ITEM_NO: ITEM_NO, itemBody: itemBody, renumber: renumber,
              breakLine: breakLine, needsNumbering: needsNumbering };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else window.DockDailyNumbering = api;
})();
