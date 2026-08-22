/* 입거 Daily Report 섹션 순서 규칙 실행형 테스트 (node --test).
 * 같은 계약이 아이폰 앱 DockDailyBlockContentTests(섹션 순서) 에도 있으니
 * 한쪽만 고치면 웹과 앱이 다른 순서를 서버에 보내게 된다. */
'use strict';
const test = require('node:test');
const assert = require('node:assert');
const O = require('../static/js/dock_daily_section_order.js');

const sec = (key, order, extra = {}) => Object.assign(
  {section_key: key, label: key.toUpperCase(), sort_order: order, kind: 'special', enabled: true},
  extra);

const keys = list => list.map(s => s.section_key);
const orders = list => list.map(s => s.sort_order);

const BASE = () => [sec('shipyard', 1, {kind: 'fixed'}), sec('sec_1', 2), sec('sec_2', 3),
                    sec('remark', 4, {kind: 'fixed'})];

test('위/아래로 한 칸씩 이웃과 자리를 바꾼다', () => {
  const up = O.movingVisible(BASE(), 'sec_2', -1);
  assert.deepStrictEqual(keys(up), ['shipyard', 'sec_2', 'sec_1', 'remark']);
  const down = O.movingVisible(BASE(), 'sec_1', 1);
  assert.deepStrictEqual(keys(down), ['shipyard', 'sec_2', 'sec_1', 'remark']);
});

test('끝단에서는 null 이라 버튼이 꺼진다', () => {
  assert.strictEqual(O.movingVisible(BASE(), 'shipyard', -1), null);
  assert.strictEqual(O.movingVisible(BASE(), 'remark', 1), null);
  assert.strictEqual(O.canMove(BASE(), 'shipyard', -1), false);
  assert.strictEqual(O.canMove(BASE(), 'shipyard', 1), true);
  assert.strictEqual(O.movingVisible(BASE(), '없는키', 1), null);
});

test('🔴 번호는 전체 목록에 매긴다 — 꺼 둔 섹션이 옛 번호로 끼어들면 안 된다', () => {
  const list = [sec('a', 1), sec('hidden', 2, {enabled: false}), sec('b', 3), sec('c', 4)];
  const moved = O.movingVisible(list, 'c', -1);
  // 보이는 이웃은 b 이므로 c 와 b 가 바뀐다. hidden 은 자리에 남지만 번호는 다시 매겨진다.
  assert.deepStrictEqual(keys(moved), ['a', 'hidden', 'c', 'b']);
  assert.deepStrictEqual(orders(moved), [1, 2, 3, 4]);
});

test('🔴 이웃은 보이는 섹션에서만 고른다 — 꺼진 섹션과 맞바꾸면 화면이 안 움직인다', () => {
  const list = [sec('a', 1), sec('hidden', 2, {enabled: false}), sec('b', 3)];
  const moved = O.movingVisible(list, 'b', -1);
  assert.deepStrictEqual(keys(moved), ['b', 'hidden', 'a'], '건너뛰고 a 와 바뀌어야 한다');
  // 꺼진 섹션 자체는 카드가 없으므로 옮길 대상이 아니다.
  assert.strictEqual(O.movingVisible(list, 'hidden', 1), null);
});

test('입력이 sort_order 순이 아니어도 화면 순서대로 판단한다', () => {
  const list = [sec('b', 9), sec('a', 2), sec('c', 30)];
  assert.deepStrictEqual(keys(O.movingVisible(list, 'a', 1)), ['b', 'a', 'c']);
});

test('원본을 건드리지 않는다', () => {
  const list = BASE();
  O.movingVisible(list, 'sec_1', 1);
  assert.deepStrictEqual(keys(list), ['shipyard', 'sec_1', 'sec_2', 'remark']);
  assert.deepStrictEqual(orders(list), [1, 2, 3, 4]);
});

test('🔴 payload: 고정 섹션은 section_key 와 sort_order 만 — label/enabled 를 보내면 서버가 400', () => {
  const body = O.payload(BASE());
  assert.deepStrictEqual(body[0], {section_key: 'shipyard', sort_order: 1});
  assert.deepStrictEqual(body[3], {section_key: 'remark', sort_order: 4});
  assert.deepStrictEqual(body[1], {section_key: 'sec_1', label: 'SEC_1', sort_order: 2, enabled: true});
});

test('payload 의 enabled 는 항상 boolean 이다', () => {
  const body = O.payload([sec('sec_1', 1, {enabled: 0}), sec('sec_2', 2, {enabled: 1})]);
  assert.strictEqual(body[0].enabled, false);
  assert.strictEqual(body[1].enabled, true);
});

// 🔴 payload 는 **넘겨받은 배열 순서**를 정답으로 본다(앱 `reorderSections` 와 같다).
// 여기서 sort_order 로 다시 정렬하면, 목록을 끌어서 새로 배열한 순서를 옛 번호가
// 되돌려 놓는다 -- 드래그가 먹지 않는 것처럼 보인다.
test('payload 는 받은 순서를 그대로 1..N 정수로 매긴다', () => {
  const body = O.payload([sec('b', 40), sec('a', 5)]);
  assert.deepStrictEqual(body.map(x => x.section_key), ['b', 'a']);
  assert.deepStrictEqual(body.map(x => x.sort_order), [1, 2]);
  for (const x of body) assert.strictEqual(Number.isInteger(x.sort_order), true);
});

// 아래 셋은 올마이트가 짚은 입력들이다(2026-08-22). 서버는 `sort_order` 정수 검증과
// `kind` CHECK 제약이 있지만, 옛 프로젝트에는 같은 번호나 빈 번호가 남아 있을 수 있다.
test('sort_order 가 겹치거나 비어 있어도 순서가 무너지지 않는다', () => {
  const list = [sec('a', 0), sec('b', 0), sec('c', 0)];
  // 같은 번호면 입력 순서를 유지한다(Array.prototype.sort 는 안정 정렬).
  assert.deepStrictEqual(keys(O.movingVisible(list, 'a', 1)), ['b', 'a', 'c']);
  const missing = [{section_key: 'a', kind: 'special', enabled: true},
                   {section_key: 'b', kind: 'special', enabled: true}];
  assert.deepStrictEqual(orders(O.movingVisible(missing, 'b', -1)), [1, 2]);
});

test('꺼진 섹션이 여러 개 연달아 있어도 보이는 이웃을 찾는다', () => {
  const list = [sec('a', 1), sec('h1', 2, {enabled: false}), sec('h2', 3, {enabled: false}),
                sec('b', 4)];
  assert.deepStrictEqual(keys(O.movingVisible(list, 'b', -1)), ['b', 'h1', 'h2', 'a']);
  assert.strictEqual(O.canMove(list, 'b', 1), false, '보이는 섹션 기준으로 마지막이다');
});

// 🔴 `special` 이 아닌 것은 전부 고정 취급이라 `label`/`enabled` 를 안 보낸다. 서버는
// 그 둘이 오면 400 으로 끊으므로, 모르는 kind 를 special 로 넘겨짚으면 순서 변경 자체가
// 실패한다 -- 안전한 쪽으로 기운다.
test('모르는 kind 는 고정 섹션처럼 다룬다', () => {
  const body = O.payload([{section_key: 'x', label: 'X', sort_order: 1, kind: 'weird', enabled: true},
                          {section_key: 'y', label: 'Y', sort_order: 2, enabled: true}]);
  assert.deepStrictEqual(body, [{section_key: 'x', sort_order: 1}, {section_key: 'y', sort_order: 2}]);
});

test('빈 목록·null 에서도 죽지 않는다', () => {
  assert.strictEqual(O.movingVisible([], 'a', 1), null);
  assert.strictEqual(O.movingVisible(null, 'a', 1), null);
  assert.deepStrictEqual(O.payload([]), []);
});
