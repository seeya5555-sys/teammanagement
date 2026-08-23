/* 읽기 시트의 고를 수 있는 행 / 기본 체크 규칙을 실제로 실행해서 잠근다.
 * 실행: node --test tests/dock_daily_docscan.test.js  (tests/test_dock_daily.py 가 같이 돌린다)
 *
 * 규칙 정본은 아이폰 앱 `DockDailyDocScanRules` 이고, 이 파일은 웹 미러를 그 규칙에 묶는다.
 * 두 화면이 갈리면 형은 "웹에서는 되는데 앱에서는 안 된다" 를 다시 겪는다. */
const test = require('node:test');
const assert = require('node:assert');
const R = require('../static/js/dock_daily_docscan.js');

const yard = { label: 'Deck (Yard)', target_key: 'shipyard' };
const orphan = { label: null, target_key: null, target_new: null, target_attach: null };
const row = (over) => Object.assign(
  { row_key: 'deck|a', desc: 'Docking of the vessel', verdict: 'include' }, over || {});

test('포함 판정은 이미 카드에 있는 행도 기본 체크다', () => {
  // 🔴 라이브 템플릿은 누적식이라 두 번째 읽기부터 포함 전부가 `이미 카드에 있음` 이다.
  //    옛 규칙(안 들어간 줄만 체크)은 사실상 "항상 0건 체크" 였다(형 지시 2026-08-23).
  const applied = { 'deck|a': { block_id: 7, edited: false } };
  assert.ok(R.isDefaultChecked(yard, row(), applied));
  assert.ok(R.isSelectable(yard, row(), applied));
  assert.ok(R.isDefaultChecked(yard, row(), {}), '아직 안 들어간 줄도 그대로 체크다');
});

test('형이 직접 고친 행은 고를 수도, 체크될 수도 없다', () => {
  // 서버도 `skipped_edited` 로 버린다 — 체크해 두면 "넣었다" 고 믿게 만드는 조용한 무동작.
  const applied = { 'deck|a': { block_id: 7, edited: true } };
  assert.equal(R.isLocked(row(), applied), true);
  assert.equal(R.isSelectable(yard, row(), applied), false);
  assert.equal(R.isDefaultChecked(yard, row(), applied), false);
});

test('포함이 아닌 판정은 기본 체크가 아니다(고를 수는 있다)', () => {
  for (const verdict of ['exclude', 'unknown', 'brand_new_verdict']) {
    assert.equal(R.isDefaultChecked(yard, row({ verdict }), {}), false, verdict);
    assert.ok(R.isSelectable(yard, row({ verdict }), {}), `${verdict} 는 직접 고를 수 있어야 한다`);
  }
});

test('갈 섹션이 없는 group 은 기본 체크에서 빠진다', () => {
  // 넣기를 눌러도 서버가 건너뛴다. 체크돼 있으면 형은 반영된 줄로 읽는다(올마이트 지적).
  assert.equal(R.isDropped(orphan), true);
  assert.equal(R.isDefaultChecked(orphan, row(), {}), false);
  assert.equal(R.isDropped({ target_new: 'Crew' }), false, '새로 만드는 섹션은 갈 곳이 있다');
  assert.equal(R.isDropped({ target_attach: 'sec_2' }), false, '붙이는 섹션도 갈 곳이 있다');
  assert.ok(R.isDefaultChecked({ target_new: 'Crew' }, row(), {}));
});

test('키나 문장이 빈 행은 고를 수 없고 체크되지 않는다', () => {
  // 키 없이 보내면 서버가 `unknown_row_keys` 로 되돌리는데 형은 넣었다고 읽는다.
  for (const bad of [row({ row_key: '' }), row({ row_key: '  ' }), row({ desc: '' }),
                     row({ desc: '   ' })]) {
    assert.equal(R.isUnusable(bad), true, JSON.stringify(bad));
    assert.equal(R.isSelectable(yard, bad, {}), false);
    assert.equal(R.isDefaultChecked(yard, bad, {}), false);
  }
  assert.equal(R.isUnusable(row()), false);
});

test('사진은 절대 기본 체크가 아니다', () => {
  // 🔴 이게 되살아남 방지선이다. 사진은 지워도 tombstone 이 없어(하드 삭제) 서버가
  //    막을 수 없고, 서버는 `photo_keys` 를 받은 대로 넣는다. 기본 체크로 바꾸면
  //    형이 지운 사진이 다시 읽기 한 번으로 되돌아온다.
  for (const photo of [{ photo_key: 'a1', caption: 'Rope guard', applied: false },
                       { photo_key: 'a2', caption: '', applied: true },
                       null]) {
    assert.equal(R.isPhotoDefaultChecked(photo), false, JSON.stringify(photo));
  }
});

test('이미 들어간 사진은 고를 수 없고 사유가 보인다', () => {
  const fresh = { photo_key: 'a1', caption: 'Rope guard', applied: false };
  const done = { photo_key: 'a2', caption: 'Anode', applied: true };
  assert.equal(R.isPhotoSelectable(fresh), true);
  assert.equal(R.isPhotoSelectable(done), false);
  assert.equal(R.photoNote(fresh), '');
  assert.match(R.photoNote(done), /이미/);
  assert.equal(R.isPhotoSelectable(null), false);
});

test('사진에서 조용히 빠지는 것은 전부 글로 나온다', () => {
  assert.deepEqual(R.photoWarnings({ photo_captions: true }), []);
  // 🔴 PDF 는 캡션을 못 읽는다(두 열이 한 줄로 뭉쳐 뽑힌다). 말 안 하면 형은 설명이
  //    빠진 걸 앱에서 보고 고장으로 읽는다.
  assert.match(R.photoWarnings({ photo_captions: false })[0], /PDF/);
  const warn = R.photoWarnings({ photo_captions: true, photo_duplicates: 2,
                                 photo_skipped: 1, photo_limit: 3 });
  assert.equal(warn.length, 3);
  assert.ok(warn.some(w => w.includes('2장')));
  assert.ok(warn.filter(w => w.startsWith('⚠')).length === 2);
});

test('앱과 같은 매트릭스: dropped·unusable·applied·edited 조합', () => {
  // 앱 `DockDailyDocScanContractTests` 와 같은 표다. 한쪽만 고치면 두 화면이 갈린다.
  const cases = [
    [yard, row(), {}, true],
    [yard, row(), { 'deck|a': { edited: false } }, true],
    [yard, row(), { 'deck|a': { edited: true } }, false],
    [orphan, row(), {}, false],
    [yard, row({ row_key: '' }), {}, false],
    [yard, row({ desc: ' ' }), {}, false],
    [yard, row({ verdict: 'exclude' }), {}, false],
  ];
  for (const [group, r, applied, want] of cases) {
    assert.equal(R.isDefaultChecked(group, r, applied), want,
                 JSON.stringify({ group: group.label, r, applied }));
  }
});

test('레터헤드로 빼낸 장수도 말한다', () => {
  // 🔴 세어만 두고 안 말하면 우리가 문서의 그림을 조용히 지운 것이다. 형은 문서의
  //    사진 수와 화면의 사진 수가 다른 이유를 알 수 없다.
  const warn = R.photoWarnings({ photo_captions: true, photo_letterhead: 10 });
  assert.equal(warn.length, 1);
  assert.match(warn[0], /레터헤드/);
  assert.ok(warn[0].includes('10장'));
});

test('사진이 0장인 이유는 레터헤드뿐일 때도 말한다', () => {
  // 사진이 없는 게 정상인 날은 아무 말도 하지 않는다.
  assert.equal(R.photoEmptyNote({ photos: [] }), '');
  assert.equal(R.photoEmptyNote({ photos: [{ photo_key: 'a1' }], photo_skipped: 2 }), '',
               '사진이 있으면 이 안내는 나오지 않는다');
  const dropped = R.photoEmptyNote({ photos: [], photo_skipped: 1, photo_limit: 2 });
  assert.ok(dropped.includes('3장'), dropped);
  // 🔴 쪽마다 같은 로고만 있는 PDF: 여기서 아무 말도 안 하면 형은 "문서엔 그림이
  //    보이는데 앱은 0장" 으로 읽는다.
  const only = R.photoEmptyNote({ photos: [], photo_letterhead: 10 });
  assert.match(only, /레터헤드/);
  const both = R.photoEmptyNote({ photos: [], photo_skipped: 1, photo_letterhead: 10 });
  assert.ok(both.includes('1장') && both.includes('10장'), both);
});
