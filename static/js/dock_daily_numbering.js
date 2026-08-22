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

  /* 백스페이스가 번호 접두어(`2) `)를 갉아먹지 않게 가로챈다.
   *
   * 🔴 이 가로채기가 없으면 자동으로 붙은 번호를 지울 수가 없다. 접두어 안에서 한 글자를
   * 지우면 남은 조각(`2`)이 번호가 아니라 **작업내용**으로 보여서 renumber 가 번호를 다시
   * 붙인다(`2) 2`). 2026-08-22 형 제보(iOS)에서 3회 주기 무한반복으로 확인됐고, 웹은
   * blur 시점에 같은 결과가 된다.
   *
   * 규칙: 접두어 안(또는 줄 맨 앞)에서의 백스페이스는 그 줄을 앞줄과 합친다(= 엔터 취소).
   * 첫 줄이면 합칠 앞줄이 없으니 아무 일도 하지 않는다(전체선택 삭제는 여기를 안 지난다).
   * null = 기본 삭제에 맡김. */
  function deleteBackward(value, caret) {
    var safeCaret = Math.max(0, Math.min(Number(caret) || 0, value.length));
    if (safeCaret <= 0) return null;
    var head = value.slice(0, safeCaret);
    var caretCol = safeCaret - (head.lastIndexOf('\n') + 1);
    var lines = value.split('\n');
    var caretLine = head.split('\n').length - 1;
    if (caretLine >= lines.length) return null;
    var prefix = ITEM_NO.exec(lines[caretLine]);
    if (!prefix || caretCol > prefix[0].length) return null;   // 접두어 밖 → 평범한 글자 삭제
    if (caretLine === 0) return { value: value, caret: safeCaret };
    var previous = itemBody(lines[caretLine - 1]);
    // 합친 줄에 접두어를 하나 달아서 넘긴다. renumber 는 줄마다 접두어 하나를 걷어내므로,
    // 맨몸으로 주면 본문이 `3) x` 처럼 생긴 경우 그 `3) ` 까지 먹혀 글자가 사라진다.
    lines.splice(caretLine - 1, 2, '1) ' + previous + itemBody(lines[caretLine]));
    var offset = 0;
    for (var i = 0; i < caretLine - 1; i++) offset += lines[i].length + 1;
    return renumber(lines.join('\n'), offset + 3 + previous.length, true);
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
              breakLine: breakLine, deleteBackward: deleteBackward,
              needsNumbering: needsNumbering };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else window.DockDailyNumbering = api;
})();
