/* 입거 Daily Report 카드 자동번호 실행형 테스트 (node --test).
 * 문자열 존재 확인이 아니라 엔터·caret·IME·빈 줄 동작을 실제로 실행한다. */
'use strict';
const test = require('node:test');
const assert = require('node:assert');
const N = require('../static/js/dock_daily_numbering.js');

test('엔터를 누르면 새 줄에 다음 번호가 붙는다', () => {
  const first = N.renumber('Tank cleaning', 13, true);
  assert.strictEqual(first.value, '1) Tank cleaning');
  assert.strictEqual(first.caret, 16);
  const second = N.breakLine(first.value, first.caret, first.caret);
  assert.strictEqual(second.value, '1) Tank cleaning\n2) ');
  assert.strictEqual(second.caret, 20, 'caret 은 새 번호 뒤에 놓여야 한다');
  const typed = second.value.slice(0, second.caret) + 'Hull blasting';
  const third = N.breakLine(typed, typed.length, typed.length);
  assert.strictEqual(third.value, '1) Tank cleaning\n2) Hull blasting\n3) ');
  assert.strictEqual(third.caret, third.value.length);
});

test('중간 줄 끝에서 엔터를 치면 이후 항목이 다시 매겨진다', () => {
  const value = '1) A\n2) B\n3) C';
  const out = N.breakLine(value, 9, 9); // "2) B" 끝
  assert.strictEqual(out.value, '1) A\n2) B\n3) \n4) C');
  assert.strictEqual(out.caret, 13);
});

test('줄 맨 앞에서 엔터를 쳐도 항목이 유실되지 않는다', () => {
  const out = N.breakLine('1) A\n2) B', 5, 5); // "2) B" 맨 앞
  assert.strictEqual(out.value, '1) A\n2) B', '빈 항목은 만들지 않는다');
  assert.strictEqual(out.value.slice(out.caret), 'B');
});

test('선택영역을 지우고 엔터를 쳐도 남은 내용이 보존된다', () => {
  const out = N.breakLine('1) Anode renewal', 3, 8); // "Anode" 선택 후 엔터
  assert.strictEqual(out.value, '1) renewal');
  assert.strictEqual(out.value.slice(out.caret), 'renewal');
});

test('빈 줄은 caret 이 없으면 목록에서 빠지고 고아 번호가 남지 않는다', () => {
  const kept = N.renumber('1) A\n2) \n3) B', 8, true);
  assert.strictEqual(kept.value, '1) A\n2) \n3) B');
  const dropped = N.renumber('1) A\n2) \n3) B', 0, false);
  assert.strictEqual(dropped.value, '1) A\n2) B', 'blur 시 빈 항목은 정리된다');
  assert.strictEqual(N.renumber('A\n\n\nB', 0, false).value, '1) A\n2) B');
});

test('손으로 쓴 번호와 이상한 간격도 1) 2) 3) 으로 정규화된다', () => {
  const out = N.renumber('1) Tank\n2 )  Hull\n7) Anode', 0, false);
  assert.strictEqual(out.value, '1) Tank\n2) Hull\n3) Anode');
});

test('caret 이 줄 중간이면 같은 글자 위치를 유지한다', () => {
  const value = 'Tank cleaning\nHull blasting';
  const caret = 'Tank cl'.length; // "cl" 뒤
  const out = N.renumber(value, caret, true);
  assert.strictEqual(out.value, '1) Tank cleaning\n2) Hull blasting');
  assert.strictEqual(out.value.slice(0, out.caret), '1) Tank cl');
});

test('caret 이 기존 번호 접두어 안에 있어도 본문 시작으로 정착한다', () => {
  const out = N.renumber('7) Anode', 1, true);
  assert.strictEqual(out.value, '1) Anode');
  assert.strictEqual(out.value.slice(out.caret), 'Anode');
});

test('renumber 는 멱등이다', () => {
  const once = N.renumber('A\nB\nC', 0, false).value;
  const twice = N.renumber(once, 0, false).value;
  assert.strictEqual(once, '1) A\n2) B\n3) C');
  assert.strictEqual(twice, once);
});

test('needsNumbering 은 번호 없는 카드만 대상으로 한다', () => {
  assert.strictEqual(N.needsNumbering(''), false);
  assert.strictEqual(N.needsNumbering('   \n  '), false);
  assert.strictEqual(N.needsNumbering('Tank cleaning'), true);
  assert.strictEqual(N.needsNumbering('1) Tank cleaning'), false);
  // IME 조합이 끝난 한글 첫 줄도 정규화 대상이다.
  assert.strictEqual(N.needsNumbering('탱크 세정'), true);
  assert.strictEqual(N.needsNumbering('1) 탱크 세정'), false);
});

test('한글 IME 시퀀스: 조합 후 정규화하고 엔터로 다음 번호를 얻는다', () => {
  const composed = '탱크 세정';
  const normalized = N.renumber(composed, composed.length, true);
  assert.strictEqual(normalized.value, '1) 탱크 세정');
  assert.strictEqual(normalized.caret, normalized.value.length);
  const next = N.breakLine(normalized.value, normalized.caret, normalized.caret);
  assert.strictEqual(next.value, '1) 탱크 세정\n2) ');
  const typedKorean = next.value + '선체 도장';
  const after = N.renumber(typedKorean, typedKorean.length, true);
  assert.strictEqual(after.value, '1) 탱크 세정\n2) 선체 도장');
});

test('범위를 벗어난 caret 도 안전하게 처리한다', () => {
  assert.strictEqual(N.renumber('A', 999, true).value, '1) A');
  assert.strictEqual(N.renumber('A', -5, true).value, '1) A');
  assert.strictEqual(N.breakLine('A', 999, 999).value, '1) A\n2) ');
});
