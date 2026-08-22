/* 입거 Daily Report 의 SVMS 반영 상태 표기 규칙 (형 지시 2026-08-22 "앱도 해당 기능 업데이트해줘").
 *
 * 아이폰 앱 `DockDailySVMSSync` 와 **같은 규칙**을 쓴다. 별 파일로 뺀 이유는
 * dock_daily_filter.js 와 같다: 규칙을 문자열 존재확인으로 때우지 말고 실제로 실행해서
 * 잠그기 위해서다(tests/dock_daily_svms.test.js).
 *
 * 🔴 상신은 맥 러너가 사내망에서 대신 저장하는 **비동기** 경로다. 화면이 이 상태를 띄우지
 *    않으면 형은 상신을 눌러놓고 결과(반영됨 / 본문만 / 실패 / 불명)를 영구히 알 수 없다. */
(function () {
  'use strict';

  var TITLES = {
    preview_only: 'SVMS 미반영',
    approved: 'SVMS 대기중',
    submitting: 'SVMS 전송중',
    synced: 'SVMS 반영됨',
    partial: 'SVMS 본문만 반영',
    unknown: 'SVMS 결과 불명',
    failed: 'SVMS 실패'
  };

  // 목록 한 줄에 붙일 짧은 꼬리. 🔴 `preview_only` 는 없다 -- 대부분의 보고서가 이 상태라
  // 꼬리를 달면 목록 전체가 같은 글자로 덮여 정작 SVMS 로 넘어간 일자를 눈으로 못 고른다.
  var SUFFIXES = {
    approved: 'SVMS 대기',
    submitting: 'SVMS 전송중',
    synced: 'SVMS 반영',
    partial: 'SVMS 본문만',
    unknown: 'SVMS 불명',
    failed: 'SVMS 실패'
  };

  // 배지 색. 앱의 Theme 토큰과 짝이 맞는 기존 클래스만 쓴다.
  var TONES = {
    preview_only: 'muted',
    approved: 'info',
    submitting: 'info',
    synced: 'ok',
    partial: 'warn',
    unknown: 'warn',
    failed: 'bad'
  };

  /* 서버가 새 상태를 추가해도 화면이 죽지 않게 미지의 값은 `unknown` 으로 접는다.
   * 🔴 안전한 쪽으로 떨어져야 한다 -- 모르는 값을 `preview_only` 로 읽으면 이미 SVMS 에
   *    들어갔을 수 있는 보고서에서 상신 버튼이 다시 열린다. */
  function normalize(raw) {
    var key = String(raw == null ? '' : raw).trim();
    if (!key) return 'preview_only';
    return Object.prototype.hasOwnProperty.call(TITLES, key) ? key : 'unknown';
  }

  function title(raw) { return TITLES[normalize(raw)]; }
  function tone(raw) { return TONES[normalize(raw)]; }
  function listSuffix(raw) { return SUFFIXES[normalize(raw)] || ''; }

  /* 🔴 재상신이 열리는 상태는 둘뿐이다. `SP_SET_DOCK_DR` 는 멱등이 아니라 같은 날짜
   *    재저장이 **새 seq 행**을 만든다(2026-08-22 카나리 실측). 서버도 409
   *    `manual_reconcile_required` 로 거절하므로 화면에서 먼저 막아 헛클릭을 없앤다. */
  function allowsPublish(raw) {
    var key = normalize(raw);
    return key === 'preview_only' || key === 'failed';
  }

  /* 사람이 SVMS 화면을 직접 봐야 하는 상태. */
  function needsManualCheck(raw) {
    var key = normalize(raw);
    return key === 'unknown' || key === 'partial';
  }

  /* 상태별 안내문. 🔴 `unknown`/`partial` 에 "다시 시도" 를 권하면 SVMS 에 중복 행이 생긴다. */
  function guidance(raw) {
    var key = normalize(raw);
    if (key === 'partial') return '본문은 SVMS에 저장됐고 첨부가 빠졌습니다. 첨부는 SVMS에서 직접 올리세요.';
    if (key === 'unknown') return 'SVMS 저장 여부가 확정되지 않았습니다. SVMS 화면에서 직접 확인하세요. 자동 재전송은 중복 저장이 됩니다.';
    return '';
  }

  var api = {
    normalize: normalize, title: title, tone: tone, listSuffix: listSuffix,
    allowsPublish: allowsPublish, needsManualCheck: needsManualCheck, guidance: guidance
  };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else window.DockDailySVMS = api;
})();
