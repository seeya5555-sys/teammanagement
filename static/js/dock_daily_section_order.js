/* 입거 Daily Report 섹션 순서 규칙 (형 지시 2026-08-22).
 *
 * 앱(`DockDailySectionEditing.renumbered` / `movingVisible`)과 **같은 규칙**이어야 한다.
 * 순서는 프로젝트 값(`dock_daily_section_def.sort_order`)이라 웹에서 바꾼 순서가 앱·메일·
 * SVMS 에 그대로 나가고, 두 미러가 다르게 매기면 어느 쪽이 옳은지 화면만 봐서는 모른다.
 *
 * 브라우저와 node 양쪽에서 돌도록 순수 함수로만 둔다(테스트: tests/dock_daily_section_order.test.js).
 */
(function () {
  'use strict';

  function sorted(sections) {
    // 서버는 `ORDER BY sort_order, id` 로 주지만, 화면 상태를 손으로 만든 자리도 있으므로
    // 여기서 한 번 더 정렬해 입력 순서에 기대지 않는다.
    return (sections || []).slice().sort(function (a, b) {
      return ((a && a.sort_order) || 0) - ((b && b.sort_order) || 0);
    });
  }

  // 🔴 번호는 **전체 목록**에 매긴다. 보이는 섹션만 매기면 꺼 둔 섹션이 옛 번호를 그대로
  // 들고 있다가, 다시 켜는 순간 엉뚱한 자리에 끼어든다.
  function renumbered(ordered) {
    return (ordered || []).map(function (section, index) {
      var next = {};
      for (var k in section) if (Object.prototype.hasOwnProperty.call(section, k)) next[k] = section[k];
      next.sort_order = index + 1;
      return next;
    });
  }

  // 🔴 이웃은 **보이는 섹션**에서만 고른다. 바로 옆이 꺼진 섹션이면 그 자리와 맞바꿔봐야
  // 화면에서는 아무것도 안 움직인다 -- 눌러도 반응 없는 버튼이 된다.
  function movingVisible(sections, key, offset) {
    var ordered = sorted(sections);
    var from = -1;
    for (var i = 0; i < ordered.length; i++) {
      if (ordered[i] && ordered[i].section_key === key) { from = i; break; }
    }
    if (from < 0) return null;
    var visible = [];
    for (var j = 0; j < ordered.length; j++) if (ordered[j] && ordered[j].enabled) visible.push(j);
    var slot = visible.indexOf(from);
    if (slot < 0) return null;                       // 꺼져 있는 섹션은 카드가 없다
    var target = slot + Number(offset);
    if (!(target >= 0) || target >= visible.length) return null;   // 끝단
    var next = ordered.slice();
    var swap = next[from];
    next[from] = next[visible[target]];
    next[visible[target]] = swap;
    return renumbered(next);
  }

  function canMove(sections, key, offset) {
    return !!movingVisible(sections, key, offset);
  }

  // 🔴 고정 섹션은 `section_key` + `sort_order` 만 보낸다. `label`·`enabled` 가 함께 오면
  // 서버가 400 으로 끊는다(이름이 바뀐 줄 알고 화면에 남기지 않도록, 조용히 무시하지
  // 않기로 한 계약). 앱 `reorderSections` 와 같은 payload 다.
  function payload(ordered) {
    return renumbered(ordered).map(function (s) {
      if (s.kind === 'special') {
        return {section_key: s.section_key, label: s.label,
                sort_order: s.sort_order, enabled: !!s.enabled};
      }
      return {section_key: s.section_key, sort_order: s.sort_order};
    });
  }

  var api = {sorted: sorted, renumbered: renumbered, movingVisible: movingVisible,
             canMove: canMove, payload: payload};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else window.DockDailySectionOrder = api;
})();
