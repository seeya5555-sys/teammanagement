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
