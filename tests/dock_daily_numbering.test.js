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

test('Tab 은 현재 줄을 하위 - 항목으로 만들고 상위 번호만 다시 매긴다', () => {
  const value = '1) 작업 시작\n2) 작업중\n3) 다음 작업';
  const pos = value.indexOf('작업중');
  const child = N.indentLine(value, pos, pos, false);
  assert.strictEqual(child.value, '1) 작업 시작\n  - 작업중\n2) 다음 작업');
  assert.strictEqual(child.value.slice(0, child.caret), '1) 작업 시작\n  - 작업중');
});

test('하위항목에서 Enter 를 누르면 다음 - 항목이 이어진다', () => {
  const value = '1) 작업 시작\n  - 작업중';
  const out = N.breakLine(value, value.length, value.length);
  assert.strictEqual(out.value, '1) 작업 시작\n  - 작업중\n  - ');
  assert.strictEqual(out.caret, out.value.length);
});

test('하위항목 접두어 Backspace 는 그 줄을 다음 상위 번호로 되돌린다', () => {
  const value = '1) 작업 시작\n  - 작업중\n  - 작업중';
  const pos = value.lastIndexOf('  - ') + 4;
  const out = N.deleteBackward(value, pos);
  assert.strictEqual(out.value, '1) 작업 시작\n  - 작업중\n2) 작업중');
  assert.strictEqual(out.caret, value.lastIndexOf('  - ') + 3);
});

test('10번째 상위항목으로 복귀해도 caret 은 두 자리 번호 접두어 뒤에 놓인다', () => {
  const value = [...Array(9)].map((_,i)=>`${i+1}) 항목${i+1}`).join('\n')+'\n  - 열번째';
  const out = N.deleteBackward(value, value.lastIndexOf('  - ')+4);
  assert.strictEqual(out.value.endsWith('\n10) 열번째'), true);
  assert.strictEqual(out.value.slice(out.caret), '열번째');
});

test('여러 하위항목 사이에서도 상위 번호는 연속된다', () => {
  const out = N.renumber('7) A\n  - a1\n  - a2\n9) B\n  - b1', 0, false);
  assert.strictEqual(out.value, '1) A\n  - a1\n  - a2\n2) B\n  - b1');
  assert.strictEqual(N.renumber(out.value, 0, false).value, out.value, '계층 번호도 멱등');
});

test('상위항목 본문 선두 하이픈과 음수는 하위항목으로 오인하지 않는다', () => {
  assert.strictEqual(N.renumber('1) -20도에서 시험', 0, false).value, '1) -20도에서 시험');
  assert.strictEqual(N.renumber('1) -압력 확인', 0, false).value, '1) -압력 확인');
  assert.strictEqual(N.itemBody('1) -20도'), '-20도');
});

test('상위항목 접두어 Backspace 는 글자를 잃지 않고 앞줄과 합친다', () => {
  assert.strictEqual(N.deleteBackward('1) 작업 시작\n2) 작업중', 12).value, '1) 작업 시작작업중');
  assert.strictEqual(N.deleteBackward('1) 작업 시작\n  - 작업중\n2) 계속', 20).value,
                     '1) 작업 시작\n  - 작업중계속');
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

/* 백스페이스 — 형 제보 2026-08-22: 엔터로 붙은 `2) ` 가 지워지지 않았다.
 * 접두어 안을 한 글자 지우면 남은 조각이 작업내용으로 보여 번호가 다시 붙는다. */
test('빈 항목의 번호 접두어에서 백스페이스를 누르면 그 줄이 사라진다', () => {
  const out = N.deleteBackward('1) shipyard 작업진행중\n2) ', 21);
  assert.strictEqual(out.value, '1) shipyard 작업진행중');
  assert.strictEqual(out.caret, 17, 'caret 은 앞줄 끝에 놓인다');
});

test('가로채지 않으면 `2) 2` 로 되살아난다(회귀 증명)', () => {
  // 기본 삭제 = 접두어의 마지막 한 글자를 지우는 것. 그 결과를 정규화하면 번호가 되살아난다.
  const naive = N.renumber('1) A\n2', 6, true);
  assert.strictEqual(naive.value, '1) A\n2) 2', '이 되살아남이 형이 본 화면이다');
  // 가로채면 그 줄이 통째로 없어진다.
  assert.strictEqual(N.deleteBackward('1) A\n2) ', 8).value, '1) A');
});

test('내용이 있는 항목은 앞줄 끝에 이어붙는다(엔터 취소)', () => {
  assert.strictEqual(N.deleteBackward('1) A\n2) B', 5).value, '1) AB', '줄 맨 앞');
  assert.strictEqual(N.deleteBackward('1) A\n2) B', 7).value, '1) AB', '접두어 안(숫자 뒤)');
  assert.strictEqual(N.deleteBackward('1) A\n2) B', 8).caret, 4, 'caret 은 합쳐진 지점');
});

test('본문이 번호처럼 생겨도 글자를 잃지 않는다', () => {
  assert.strictEqual(N.deleteBackward('1) A\n2) 3) x', 8).value, '1) A3) x');
});

test('접두어 밖에서는 가로채지 않는다(평범한 글자 삭제)', () => {
  assert.strictEqual(N.deleteBackward('1) A\n2) B', 9), null, '본문 안');
  assert.strictEqual(N.deleteBackward('1) A', 0), null, '문서 맨 앞');
});

test('첫 줄에서는 접두어를 부수지 않고 삼킨다', () => {
  const out = N.deleteBackward('1) ', 3);
  assert.strictEqual(out.value, '1) ', '앞줄이 없으니 아무 일도 하지 않는다');
  assert.strictEqual(out.caret, 3);
  assert.strictEqual(N.deleteBackward('1) abc', 2).value, '1) abc', '숫자를 지워 `) abc` 가 되지 않는다');
});

test('두 자리 번호 경계에서도 caret 이 맞는다', () => {
  const nine = Array.from({length: 10}, (_, i) => `${i + 1}) 항목${i + 1}`).join('\n');
  const caret = nine.length;                                  // "10) 항목10" 끝
  const out = N.deleteBackward(nine, caret - '항목10'.length - 4);  // "10) " 접두어 안
  assert.strictEqual(out.value.split('\n').length, 9, '10번 줄이 9번 줄에 합쳐진다');
  assert.ok(out.value.endsWith('9) 항목9항목10'));
});

test('이모지가 섞인 본문도 잃지 않는다', () => {
  const out = N.deleteBackward('1) A\n2) 🚢선체', 7);
  assert.strictEqual(out.value, '1) A🚢선체');
});
