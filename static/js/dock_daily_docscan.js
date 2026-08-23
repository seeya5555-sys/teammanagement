/* 감독 DD report(.docx/.pdf) 읽기 시트의 **고를 수 있는 행 / 기본 체크** 규칙
 * (형 지시 2026-08-23 "당일자 작업항목은 읽어올때 체크박스 자동 표시되도록해줘(포함 20건)").
 *
 * 아이폰 앱 `DockDailyDocScanRules.defaultSelection`/`isUnusable`/`isDropped` 와 **같은
 * 규칙**이다. 별 파일로 뺀 이유는 dock_daily_svms.js 와 같다: 규칙이 render 템플릿
 * 문자열 안에 있으면 실행해서 잠글 수 없다(tests/dock_daily_docscan.test.js).
 *
 * 🔴 기본 체크는 `이미 카드에 있음` 도 포함한다. 라이브 템플릿이 누적식이라 두 번째
 *    읽기부터는 포함 판정 전부가 이미 들어간 줄이고, 그때 체크가 0건이면 형이 매번 손으로
 *    20번 눌러야 한다. 옛 근거("다시 체크하면 revision 만 올라간다")는 죽었다 — 서버는
 *    내용이 같으면 아무 쓰기도 하지 않고 revision 도 올리지 않는다(routes_dock_daily.py).
 * 🔴 형이 직접 고친 줄(`edited`)은 고를 수도, 체크될 수도 없다. 서버도 `skipped_edited` 로
 *    건너뛰므로 체크해 두면 "넣었다" 고 믿게 만드는 조용한 무동작이 된다.
 * 🔴 갈 섹션이 없는 group·키나 문장이 빈 행도 체크하지 않는다(올마이트 지적 2026-08-23).
 *    넣기를 눌러도 서버가 건너뛰거나 `unknown_row_keys` 로 되돌리는데, 체크돼 있으면
 *    형은 반영된 줄로 읽는다. */
(function () {
  'use strict';

  function text(v) { return typeof v === 'string' ? v.trim() : ''; }

  /* 갈 섹션이 없는 group. 넣기를 눌러도 그 행은 빠진다. */
  function isDropped(group) {
    if (!group) return true;
    return !group.target_key && !group.target_new && !group.target_attach;
  }

  /* 키나 문장이 비어 못 쓰는 행. */
  function isUnusable(row) {
    if (!row) return true;
    return !text(row.row_key) || !text(row.desc);
  }

  /* 형이 직접 고친 카드에 있는 행 — 덮을 근거가 없다. */
  function isLocked(row, applied) {
    var done = row && applied ? applied[row.row_key] : null;
    return !!(done && done.edited);
  }

  /* 화면에서 체크박스를 누를 수 있는 행. */
  function isSelectable(group, row, applied) {
    return !isUnusable(row) && !isLocked(row, applied);
  }

  /* 읽은 직후 기본으로 체크돼 있는 행. */
  function isDefaultChecked(group, row, applied) {
    if (!isSelectable(group, row, applied)) return false;
    if (isDropped(group)) return false;
    return row.verdict === 'include';
  }

  /* ── 문서 안 사진 (형 지시 2026-08-23 Phase 3) ──────────────────────────────
   * 🔴 사진은 **기본 체크가 아니다**. 행과 규칙이 다른 이유가 둘이다.
   *    ① 서버가 `photo_keys` 를 안 받으면 0장을 넣는다 — 고른 것만 들어온다는 계약이고,
   *       화면 기본값이 체크면 그 계약이 화면에서 무의미해진다.
   *    ② 사진은 형이 지워도 tombstone 이 없다(`attachment_delete` 는 하드 삭제).
   *       기본 체크면 지운 사진이 다시 읽기 한 번으로 되살아난다 — 되살아남 방지선이
   *       바로 이 기본값이다.
   * 🔴 이미 들어간 사진(`applied`)은 고를 수 없다. 서버도 같은 바이트면 `photos_already`
   *    로 건너뛰므로, 체크해 두면 "넣었다" 고 믿게 만드는 조용한 무동작이 된다. */
  function isPhotoSelectable(photo) {
    return !!photo && !photo.applied;
  }

  function isPhotoDefaultChecked() {
    return false;
  }

  /* 사진 행에 붙는 사유. 못 고르는 이유는 화면에 글로 적는다(툴팁은 터치기기에서 안 보인다). */
  function photoNote(photo) {
    if (!photo) return '';
    return photo.applied ? '이미 이 보고서에 있음' : '';
  }

  /* 사진 묶음에 대해 형이 알아야 하는 것. 조용히 빠지면 문서에 있던 사진이 빠진 걸 모른다. */
  function photoWarnings(scan) {
    var out = [];
    if (!scan) return out;
    if (scan.photo_captions === false) out.push('PDF 는 사진 설명을 읽을 수 없어 빈 칸으로 들어갑니다');
    if (scan.photo_duplicates) out.push('같은 사진 ' + scan.photo_duplicates + '장을 접었습니다');
    /* 🔴 레터헤드로 빼낸 것도 말한다. 세어만 두고 안 말하면 우리가 조용히 지운 것이다. */
    if (scan.photo_letterhead) out.push('쪽마다 반복되는 그림 ' + scan.photo_letterhead
                                        + '장은 레터헤드로 보고 뺐습니다');
    if (scan.photo_skipped) out.push('⚠ 넣을 수 없는 그림 ' + scan.photo_skipped + '장은 빠집니다(형식·용량)');
    if (scan.photo_limit) out.push('⚠ 상한을 넘은 사진 ' + scan.photo_limit + '장은 빠집니다');
    return out;
  }

  var api = {
    isDropped: isDropped, isUnusable: isUnusable, isLocked: isLocked,
    isSelectable: isSelectable, isDefaultChecked: isDefaultChecked,
    isPhotoSelectable: isPhotoSelectable, isPhotoDefaultChecked: isPhotoDefaultChecked,
    photoNote: photoNote, photoWarnings: photoWarnings
  };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else window.DockDailyDocScanRules = api;
})();
