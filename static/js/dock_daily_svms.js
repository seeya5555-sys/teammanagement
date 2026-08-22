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

  /* 미리보기의 `blockers` 는 서버가 판정한 **상신 불가 사유**다.
   * 🔴 사유를 화면에 안 뿌리면 "상신 불가" 한 줄만 남아 형이 고칠 방법을 알 수 없다
   *    (2026-08-22 형 캡쳐: DK_CD 가 비어 있었는데 화면엔 사유가 없었다). */
  function blockerList(preview) {
    var raw = preview && preview.blockers;
    if (!Array.isArray(raw)) return [];
    return raw.map(function (b) { return String(b == null ? '' : b).trim(); })
              .filter(function (b) { return !!b; });
  }

  /* `DK_CD 미설정` 은 사람이 화면에서 고칠 수 있는 유일한 blocker 다(Dock 연결).
   * 나머지(byte 한도 미설정/초과)는 설정이나 본문 편집 문제라 여기서 못 푼다. */
  function needsDockLink(preview) {
    return blockerList(preview).some(function (b) { return b.indexOf('DK_CD') === 0; });
  }

  /* 후보 한 줄 표기. `open=false`(출거 완료/상태 C)는 눈에 보이게 구분한다 --
   * 지난 입거에 오늘 daily report 를 쓰는 게 제일 흔한 사고다. */
  function candidateLabel(cand) {
    if (!cand) return '';
    var bits = [String(cand.dk_cd || '')];
    if (cand.subj) bits.push(String(cand.subj));
    var state = [];
    if (cand.status) state.push('STATUS ' + cand.status);
    if (cand.dk_out_date) state.push('출거 ' + cand.dk_out_date);
    if (!cand.open) state.push('종료된 입거');
    if (state.length) bits.push('(' + state.join(' · ') + ')');
    return bits.join(' · ');
  }

  var api = {
    normalize: normalize, title: title, tone: tone, listSuffix: listSuffix,
    allowsPublish: allowsPublish, needsManualCheck: needsManualCheck, guidance: guidance,
    blockerList: blockerList, needsDockLink: needsDockLink, candidateLabel: candidateLabel
  };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else window.DockDailySVMS = api;
})();
