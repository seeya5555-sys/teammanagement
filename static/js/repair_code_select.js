/* 수리신청서 Reason/Department 코드 선택 규칙.
 *
 * 형 요청(2026-08-23): 웹도 iOS 처럼 드롭다운으로. 화면 조작이 아니라 **규칙**을 여기 두는 이유는
 * 올마이트 지적("문자열 존재 검사뿐, 실제 동작 회귀를 못 잡는다") 때문이다. select 를 흉내낸 DOM
 * 스텁으로 테스트하면 스텁이 브라우저와 다르게 굴 때 **가짜 초록**이 된다. 그래서 "무엇을 그리고
 * 무엇을 고를지" 는 순수함수로 내리고(여기, node 로 실행 테스트), 화면은 그 결과를 그대로 그린다.
 *
 * 🔴 목록은 iOS `RepairRequestView.reasonOptions`/`departmentOptions` 와 **코드·이름·순서까지**
 *    같아야 한다. 한쪽만 늘리면 같은 신청서를 다른 쪽에서 열었을 때 그 코드가 목록 밖 값이 된다.
 *    (`tests/test_repair_code_dropdowns.py` 가 두 파일을 대조한다.)
 */
(function () {
  'use strict';

  var REASONS = [
    ['P', 'PMS'], ['S', 'Survey'], ['A', 'A/S Repair'], ['R', 'Recondition'], ['T', 'Trouble'],
    ['C', 'Accident'], ['X', 'Etc'], ['V', 'Apply Convention'], ['M', 'Maintain Mistake'],
    ['O', 'Operation Mistake'], ['U', 'Unsuitable Spare Part'], ['W', 'Weather'],
    ['N', 'Worn Out'], ['D', 'PMS Overdue']
  ];
  var DEPTS = [['D', 'Deck'], ['E', 'Engine']];
  var DEFAULTS = { reason: 'P', dept: 'E' };

  function norm(value) {
    return String(value === null || value === undefined ? '' : value).trim().toUpperCase();
  }

  /** 그릴 option 목록과 고를 코드를 함께 돌려준다.
   *
   * 🔴 목록 밖 값(옛 자유입력 초안·SVMS 신설 코드)을 select 에 그냥 대입하면 브라우저는 **조용히
   *    빈 값**으로 만들고, 그대로 저장하면 형이 적었던 코드가 사라진다. 그래서 목록 밖 값일 때만
   *    `legacy` option 을 하나 붙여 원래 값을 지킨다.
   * 🔴 값이 비었으면 **기본값으로 내려앉는다**(올마이트 지적). 편집 화면은 칸을 재사용하므로,
   *    비었을 때 "지금 선택" 을 유지하면 **직전에 열어 본 신청서의 코드가 다음 건에 조용히 복사**
   *    된다. 목록·선택을 매번 통째로 다시 만드는 것도 같은 이유다 — 지난 legacy option 이 남아
   *    있을 수 있는 경로를 아예 없앤다.
   */
  function build(kind, value) {
    var list = kind === 'reason' ? REASONS : DEPTS;
    var def = DEFAULTS[kind === 'reason' ? 'reason' : 'dept'];
    var code = norm(value) || def;
    var options = list.map(function (pair) {
      return { code: pair[0], name: pair[1], legacy: false };
    });
    var known = options.some(function (o) { return o.code === code; });
    if (!known) options.push({ code: code, name: code + ' (목록 외 기존 값)', legacy: true });
    return { options: options, selected: code };
  }

  /** option 라벨. 목록 밖 값은 이름 자체가 안내문이라 코드를 덧붙이지 않는다. */
  function label(option) {
    return option.legacy ? option.name : option.name + ' (' + option.code + ')';
  }

  var api = { REASONS: REASONS, DEPTS: DEPTS, DEFAULTS: DEFAULTS, build: build, label: label };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else window.RepairCodeSelect = api;
})();
