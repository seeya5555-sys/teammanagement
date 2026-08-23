/* 수리신청서 Reason/Department 코드 선택 실행형 테스트 (node --test).
 *
 * 올마이트 지적(2026-08-23): "문자열 존재 검사라 실제 동작을 못 잡는다". 그래서 규칙을
 * `static/js/repair_code_select.js` 로 빼고 여기서 **실행**한다. select 를 흉내낸 DOM 스텁은
 * 일부러 안 쓴다 — 스텁이 브라우저와 다르게 굴면 가짜 초록이 되고, 그건 테스트가 없는 것보다 나쁘다.
 * 목록이 iOS 와 같은지는 `tests/test_repair_code_dropdowns.py` 가 두 소스를 대조한다.
 */
'use strict';
const test = require('node:test');
const assert = require('node:assert');
const R = require('../static/js/repair_code_select.js');

const codes = (built) => built.options.map((o) => o.code);
const legacies = (built) => built.options.filter((o) => o.legacy).map((o) => o.code);

test('기본값은 P/E 이고 목록 안에 있다', () => {
  assert.strictEqual(R.build('reason', '').selected, 'P');
  assert.strictEqual(R.build('dept', '').selected, 'E');
  assert.ok(codes(R.build('reason', '')).includes('P'));
  assert.ok(codes(R.build('dept', '')).includes('E'));
});

test('목록은 14/2개이고 코드가 중복되지 않는다', () => {
  const reason = codes(R.build('reason', 'P'));
  const dept = codes(R.build('dept', 'E'));
  assert.strictEqual(reason.length, 14);
  assert.deepStrictEqual(dept, ['D', 'E']);
  assert.strictEqual(new Set(reason).size, reason.length);
});

// 🔴 이게 올마이트가 잡은 실제 유실 경로다. 값이 비었을 때 "지금 선택" 을 유지하면
//    직전에 열어 본 신청서의 코드가 다음 건에 조용히 복사돼 그대로 SVMS 로 나간다.
test('빈 값·공백·null·undefined 는 모두 기본값으로 내려앉는다', () => {
  for (const v of ['', '   ', null, undefined, '\t\n']) {
    assert.strictEqual(R.build('reason', v).selected, 'P', JSON.stringify(v));
    assert.strictEqual(R.build('dept', v).selected, 'E', JSON.stringify(v));
    assert.deepStrictEqual(legacies(R.build('reason', v)), [], JSON.stringify(v));
  }
});

test('소문자·앞뒤 공백은 대문자 코드로 정규화된다', () => {
  for (const v of ['t', ' T ', '\tt\n']) {
    const built = R.build('reason', v);
    assert.strictEqual(built.selected, 'T', v);
    assert.deepStrictEqual(legacies(built), [], v); // 목록 안 값이므로 임시 option 없음
    assert.strictEqual(built.options.length, 14, v);
  }
});

test('목록 밖 값은 임시 option 으로 살아남고 맨 끝에 붙는다', () => {
  const built = R.build('reason', 'z9');
  assert.strictEqual(built.selected, 'Z9', '형이 적었던 값이 그대로 선택된다');
  assert.strictEqual(built.options.length, 15);
  assert.deepStrictEqual(legacies(built), ['Z9']);
  assert.strictEqual(built.options[built.options.length - 1].code, 'Z9', '정상 코드 뒤에 붙는다');
  assert.match(R.label(built.options[14]), /목록 외 기존 값/, '목록 밖이라는 걸 라벨로 알려준다');
});

test('Department 도 목록 밖 값을 지킨다', () => {
  const built = R.build('dept', 'q');
  assert.strictEqual(built.selected, 'Q');
  assert.deepStrictEqual(legacies(built), ['Q']);
});

// 🔴 통째로 다시 그리는 설계의 핵심 이득: 지난 편집의 "목록 외" option 이 남을 경로가 없다.
//    (option 을 지우는 별도 경로를 두면 한 군데만 빠뜨렸을 때 남의 코드가 목록에 남는다.)
test('다음 건을 그리면 지난 목록 밖 값은 사라진다', () => {
  const legacy = R.build('reason', 'Z9');
  assert.deepStrictEqual(legacies(legacy), ['Z9']);
  for (const next of ['T', '']) {
    const built = R.build('reason', next);
    assert.deepStrictEqual(legacies(built), [], next);
    assert.strictEqual(built.options.length, 14, next);
  }
});

test('라벨은 정상 코드에만 코드를 덧붙인다', () => {
  const built = R.build('reason', 'P');
  assert.strictEqual(R.label(built.options[0]), 'PMS (P)');
  assert.strictEqual(R.label({ code: 'Z9', name: 'Z9 (목록 외 기존 값)', legacy: true }),
    'Z9 (목록 외 기존 값)');
});

test('kind 가 reason 이 아니면 Department 목록을 쓴다(호출부 오타 안전)', () => {
  assert.deepStrictEqual(codes(R.build('dept', 'E')), codes(R.build('anything-else', 'E')));
});
