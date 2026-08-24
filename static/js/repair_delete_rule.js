/* 수리신청서 신청 목록 — [삭제] 버튼 노출·확인 규칙.
 *
 * 형 지시(2026-08-24): "신청목록 리스트 삭제가 가능하게 버튼만들어줘". 초안 삭제는 이미 있었고
 * 실제로 막혀 있던 것은 **SVMS 저장본**(REP_CD 있는 건)이다. 그 삭제는 되돌릴 수 없고
 * SVMS 문서는 남으므로, 무엇을 묻고 무엇을 숨길지를 여기 한 곳에 둔다.
 *
 * 🔴 최종 판정은 **서버**(`routes_repair_request.repair_request_delete`)다. 이 규칙은 화면이
 *    보여줄 것만 정한다. 특히 견적·발주 데이터가 붙은 건의 차단은 여기서 흉내내지 않는다 —
 *    목록 응답에 그 필드가 다 오지 않으므로 흉내내면 두 판정이 갈리고, 갈리는 순간
 *    "버튼은 있는데 눌러도 안 되는" 쪽이 아니라 **"막아야 하는데 열린"** 쪽으로 틀릴 수 있다.
 * 🔴 iOS `RepairRequest.deleteRule` 과 **분기·문구가 같아야 한다**
 *    (`tests/test_repair_delete_button.py` 가 두 소스를 대조한다).
 */
(function () {
  'use strict';

  // 러너가 이미 물었다. 지우면 SVMS 생성 여부를 영구히 모른다 → 버튼을 숨긴다.
  var INFLIGHT = ['saving'];
  // REP_CD 없이 지워도 되는 상태 whitelist. 🔴 낯선 상태·REP_CD 없는 `saved` 는 버튼을 숨긴다
  //    (서버도 409). "아마 초안" 으로 봐 주면 SVMS 에 뭐가 있는지 모르는 행이 지워진다.
  var DRAFTISH = ['pending', 'failed', 'approved'];
  var CONFIRM_TOKEN = 'TRMT에서만삭제';   // 서버 `_DELETE_CONFIRM` 과 같은 문자열

  /** 이 행에 [삭제] 를 보여줄지, 누르면 무엇을 물을지.
   *
   * 돌려주는 값:
   *   visible   — 버튼을 그릴지
   *   token     — 서버에 보낼 확인 문구(없으면 null)
   *   prompts   — 순서대로 물어볼 확인 문구들. 하나라도 취소하면 삭제하지 않는다.
   */
  function deleteRule(row) {
    var status = String((row && row.status) || '');
    var repCd = (row && row.rep_cd) || null;
    if (INFLIGHT.indexOf(status) >= 0) {
      return { visible: false, token: null, prompts: [] };
    }
    if (repCd) {
      // 🔴 2단 확인. 1단은 "지워도 SVMS 에 남는다"(형이 SVMS 를 정리했다고 오해하지 않게),
      //    2단은 되돌릴 수 없다는 사실. 한 문장에 합치면 둘 중 하나는 안 읽힌다.
      return {
        visible: true,
        token: CONFIRM_TOKEN,
        prompts: [
          'SVMS에 이미 저장된 신청서입니다(REP_CD ' + repCd + ').\n\n'
            + 'TRMT 목록에서만 사라지고 SVMS 문서는 그대로 남습니다. SVMS 정리는 별도로 해야 합니다.\n\n'
            + '계속할까요?',
          '되돌릴 수 없습니다. REP_CD ' + repCd + ' 건을 TRMT에서 삭제할까요?'
        ]
      };
    }
    if (DRAFTISH.indexOf(status) < 0) {
      return { visible: false, token: null, prompts: [] };
    }
    if (status === 'approved') {
      // 저장 큐에 올라간 초안. 아직 SVMS 전이지만 형이 [저장] 을 누른 건이라 그 사실을 말해 준다.
      return {
        visible: true,
        token: null,
        prompts: ['아직 SVMS 저장을 기다리는 중입니다.\n\n'
          + '러너가 이 건을 물기 전이면 저장이 취소됩니다. 이미 물었으면 삭제가 거부됩니다.\n\n'
          + '삭제할까요?']
      };
    }
    return { visible: true, token: null, prompts: ['이 초안을 삭제할까요?'] };
  }

  var api = { INFLIGHT: INFLIGHT, DRAFTISH: DRAFTISH,
              CONFIRM_TOKEN: CONFIRM_TOKEN, deleteRule: deleteRule };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else window.RepairDeleteRule = api;
})();
