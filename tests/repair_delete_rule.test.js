/* 수리신청서 [삭제] 노출·확인 규칙 실행형 테스트 (node --test).
 *
 * 규칙 정본 = `static/js/repair_delete_rule.js`. DOM 스텁은 쓰지 않는다(같은 이유로
 * `repair_code_select.test.js` 도 순수함수만 실행한다 — 스텁이 브라우저와 다르게 굴면 가짜 초록).
 * 웹↔iOS 문구 파리티는 `tests/test_repair_delete_button.py` 가 두 소스를 대조한다.
 */
'use strict';
const test = require('node:test');
const assert = require('node:assert');
const R = require('../static/js/repair_delete_rule.js');

const row = (status, rep_cd) => ({ id: 1, status, rep_cd: rep_cd ?? null });

test('초안은 한 번만 묻고 확인 문구 없이 지운다(옛 계약 유지)', () => {
  for (const st of ['pending', 'failed']) {
    const rule = R.deleteRule(row(st));
    assert.strictEqual(rule.visible, true, st);
    assert.strictEqual(rule.token, null, st);
    assert.strictEqual(rule.prompts.length, 1, st);
  }
});

// 🔴 러너가 물은 뒤(saving)에 지우면 SVMS 에 문서가 생겼는지 영구히 알 수 없다(`/result` 가 404).
test('저장 진행 중(saving)은 버튼이 아예 안 보인다', () => {
  const rule = R.deleteRule(row('saving'));
  assert.strictEqual(rule.visible, false);
  assert.deepStrictEqual(rule.prompts, []);
  assert.strictEqual(rule.token, null);
});

// 🔴 approved 는 아직 SVMS 전이다(러너는 claim 으로 saving 으로 돌린 뒤에만 SVMS 에 쓴다).
//    막아 두면 러너가 죽은 사이 고인 행이 편집·삭제·resolve 전부 안 되는 영구 stuck 이 된다.
test('저장 대기(approved)는 경고를 한 번 물어보고 지울 수 있다', () => {
  const rule = R.deleteRule(row('approved'));
  assert.strictEqual(rule.visible, true);
  assert.strictEqual(rule.token, null);
  assert.strictEqual(rule.prompts.length, 1);
  assert.match(rule.prompts[0], /SVMS 저장을 기다리는 중입니다/);
  assert.match(rule.prompts[0], /이미 물었으면 삭제가 거부됩니다/);
});

test('SVMS 저장본은 2단 확인 + 확인 문구를 붙인다', () => {
  const rule = R.deleteRule(row('saved', 'BGBBMD26081401'));
  assert.strictEqual(rule.visible, true);
  assert.strictEqual(rule.token, 'TRMT에서만삭제');
  assert.strictEqual(rule.prompts.length, 2);
});

// 🔴 이게 이 기능의 가장 위험한 오해다. TRMT 에는 SVMS 수리신청서를 지우는 경로가 없다.
//    1단 문구가 그 사실을 말하지 않으면 형은 SVMS 를 정리했다고 믿는다.
test('1단 문구는 SVMS 문서가 남는다는 사실과 REP_CD 를 말한다', () => {
  const [first, second] = R.deleteRule(row('saved', 'BGBBMD26081401')).prompts;
  assert.match(first, /SVMS 문서는 그대로 남습니다/);
  assert.match(first, /BGBBMD26081401/);
  assert.match(second, /되돌릴 수 없습니다/);
  assert.match(second, /BGBBMD26081401/);
});

// REP_CD 가 있으면 status 가 무엇이든 SVMS 문서가 존재한다 → 초안 취급 금지.
test('failed + REP_CD 도 SVMS 저장본으로 다룬다', () => {
  const rule = R.deleteRule(row('failed', 'TSTVME77'));
  assert.strictEqual(rule.token, 'TRMT에서만삭제');
  assert.strictEqual(rule.prompts.length, 2);
});

test('빈 문자열 REP_CD 는 미저장으로 본다(서버도 NULL/빈값을 같이 취급)', () => {
  const rule = R.deleteRule(row('failed', ''));
  assert.strictEqual(rule.token, null);
  assert.strictEqual(rule.prompts.length, 1);
});

// 🔴 fail-closed. "아마 초안" 으로 봐 주면 SVMS 에 뭐가 있는지 모르는 행이 지워진다.
//    서버도 같은 상태를 409 로 막는다(REP_CD 없는 `saved` 는 이상행이다).
test('낯선 상태·REP_CD 없는 saved 는 버튼을 숨긴다', () => {
  for (const v of [undefined, null, '', 'weird', 'saved']) {
    const rule = R.deleteRule({ id: 1, status: v, rep_cd: null });
    assert.strictEqual(rule.visible, false, String(v));
    assert.deepStrictEqual(rule.prompts, [], String(v));
  }
});

test('행 자체가 없어도 던지지 않는다(목록 갱신 경쟁)', () => {
  assert.strictEqual(R.deleteRule(undefined).visible, false);
  assert.strictEqual(R.deleteRule({}).token, null);
});
