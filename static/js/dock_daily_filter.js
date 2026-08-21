/* 입거 Daily Report 일자 목록 필터 (형 지시 2026-08-21 ②: 드롭다운 + 필터검색).
 *
 * 아이폰 앱의 DockDailyReportFilter 와 같은 규칙을 쓴다. 별 파일로 뺀 이유는
 * dock_daily_numbering.js 와 같다: 규칙을 문자열 존재확인으로 때우지 말고 실제로
 * 실행해서 잠그기 위해서다(tests/dock_daily_filter.test.js). */
(function () {
  'use strict';
  var digits = function (v) { return String(v == null ? '' : v).replace(/\D+/g, ''); };

  // 날짜 원문 부분일치 OR 숫자만 남긴 부분일치.
  // 🔴 규칙을 넓히면 엉뚱한 날짜가 걸린다. `9/1` 은 숫자열이 `91` 이라 `2026-09-01`
  // (= 20260901) 에 안 걸리는 게 의도다. `0901`·`09-01`·`2026-09` 는 걸린다.
  function matches(report, query) {
    var q = String(query == null ? '' : query).trim();
    if (!q) return true;
    var date = String((report && report.report_date) || '');
    if (date.indexOf(q) >= 0) return true;
    var d = digits(q);
    return !!d && digits(date).indexOf(d) >= 0;
  }

  // 상태와 날짜는 AND 다.
  // 🔴 여기서 다시 정렬하면 서버가 준 날짜 역순(=앱 순서)과 갈린다.
  function apply(reports, query, status) {
    return (reports || []).filter(function (r) {
      return (!status || r.status === status) && matches(r, query);
    });
  }

  var api = { matches: matches, apply: apply, digits: digits };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else window.DockDailyReportFilter = api;
})();
