/* SVMS 반영 상태 표기 규칙을 실제로 실행해서 잠근다.
 * 실행: node --test tests/dock_daily_svms.test.js  (tests/test_dock_daily.py 가 같이 돌린다)
 *
 * 규칙 정본은 아이폰 앱 `DockDailySVMSSync` 이고 이 파일은 웹 미러를 그 규칙에 묶는다. */
const test = require('node:test');
const assert = require('node:assert');
const S = require('../static/js/dock_daily_svms.js');

test('빈 값은 preview_only, 모르는 값은 unknown 으로 접힌다', () => {
  assert.equal(S.normalize(null), 'preview_only');
  assert.equal(S.normalize(''), 'preview_only');
  assert.equal(S.normalize('   '), 'preview_only');
  // 🔴 안전한 쪽으로 떨어져야 한다 — 모르는 값을 preview_only 로 읽으면 이미 SVMS 에
  //    들어갔을 수 있는 보고서에서 상신 버튼이 다시 열린다.
  assert.equal(S.normalize('something_new'), 'unknown');
  assert.equal(S.normalize('synced'), 'synced');
});

test('상신이 열리는 상태는 preview_only 와 failed 뿐이다', () => {
  // SP_SET_DOCK_DR 는 멱등이 아니다 — 같은 날짜 재저장이 새 seq 행을 만든다(2026-08-22 실측).
  assert.ok(S.allowsPublish(null));
  assert.ok(S.allowsPublish('failed'));
  for (const state of ['approved', 'submitting', 'synced', 'partial', 'unknown', 'something_new']) {
    assert.equal(S.allowsPublish(state), false, `${state} 에서 상신이 열리면 중복 저장이 된다`);
  }
});

test('unknown·partial 만 사람이 직접 확인해야 한다', () => {
  assert.ok(S.needsManualCheck('unknown'));
  assert.ok(S.needsManualCheck('partial'));
  assert.equal(S.needsManualCheck('failed'), false);
  assert.equal(S.needsManualCheck('synced'), false);
});

test('안내문은 재시도를 권하지 않는다', () => {
  // 재시도를 권하면 SVMS 에 중복 행이 생긴다.
  assert.match(S.guidance('unknown'), /직접 확인/);
  assert.match(S.guidance('unknown'), /중복/);
  assert.match(S.guidance('partial'), /첨부/);
  assert.equal(S.guidance('synced'), '');
  assert.equal(S.guidance(null), '');
});

test('preview_only 는 목록 꼬리가 없다', () => {
  // 대부분의 보고서가 이 상태다. 꼬리를 달면 목록 전체가 같은 글자로 덮여
  // 정작 SVMS 로 넘어간 일자를 눈으로 못 고른다.
  assert.equal(S.listSuffix(null), '');
  assert.equal(S.listSuffix('preview_only'), '');
  assert.equal(S.listSuffix('synced'), 'SVMS 반영');
  assert.equal(S.listSuffix('partial'), 'SVMS 본문만');
  assert.equal(S.listSuffix('something_new'), 'SVMS 불명');
});

test('모든 상태가 제목과 색을 가진다', () => {
  for (const state of ['preview_only', 'approved', 'submitting', 'synced', 'partial', 'unknown', 'failed']) {
    assert.ok(S.title(state), `${state} 제목 없음`);
    assert.ok(S.tone(state), `${state} 색 없음`);
  }
  assert.equal(S.tone('synced'), 'ok');
  assert.equal(S.tone('failed'), 'bad');
  assert.equal(S.tone('partial'), 'warn');
});

test('blockers 는 배열이 아니어도 화면을 죽이지 않는다', () => {
  // 서버가 이 키를 안 주는 갈래(이메일 미리보기)도 같은 함수를 지나간다.
  assert.deepEqual(S.blockerList(null), []);
  assert.deepEqual(S.blockerList({}), []);
  assert.deepEqual(S.blockerList({ blockers: 'DK_CD 미설정' }), []);
  assert.deepEqual(S.blockerList({ blockers: ['  DK_CD 미설정 ', '', null, 'byte 한도 초과: RMK'] }),
                   ['DK_CD 미설정', 'byte 한도 초과: RMK']);
});

test('Dock 연결 UI 는 DK_CD blocker 에서만 뜬다', () => {
  // 🔴 byte 한도 초과는 본문 편집 문제라 Dock 연결로 못 푼다. 여기서 UI 를 띄우면
  //    형이 Dock 을 바꿔가며 고쳐지지 않는 이유를 찾게 된다.
  assert.ok(S.needsDockLink({ blockers: ['DK_CD 미설정'] }));
  assert.equal(S.needsDockLink({ blockers: ['byte 한도 초과: RMK'] }), false);
  assert.equal(S.needsDockLink({ blockers: ['RMK byte 한도 계약 미설정'] }), false);
  assert.equal(S.needsDockLink({}), false);
});

test('후보 표기는 종료된 입거를 눈에 보이게 구분한다', () => {
  // 지난 입거에 오늘 daily report 를 쓰는 게 제일 흔한 사고다.
  assert.equal(S.candidateLabel(null), '');
  assert.equal(S.candidateLabel({ dk_cd: 'ATGRMD2607130001', subj: 'DD 2026', status: 'I', open: true }),
               'ATGRMD2607130001 · DD 2026 · (STATUS I)');
  const closed = S.candidateLabel({ dk_cd: 'ATGR22062701', status: 'C', dk_out_date: '20211105', open: false });
  assert.match(closed, /종료된 입거/);
  assert.match(closed, /출거 20211105/);
  assert.equal(S.candidateLabel({ dk_cd: 'ATGRMD2607130001', open: true }), 'ATGRMD2607130001');
});

test('상신 차단 사유는 계약 -> 확정 -> 반영상태 순서다', () => {
  // 🔴 계약 위반이 먼저다 — blockers 패널이 구체적 사유(DK_CD 미설정 등)를 이미
  //    보여주므로 "확정하세요" 로 덮어버리면 형이 엉뚱한 데를 고친다.
  assert.equal(S.publishBlockReason({ publishable: false, status: 'final', sync: 'preview_only' }), 'contract');
  assert.equal(S.publishBlockReason({ publishable: false, status: 'editing', sync: 'synced' }), 'contract');
  assert.equal(S.publishBlockReason({ publishable: true, status: 'editing', sync: 'preview_only' }), 'draft');
  assert.equal(S.publishBlockReason({ publishable: true, status: 'final', sync: 'synced' }), 'state');
  assert.equal(S.publishBlockReason({ publishable: true, status: 'final', sync: 'preview_only' }), '');
  // 실패는 다시 상신할 수 있다.
  assert.equal(S.publishBlockReason({ publishable: true, status: 'final', sync: 'failed' }), '');
  // 모르는 상태는 unknown 으로 접혀 막힌다.
  assert.equal(S.publishBlockReason({ publishable: true, status: 'final', sync: 'something_new' }), 'state');
  // 빈 sync = preview_only.
  assert.equal(S.publishBlockReason({ publishable: true, status: 'final', sync: null }), '');
  // 인자 없이 불러도 죽지 않는다.
  assert.equal(S.publishBlockReason(), 'contract');
});

test('상신 차단 사유는 반드시 화면에 적을 문구를 가진다', () => {
  // 🔴 버튼만 비활성이면 형은 왜 못 누르는지 알 수 없다(2026-08-22 형 질문:
  //    "상신 가능이라고 표시는 되는데 푸시 버튼은 어디있음?").
  assert.match(S.publishBlockText('draft', 'preview_only'), /확정/);
  assert.ok(S.publishBlockText('contract', 'preview_only'));
  // unknown/partial 은 "다시 시도" 를 권하지 않는다 — 중복 행이 생긴다.
  assert.equal(S.publishBlockText('state', 'unknown'), S.guidance('unknown'));
  assert.equal(S.publishBlockText('state', 'partial'), S.guidance('partial'));
  // guidance 가 없는 상태도 문구가 비지 않는다.
  assert.match(S.publishBlockText('state', 'synced'), /SVMS 반영됨/);
  assert.match(S.publishBlockText('state', 'submitting'), /SVMS 전송중/);
  // 막히지 않았으면 문구도 없다.
  assert.equal(S.publishBlockText('', 'preview_only'), '');
});

test('동작줄 상신 버튼은 계약을 모를 때도 그려진다', () => {
  // 🔴 형이 보고서를 열고 "웹에는 SVMS 푸싱 버튼이 안보이는데" 라고 물었다(2026-08-23).
  //    웹은 버튼이 미리보기 모달 안에만 있었다. 동작줄 버튼은 미리보기를 안 열어도
  //    그려져야 하고, 그때 계약은 **모르는** 상태다 -- 모른다고 닫으면 상신 경로가 없다.
  const unknown = S.publishButtonState({ publishable: null, status: 'final', sync: 'preview_only' });
  assert.equal(unknown.disabled, false);
  assert.equal(unknown.label, 'SVMS 상신');
  assert.equal(unknown.reason, '');
  // 인자 자체가 없어도 죽지 않는다(계약·상태 모름 → 확정 아님으로 막힌다).
  assert.equal(S.publishButtonState().reason, 'draft');
  // 🔴 동작줄은 계약을 캐시하지 않고 항상 `null` 을 넘긴다. 계약이 동작줄 버튼을 막는
  //    입력이 되면, 고친 뒤에도 미리보기를 다시 열기 전엔 영구 비활성이다(캐시 stale).
  //    그래서 확정+상신가능 상태에서 계약을 모른다는 이유로 막히는 일이 없어야 한다.
  ['preview_only', 'failed', null].forEach(function (sync) {
    assert.equal(S.publishButtonState({ publishable: null, status: 'final', sync: sync }).disabled,
                 false, String(sync) + ' 에서 계약 미상으로 버튼이 막히면 안 된다');
  });
  assert.equal(S.publishButtonState({ publishable: undefined, status: 'final', sync: 'preview_only' })
                .disabled, false);
});

test('동작줄 상신 버튼은 못 누를 때도 사유를 들고 있다', () => {
  const draft = S.publishButtonState({ publishable: true, status: 'editing', sync: 'preview_only' });
  assert.equal(draft.disabled, true);
  assert.match(draft.text, /확정/);
  // 계약을 아는 경우엔 그 판정을 그대로 쓴다.
  const blocked = S.publishButtonState({ publishable: false, status: 'final', sync: 'preview_only' });
  assert.equal(blocked.reason, 'contract');
  assert.ok(blocked.text);
  // 이미 반영된 보고서는 막히고, 문구는 "다시 시도" 를 권하지 않는다.
  const done = S.publishButtonState({ publishable: true, status: 'final', sync: 'synced' });
  assert.equal(done.disabled, true);
  assert.equal(done.label, 'SVMS 상신');
});

test('재상신 라벨은 failed 에서만 나온다', () => {
  // 🔴 `SP_SET_DOCK_DR` 는 멱등이 아니다. failed 외의 상태에서 재상신을 권하면 SVMS 에
  //    중복 행이 생긴다.
  assert.equal(S.publishButtonState({ publishable: true, status: 'final', sync: 'failed' }).label,
               'SVMS 재상신');
  assert.equal(S.publishButtonState({ publishable: true, status: 'final', sync: 'failed' }).disabled,
               false);
  ['preview_only', 'approved', 'submitting', 'synced', 'unknown', 'partial', null]
    .forEach(function (sync) {
      assert.equal(S.publishButtonState({ publishable: true, status: 'final', sync: sync }).label,
                   'SVMS 상신', String(sync) + ' 에서 재상신 라벨이 나오면 안 된다');
    });
});
