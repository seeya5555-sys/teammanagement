// ════════════════════════════════════════════════════════════════
//  Dry Dock Report Editor — Step 2 (v2)
//  · 목차 트리 관리 (1, 1-1, 1-1-1)
//  · 블록 4종 인라인 편집: paragraph / bullet_list / table / image(gallery)
//  · 블록 헤더 제거 — 호버 시 우측에 작은 컨트롤만 노출
//  · 표: 컬럼 너비 드래그 조정
//  · 이미지: 한 블록에 여러 장 (2×N 등 그리드 배치)
//  · 자동 저장 (debounce 500ms)
// ════════════════════════════════════════════════════════════════
const E = {
  reportId: window.DDE_REPORT_ID,
  report: null,
  sectionsFlat: [],
  tree: [],
  byId: new Map(),
  activeSecId: null,
  saveTimer: null,
  canEdit: true,   // 서버에서 보내주는 can_edit 플래그
};

const $  = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === 'class') e.className = v;
    else if (k.startsWith('on') && typeof v === 'function') e.addEventListener(k.slice(2), v);
    else if (v === true) e.setAttribute(k, '');
    else if (v !== false && v != null) e.setAttribute(k, v);
  }
  for (const c of children) {
    if (c == null || c === false) continue;
    e.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return e;
}

async function api(url, opts = {}) {
  const headers = opts.body instanceof FormData
    ? (opts.headers || {})
    : { 'Content-Type': 'application/json', ...(opts.headers || {}) };
  const r = await fetch(url, { headers, ...opts });
  if (!r.ok) {
    let msg = `${r.status} ${r.statusText}`;
    try { const j = await r.json(); if (j.error) msg = j.error; } catch {}
    throw new Error(msg);
  }
  return r.status === 204 ? null : r.json();
}

function setSaveStatus(text, kind = '') {
  const s = $('#dde-save-status');
  s.textContent = text;
  s.className = 'dde-save-status' + (kind ? ' dde-save-' + kind : '');
}

// ─────────────────────────────────────────────────────────────
//  Init / Load
// ─────────────────────────────────────────────────────────────
async function init() {
  try {
    await loadReport();
    bindEvents();
  } catch (e) {
    alert('보고서 로드 실패: ' + e.message);
    window.location = '/dry-dock';
  }
}

async function loadReport() {
  const r = await api(`/api/dock-reports/${E.reportId}`);
  E.report = r;
  E.sectionsFlat = r.sections || [];
  E.canEdit = !!r.can_edit;

  $('#dde-title').textContent = r.title || '제목 없음';
  const subs = [];
  if (r.vessel_name) subs.push(r.vessel_name);
  if (r.dock_no)     subs.push(r.dock_no);
  if (r.shipyard)    subs.push(r.shipyard);
  if (r.period_start || r.period_end) {
    subs.push(`${(r.period_start||'').replace(/-/g,'.')} ~ ${(r.period_end||'').replace(/-/g,'.')}`);
  }
  $('#dde-subtitle').textContent = subs.join('   ·   ');

  // 읽기 전용 모드 시각화
  document.body.classList.toggle('dde-readonly', !E.canEdit);
  const ro = $('#dde-readonly-banner');
  if (ro) ro.hidden = E.canEdit;

  // 좌측 사이드바의 편집 버튼 비활성화
  const btnAddSec = $('#dde-btn-add-section');
  const btnAddSub = $('#dde-btn-add-sub');
  const btnBulk   = $('#dde-btn-bulk-add');
  const btnDelSec = $('#dde-btn-del-section');
  if (btnAddSec) btnAddSec.style.display = E.canEdit ? '' : 'none';
  if (btnAddSub) btnAddSub.style.display = E.canEdit ? '' : 'none';
  if (btnBulk)   btnBulk.style.display   = E.canEdit ? '' : 'none';
  if (btnDelSec) btnDelSec.style.display = E.canEdit ? '' : 'none';

  // 섹션 제목 input 읽기 전용
  const titleInp = $('#dde-section-title');
  if (titleInp) titleInp.readOnly = !E.canEdit;

  buildTree();
  renderTOC();
  if (E.activeSecId && !E.byId.has(E.activeSecId)) E.activeSecId = null;
  if (!E.activeSecId && E.tree.length > 0) E.activeSecId = E.tree[0].id;
  renderEditor();
}

function buildTree() {
  E.byId.clear();
  const map = new Map();
  for (const s of E.sectionsFlat) map.set(s.id, { ...s, children: [] });
  const roots = [];
  for (const s of map.values()) {
    if (s.parent_id && map.has(s.parent_id)) {
      map.get(s.parent_id).children.push(s);
    } else { roots.push(s); }
  }
  function sortRec(arr) {
    arr.sort((a, b) => a.display_order - b.display_order || a.id - b.id);
    for (const x of arr) sortRec(x.children);
  }
  sortRec(roots);
  function walk(nodes, prefix = '', depth = 0, parent = null) {
    nodes.forEach((n, i) => {
      const num = prefix ? `${prefix}-${i + 1}` : `${i + 1}`;
      E.byId.set(n.id, {
        section: n, parent, depth, number: num,
        siblings: nodes, indexInSiblings: i,
      });
      walk(n.children, num, depth + 1, n);
    });
  }
  walk(roots);
  E.tree = roots;
}

// ─────────────────────────────────────────────────────────────
//  TOC
// ─────────────────────────────────────────────────────────────
function renderTOC() {
  const root = $('#dde-toc');
  root.innerHTML = '';
  if (E.tree.length === 0) {
    root.append(el('div', { class: 'dde-toc-empty' },
      '아직 섹션이 없습니다.', el('br'), '+ 섹션 버튼으로 시작하세요.'));
  } else {
    renderTOCNodes(E.tree, root);
  }
  const subBtn = $('#dde-btn-add-sub');
  const info = E.activeSecId ? E.byId.get(E.activeSecId) : null;
  subBtn.disabled = !(info && info.depth < 2);
}

function renderTOCNodes(nodes, container) {
  for (const n of nodes) {
    const info = E.byId.get(n.id);
    const item = el('div', {
      class: 'dde-toc-item' + (E.activeSecId === n.id ? ' active' : '') +
             ` depth-${info.depth}`,
      onclick: (ev) => {
        if (ev.target.closest('.dde-toc-actions')) return;
        E.activeSecId = n.id;
        renderTOC();
        renderEditor();
      },
    });
    item.append(
      el('span', { class: 'dde-toc-no' }, info.number + '.'),
      el('span', { class: 'dde-toc-title' }, n.title || '(제목 없음)'),
      E.canEdit ? el('div', { class: 'dde-toc-actions' },
        el('button', { class: 'dde-toc-btn', title: '위로',
          onclick: (e) => { e.stopPropagation(); moveSection(n.id, 'up'); }}, '↑'),
        el('button', { class: 'dde-toc-btn', title: '아래로',
          onclick: (e) => { e.stopPropagation(); moveSection(n.id, 'down'); }}, '↓'),
      ) : null,
    );
    container.append(item);
    if (n.children.length > 0) {
      const subWrap = el('div', { class: 'dde-toc-children' });
      renderTOCNodes(n.children, subWrap);
      container.append(subWrap);
    }
  }
}

async function addSection(parentId = null) {
  const title = prompt(parentId ? '새 하위 섹션 제목:' : '새 섹션 제목:', '');
  if (title === null) return;
  const t = title.trim() || '새 섹션';
  try {
    const r = await api(`/api/dock-reports/${E.reportId}/sections`, {
      method: 'POST',
      body: JSON.stringify({ title: t, parent_id: parentId }),
    });
    E.activeSecId = r.id;
    await loadReport();
  } catch (e) { alert('섹션 추가 실패: ' + e.message); }
}

async function deleteSection(sid) {
  const info = E.byId.get(sid);
  if (!info) return;
  const hasChildren = info.section.children.length > 0;
  const msg = hasChildren
    ? `"${info.section.title}" 및 하위 섹션 ${countDescendants(info.section)}개와 모든 블록을 삭제합니다. 계속할까요?`
    : `"${info.section.title}"과 모든 블록을 삭제합니다. 계속할까요?`;
  if (!confirm(msg)) return;
  try {
    await api(`/api/dock-sections/${sid}`, { method: 'DELETE' });
    if (E.activeSecId === sid) E.activeSecId = null;
    await loadReport();
  } catch (e) { alert('삭제 실패: ' + e.message); }
}

function countDescendants(node) {
  let c = node.children.length;
  for (const ch of node.children) c += countDescendants(ch);
  return c;
}

async function moveSection(sid, direction) {
  try {
    await api(`/api/dock-sections/${sid}/move`, {
      method: 'POST', body: JSON.stringify({ direction }),
    });
    await loadReport();
  } catch (e) { alert('순서 변경 실패: ' + e.message); }
}

// ─── 일괄 추가 ───────────────────────────────────────────────
// 텍스트 한 줄당 1 섹션. 줄 앞 Tab 개수(또는 4 스페이스 단위)로 들여쓰기
// 빈 줄과 # 으로 시작하는 줄은 무시
function parseBulkText(text) {
  const out = [];
  const lines = text.split(/\r?\n/);
  for (const raw of lines) {
    if (!raw.trim()) continue;
    if (raw.trim().startsWith('#')) continue;
    // 줄 앞 Tab 또는 스페이스 카운트
    let indent = 0;
    let i = 0;
    while (i < raw.length) {
      if (raw[i] === '\t') { indent += 1; i += 1; }
      else if (raw[i] === ' ') {
        // 4 스페이스 = Tab 1개 (4개씩 끊어서)
        let sp = 0;
        while (i < raw.length && raw[i] === ' ' && sp < 4) { sp++; i++; }
        if (sp === 4) indent += 1;
        else break;
      } else break;
    }
    // 최대 깊이 2 (1단계 / 2단계 / 3단계 = depth 0~2)
    indent = Math.min(2, indent);
    const title = raw.slice(i).trim();
    if (!title) continue;
    out.push({ indent, title });
  }
  return out;
}

function openBulkAddDialog() {
  const m = $('#dde-bulk-modal');
  $('#dde-bulk-text').value = '';
  $('#dde-bulk-preview').hidden = true;

  // "현재 섹션 아래에 추가" 옵션 가용 여부
  const underRadio = $('input[name="dde-bulk-target"][value="under"]');
  const underLabel = $('#dde-bulk-under-label');
  const curTitle = $('#dde-bulk-current-title');
  const info = E.activeSecId ? E.byId.get(E.activeSecId) : null;
  // 현재 섹션 depth가 0 또는 1이어야 그 아래로 1~2단계 추가 가능
  if (info && info.depth < 2) {
    underRadio.disabled = false;
    underLabel.style.opacity = '1';
    curTitle.textContent = `"${info.section.title}"`;
  } else {
    underRadio.disabled = true;
    underLabel.style.opacity = '0.4';
    curTitle.textContent = '—';
  }
  // 기본 선택: 최상위
  $('input[name="dde-bulk-target"][value="root"]').checked = true;

  m.hidden = false;
  document.body.classList.add('modal-open');
  setTimeout(() => $('#dde-bulk-text').focus(), 50);
}

function closeBulkAddDialog() {
  $('#dde-bulk-modal').hidden = true;
  document.body.classList.remove('modal-open');
}

async function applyBulkAdd() {
  const text = $('#dde-bulk-text').value;
  const parsed = parseBulkText(text);
  if (parsed.length === 0) {
    alert('추가할 섹션이 없습니다.');
    return;
  }
  // 깊이 검증 — 부모 없이 indent > 0인 첫 항목은 자동 보정
  // 첫 항목은 무조건 indent 0으로
  if (parsed[0].indent > 0) parsed[0].indent = 0;

  const targetMode = document.querySelector('input[name="dde-bulk-target"]:checked').value;
  let basePid = null;
  let baseDepth = 0;
  if (targetMode === 'under' && E.activeSecId) {
    const info = E.byId.get(E.activeSecId);
    if (info && info.depth < 2) {
      basePid = E.activeSecId;
      baseDepth = info.depth + 1;
    }
  }

  // 깊이 검증 — 추가 후 최대 깊이 2(=3단계) 초과 방지
  const overflow = parsed.some(p => baseDepth + p.indent > 2);
  if (overflow) {
    alert('최대 3단계까지만 추가할 수 있습니다. (들여쓰기 깊이 줄이기 필요)');
    return;
  }

  // 진행 — depth 별 parent stack
  const btn = $('#dde-bulk-apply');
  btn.disabled = true;
  btn.textContent = `추가 중... (0/${parsed.length})`;

  const parents = [basePid, null, null];  // depth별 마지막 부모 id

  try {
    let done = 0;
    for (const item of parsed) {
      const lv = item.indent;
      const parentId = lv === 0 ? basePid : parents[lv - 1];
      const r = await api(`/api/dock-reports/${E.reportId}/sections`, {
        method: 'POST',
        body: JSON.stringify({ title: item.title, parent_id: parentId }),
      });
      parents[lv] = r.id;
      // 하위 레벨 부모 stack 리셋
      for (let i = lv + 1; i < parents.length; i++) parents[i] = null;
      done += 1;
      btn.textContent = `추가 중... (${done}/${parsed.length})`;
    }
    closeBulkAddDialog();
    await loadReport();
    setSaveStatus(`섹션 ${parsed.length}개 추가됨`, 'ok');
  } catch (e) {
    alert('일괄 추가 중 오류: ' + e.message + '\n일부만 추가되었을 수 있습니다.');
    await loadReport();
  } finally {
    btn.disabled = false;
    btn.textContent = '추가';
  }
}

async function saveSectionTitle(sid, title) {
  if (!title.trim()) return;
  setSaveStatus('저장 중...', 'busy');
  try {
    await api(`/api/dock-sections/${sid}`, {
      method: 'PUT', body: JSON.stringify({ title: title.trim() }),
    });
    const info = E.byId.get(sid);
    if (info) info.section.title = title.trim();
    renderTOC();
    setSaveStatus('저장됨', 'ok');
  } catch (e) { setSaveStatus('저장 실패: ' + e.message, 'err'); }
}

// ─────────────────────────────────────────────────────────────
//  Editor (우측)
// ─────────────────────────────────────────────────────────────
function renderEditor() {
  const empty = $('#dde-main-empty');
  const editor = $('#dde-section-edit');
  if (!E.activeSecId || !E.byId.has(E.activeSecId)) {
    empty.hidden = false;
    editor.hidden = true;
    return;
  }
  const info = E.byId.get(E.activeSecId);
  const sec = info.section;

  empty.hidden = true;
  editor.hidden = false;
  $('#dde-section-no').textContent = info.number + '.';
  $('#dde-section-title').value = sec.title || '';

  const blocksWrap = $('#dde-blocks');
  blocksWrap.innerHTML = '';
  const blocks = (sec.blocks || []).slice().sort((a, b) =>
    a.display_order - b.display_order || a.id - b.id);

  if (blocks.length === 0) {
    if (E.canEdit) {
      blocksWrap.append(renderEmptyInserter());
    } else {
      blocksWrap.append(el('div', { class: 'dde-blocks-empty-ro' },
        '이 섹션에는 작성된 내용이 없습니다.'));
    }
    return;
  }

  // 블록이 있을 때
  // - 편집 권한 있으면 inserter + tail-add도 렌더
  if (E.canEdit) {
    blocksWrap.append(renderInserter(0));
  }
  blocks.forEach((b, idx) => {
    blocksWrap.append(renderBlock(b, idx, blocks.length));
    if (E.canEdit && idx < blocks.length - 1) {
      blocksWrap.append(renderInserter(idx + 1));
    }
  });
  if (E.canEdit) {
    blocksWrap.append(renderTailAdder(blocks.length));
  }
}

// 맨 끝에 항상 보이는 추가 영역 — 4가지 종류 작은 버튼 가로 배치
function renderTailAdder(position) {
  const wrap = el('div', { class: 'dde-tail-add' });
  wrap.append(el('span', { class: 'dde-tail-add-label' }, '+ 블록 추가:'));
  const items = [
    { type: 'paragraph',   icon: 'T',  label: '텍스트' },
    { type: 'bullet_list', icon: '•',  label: '불릿' },
    { type: 'table',       icon: '▦',  label: '표' },
    { type: 'image',       icon: '🖼', label: '사진' },
  ];
  for (const it of items) {
    wrap.append(el('button', {
      class: 'dde-tail-btn', type: 'button',
      onclick: () => addBlockAt(it.type, position),
    },
      el('span', { class: 'dde-tail-icon' }, it.icon),
      el('span', {}, it.label),
    ));
  }
  return wrap;
}

// 섹션에 블록이 하나도 없을 때 보여줄 큰 추가 영역
function renderEmptyInserter() {
  const wrap = el('div', { class: 'dde-empty-add' });
  wrap.append(el('div', { class: 'dde-empty-add-hint' },
    '이 섹션에 추가할 블록 종류를 선택하세요'));
  const grid = el('div', { class: 'dde-empty-add-grid' });
  const items = [
    { type: 'paragraph',   icon: 'T',  label: '텍스트', desc: '단락 본문' },
    { type: 'bullet_list', icon: '•',  label: '불릿 리스트', desc: '항목 나열' },
    { type: 'table',       icon: '▦',  label: '표', desc: '행/열 데이터' },
    { type: 'image',       icon: '🖼', label: '사진 갤러리', desc: '여러 장 가능' },
  ];
  for (const it of items) {
    grid.append(el('button', {
      class: 'dde-empty-add-btn', type: 'button',
      onclick: () => addBlockAt(it.type, 0),
    },
      el('span', { class: 'dde-empty-add-icon' }, it.icon),
      el('span', { class: 'dde-empty-add-label' }, it.label),
      el('span', { class: 'dde-empty-add-desc' }, it.desc),
    ));
  }
  wrap.append(grid);
  return wrap;
}

function renderInserter(position) {
  const ins = el('div', { class: 'dde-inserter' });
  const btn = el('button', {
    class: 'dde-inserter-btn',
    type: 'button',
    title: '여기에 블록 추가',
    onclick: (e) => {
      e.stopPropagation();
      showInsertMenu(btn, position);
    },
  }, '+');
  ins.append(btn);
  return ins;
}

function showInsertMenu(anchor, position) {
  $$('.dde-insert-menu').forEach(m => m.remove());
  const menu = el('div', { class: 'dde-insert-menu' });
  const items = [
    { type: 'paragraph',   icon: 'T',  label: '텍스트' },
    { type: 'bullet_list', icon: '•',  label: '불릿 리스트' },
    { type: 'table',       icon: '▦',  label: '표' },
    { type: 'image',       icon: '🖼', label: '사진 (갤러리)' },
  ];
  for (const it of items) {
    menu.append(el('button', {
      class: 'dde-insert-item', type: 'button',
      onclick: () => { menu.remove(); addBlockAt(it.type, position); },
    }, el('span', { class: 'dde-insert-icon' }, it.icon),
       el('span', {}, it.label)));
  }
  document.body.append(menu);
  const r = anchor.getBoundingClientRect();
  // 메뉴는 버튼 우측에 띄움 (화면 우측 벗어나면 좌측으로)
  const menuWidth = 200;
  let left = r.right + 8;
  if (left + menuWidth > window.innerWidth) left = r.left - menuWidth - 8;
  menu.style.top = (r.top + window.scrollY) + 'px';
  menu.style.left = left + 'px';

  setTimeout(() => {
    const onDocClick = (e) => {
      if (!menu.contains(e.target)) {
        menu.remove();
        document.removeEventListener('click', onDocClick);
      }
    };
    document.addEventListener('click', onDocClick);
  }, 0);
}

function renderBlock(b, idx, total) {
  const wrap = el('div', { class: `dde-block dde-block-${b.block_type}`, 'data-id': b.id });

  if (E.canEdit) {
    const controls = el('div', { class: 'dde-block-controls' },
      el('button', { class: 'dde-block-btn', title: '위로', disabled: idx === 0,
        onclick: () => moveBlock(b.id, 'up') }, '↑'),
      el('button', { class: 'dde-block-btn', title: '아래로', disabled: idx === total - 1,
        onclick: () => moveBlock(b.id, 'down') }, '↓'),
      el('button', { class: 'dde-block-btn dde-block-del', title: '삭제',
        onclick: () => deleteBlock(b.id) }, '✕'),
    );
    wrap.append(controls);
  }

  const body = el('div', { class: 'dde-block-body' });
  if (b.block_type === 'paragraph')        renderParagraph(body, b);
  else if (b.block_type === 'bullet_list') renderBulletList(body, b);
  else if (b.block_type === 'table')       renderTable(body, b);
  else if (b.block_type === 'image')       renderImageGallery(body, b);
  wrap.append(body);
  return wrap;
}

// ─── paragraph ───────────────────────────────────────────────
function renderParagraph(body, b) {
  const ta = el('textarea', {
    class: 'dde-p-input', placeholder: '내용을 입력하세요...', rows: 3,
  });
  ta.value = b.content?.text || '';
  ta.addEventListener('input', () => {
    autoResize(ta);
    scheduleBlockSave(b.id, () => ({ text: ta.value }));
  });
  body.append(ta);
  // DOM에 부착된 다음에 autoResize 실행 (scrollHeight가 정상 계산됨)
  setTimeout(() => autoResize(ta), 0);
}

function autoResize(ta) {
  // 'auto'로 일단 줄여서 scrollHeight 정확히 측정 후 다시 늘림
  ta.style.height = 'auto';
  ta.style.height = Math.max(ta.scrollHeight + 2, 40) + 'px';
}


// ════════════════════════════════════════════════════════════════
//  표 paste 헬퍼: Excel/Google Sheets에서 복사한 데이터 → 2D 배열
// ════════════════════════════════════════════════════════════════

/**
 * TSV/CSV 텍스트를 2D 배열로 파싱.
 * 엑셀이 \r\n으로 행 구분, \t로 셀 구분.
 * 셀 안에 줄바꿈이 있는 경우 그 셀은 "..."로 인용됨 (RFC 4180 스타일)
 */
function parseTsv(text) {
  if (!text) return null;
  // 끝의 trailing newline 제거
  text = text.replace(/[\r\n]+$/, '');
  if (!text) return null;

  // 탭 또는 다중 공백이 보이지 않으면 표가 아님
  const hasTab = text.includes('\t');
  const hasMultiLine = /\r?\n/.test(text);
  if (!hasTab && !hasMultiLine) return null;

  const rows = [];
  let i = 0;
  let cur = '';
  let curRow = [];
  let inQuote = false;
  while (i < text.length) {
    const ch = text[i];
    if (inQuote) {
      if (ch === '"') {
        if (text[i + 1] === '"') { cur += '"'; i += 2; continue; }
        inQuote = false; i++; continue;
      }
      cur += ch; i++; continue;
    }
    if (ch === '"' && cur === '') {
      // 셀 시작의 인용
      inQuote = true; i++; continue;
    }
    if (ch === '\t') { curRow.push(cur); cur = ''; i++; continue; }
    if (ch === '\r') { i++; continue; }
    if (ch === '\n') {
      curRow.push(cur); rows.push(curRow);
      curRow = []; cur = ''; i++; continue;
    }
    cur += ch; i++;
  }
  curRow.push(cur);
  rows.push(curRow);

  // 빈 행 제거 (모든 셀이 빈 문자열인 경우)
  return rows.filter(r => r.some(c => c.trim() !== ''));
}

/**
 * HTML 안의 <table>을 2D 배열로 파싱.
 * Excel 클립보드는 HTML도 함께 넣어주므로, 그게 있으면 더 정확.
 */
function parseHtmlTable(html) {
  try {
    const doc = new DOMParser().parseFromString(html, 'text/html');
    const tbl = doc.querySelector('table');
    if (!tbl) return null;
    const result = [];
    tbl.querySelectorAll('tr').forEach(tr => {
      const row = [];
      tr.querySelectorAll('th, td').forEach(c => {
        // 셀 내용에서 의미 없는 줄바꿈 제거, 단 <br>은 \n으로
        let text = c.innerHTML
          .replace(/<br\s*\/?>/gi, '\n')
          .replace(/<[^>]+>/g, '')
          .replace(/&nbsp;/g, ' ')
          .replace(/&amp;/g, '&')
          .replace(/&lt;/g, '<')
          .replace(/&gt;/g, '>');
        // 줄 단위 trim 후, 빈 줄 제거
        text = text.split('\n').map(s => s.trim()).filter(Boolean).join('\n');
        row.push(text);
      });
      if (row.length > 0) result.push(row);
    });
    return result.length > 0 ? result : null;
  } catch (e) {
    return null;
  }
}

/**
 * 현재 포커스된 input/textarea에 plain text 삽입 (paste 동작 직접 구현)
 */
function insertPlainTextAtFocused(el, text) {
  if (!el || (el.tagName !== 'INPUT' && el.tagName !== 'TEXTAREA')) return;
  const start = el.selectionStart ?? el.value.length;
  const end   = el.selectionEnd   ?? el.value.length;
  el.value = el.value.slice(0, start) + text + el.value.slice(end);
  el.selectionStart = el.selectionEnd = start + text.length;
  // 변경 이벤트 발생 (oninput 핸들러 트리거)
  el.dispatchEvent(new Event('input', { bubbles: true }));
}

// ─── bullet_list ─────────────────────────────────────────────
// marker 종류: 'bullet'(•) / 'dash'(-) / 'number'(1)) / 'alpha'(a))
// items: [{text:string, indent:number}, ...]  (indent: 0~3)
// 구버전 호환: items가 문자열 배열이면 {text, indent:0}으로 변환
function normalizeBulletItems(items) {
  return (items || []).map(it =>
    typeof it === 'string' ? { text: it, indent: 0 } : {
      text: it.text || '',
      indent: Math.max(0, Math.min(3, it.indent || 0)),
    });
}

const MAX_INDENT = 3;

// marker 종류: 'bullet'(•) / 'dash'(-) / 'number' / 'alpha'
// number 들여쓰기별 형식: 0=1.  1=1)  2=①  3=a)
// alpha  들여쓰기별 형식: 0=a.  1=a)  2=①  3=1)
const CIRCLED_NUMS = ['①','②','③','④','⑤','⑥','⑦','⑧','⑨','⑩',
                       '⑪','⑫','⑬','⑭','⑮','⑯','⑰','⑱','⑲','⑳'];

function numberMarkerByDepth(depth, n) {
  // n: 1-based 카운터
  if (depth === 0) return `${n}.`;
  if (depth === 1) return `${n})`;
  if (depth === 2) return CIRCLED_NUMS[(n - 1) % CIRCLED_NUMS.length];
  // depth >= 3
  return `${String.fromCharCode(96 + ((n - 1) % 26) + 1)})`;
}

function alphaMarkerByDepth(depth, n) {
  if (depth === 0) return `${String.fromCharCode(96 + ((n - 1) % 26) + 1)}.`;
  if (depth === 1) return `${String.fromCharCode(96 + ((n - 1) % 26) + 1)})`;
  if (depth === 2) return CIRCLED_NUMS[(n - 1) % CIRCLED_NUMS.length];
  return `${n})`;
}

// 들여쓰기 레벨별 카운터로 마커 계산
function computeBulletMarkers(items, kind) {
  const markers = [];
  const counters = [0, 0, 0, 0];   // level 0~3
  for (const it of items) {
    const lv = Math.max(0, Math.min(MAX_INDENT, it.indent || 0));
    counters[lv]++;
    for (let i = lv + 1; i <= MAX_INDENT; i++) counters[i] = 0;
    if (kind === 'dash')         markers.push('–');
    else if (kind === 'number')  markers.push(numberMarkerByDepth(lv, counters[lv]));
    else if (kind === 'alpha')   markers.push(alphaMarkerByDepth(lv, counters[lv]));
    else                          markers.push('•');
  }
  return markers;
}

function renderBulletList(body, b) {
  const list = el('div', { class: 'dde-bullet-list' });
  let items = normalizeBulletItems(b.content?.items);
  if (items.length === 0) items = [{ text: '', indent: 0 }];
  let marker = b.content?.marker || 'bullet';

  function getCurrent() {
    return {
      items: items.map(it => ({ text: it.text, indent: it.indent })),
      marker,
    };
  }

  function rebuild() {
    list.innerHTML = '';
    const markers = computeBulletMarkers(items, marker);
    items.forEach((it, i) => {
      const row = el('div', {
        class: `dde-bullet-row dde-bullet-${marker} indent-${it.indent}`,
        'data-indent': it.indent,
      });
      const inp = el('textarea', {
        class: 'dde-bullet-input', placeholder: '항목...', rows: 1,
        oninput: (e) => {
          items[i].text = e.target.value;
          autoResize(e.target);
          scheduleBlockSave(b.id, getCurrent);
        },
        onkeydown: (e) => {
          if (e.key === 'Tab') {
            // Tab → indent +1, Shift+Tab → indent -1
            e.preventDefault();
            if (e.shiftKey) {
              if (items[i].indent > 0) {
                items[i].indent -= 1;
                rebuild();
                focusItem(i);
                scheduleBlockSave(b.id, getCurrent);
              }
            } else {
              if (items[i].indent < MAX_INDENT) {
                items[i].indent += 1;
                rebuild();
                focusItem(i);
                scheduleBlockSave(b.id, getCurrent);
              }
            }
            return;
          }
          if (e.key === 'Enter' && !e.shiftKey) {
            // Enter: 새 항목 추가
            // Shift+Enter: 같은 항목 내에서 줄바꿈 (기본 동작 유지)
            e.preventDefault();
            items.splice(i + 1, 0, { text: '', indent: items[i].indent });
            rebuild();
            focusItem(i + 1);
            scheduleBlockSave(b.id, getCurrent);
            return;
          }
          if (e.key === 'Backspace' && !e.target.value) {
            // 빈 줄에서 백스페이스
            // - indent가 있으면 먼저 내어쓰기
            // - indent 0이고 항목이 여러 개면 항목 삭제
            if (items[i].indent > 0) {
              e.preventDefault();
              items[i].indent -= 1;
              rebuild();
              focusItem(i);
              scheduleBlockSave(b.id, getCurrent);
            } else if (items.length > 1) {
              e.preventDefault();
              items.splice(i, 1);
              rebuild();
              focusItem(Math.max(0, i - 1), 'end');
              scheduleBlockSave(b.id, getCurrent);
            }
          }
        },
      });
      inp.value = it.text || '';
      // 다음 frame에서 autoResize 실행 (DOM에 붙은 후)
      setTimeout(() => autoResize(inp), 0);
      row.append(
        el('span', { class: 'dde-bullet-marker' }, markers[i]),
        inp,
        el('button', { class: 'dde-bullet-x', type: 'button', title: '항목 삭제',
          onclick: () => {
            if (items.length <= 1) items[0] = { text: '', indent: 0 };
            else items.splice(i, 1);
            rebuild();
            scheduleBlockSave(b.id, getCurrent);
          }}, '✕'),
      );
      list.append(row);
    });
  }

  function focusItem(i, where) {
    const inputs = list.querySelectorAll('.dde-bullet-input');
    const t = inputs[i];
    if (!t) return;
    t.focus();
    if (where === 'end') t.setSelectionRange(t.value.length, t.value.length);
  }

  // 마커 선택 옵션 바
  const opts = el('div', { class: 'dde-bullet-opts' },
    el('span', { class: 'dde-bullet-opts-label' }, '마커:'));
  const markerOptions = [
    { v: 'bullet', icon: '•',  title: '점 (•)' },
    { v: 'dash',   icon: '–',  title: '대시 (–)' },
    { v: 'number', icon: '1)', title: '숫자 (1) 2) 3))' },
    { v: 'alpha',  icon: 'a)', title: '알파벳 (a) b) c))' },
  ];
  for (const m of markerOptions) {
    opts.append(el('button', {
      class: 'dde-marker-btn' + (marker === m.v ? ' active' : ''),
      type: 'button', title: m.title, 'data-v': m.v,
      onclick: () => {
        marker = m.v;
        opts.querySelectorAll('.dde-marker-btn').forEach(btn => {
          btn.classList.toggle('active', btn.dataset.v === m.v);
        });
        rebuild();
        scheduleBlockSave(b.id, getCurrent);
      },
    }, m.icon));
  }
  opts.append(el('span', { class: 'dde-bullet-hint' },
    'Tab: 들여쓰기 / Shift+Tab: 내어쓰기'));

  rebuild();

  const addBtn = el('button', {
    class: 'dde-bullet-add-link', type: 'button',
    onclick: () => {
      // 마지막 항목의 indent를 따라감
      const last = items[items.length - 1];
      items.push({ text: '', indent: last ? last.indent : 0 });
      rebuild();
      focusItem(items.length - 1);
      scheduleBlockSave(b.id, getCurrent);
    },
  }, '+ 항목 추가');

  body.append(opts, list, addBtn);
}

// ─── table (with column resize) ──────────────────────────────
function renderTable(body, b) {
  const c = b.content || { headers: ['항목', '내용'], rows: [['', '']], col_widths: [] };
  const headers = (c.headers || []).slice();
  const rows = (c.rows || []).map(r => r.slice());
  let colWidths = (c.col_widths && c.col_widths.length === headers.length)
                   ? c.col_widths.slice()
                   : new Array(headers.length).fill(0);

  const tblWrap = el('div', { class: 'dde-table-wrap' });

  function getCurrent() {
    // 저장 시점에, colWidths에 0(auto)인 컬럼이 있으면 실제 렌더링된 너비로 채움
    // 이렇게 해야 Word/PDF 출력 시 비율 계산이 망가지지 않음
    const liveTbl = tblWrap.querySelector('.dde-table');
    if (liveTbl) {
      const ths = liveTbl.querySelectorAll('thead th');
      for (let i = 0; i < headers.length; i++) {
        if (!colWidths[i] || colWidths[i] <= 0) {
          const th = ths[i];
          if (th) {
            const w = Math.round(th.getBoundingClientRect().width);
            if (w > 0) colWidths[i] = w;
          }
        }
      }
    }
    return {
      headers: headers.slice(),
      rows: rows.map(r => r.slice()),
      col_widths: colWidths.slice(),
    };
  }

  function rebuild() {
    tblWrap.innerHTML = '';
    const tbl = el('table', { class: 'dde-table' });

    const cg = el('colgroup');
    headers.forEach((_, ci) => {
      const w = colWidths[ci] || 0;
      const col = el('col');
      if (w > 0) col.style.width = w + 'px';
      cg.append(col);
    });
    cg.append(el('col', { class: 'dde-tbl-ctrl-col-c' }));
    tbl.append(cg);

    const thead = el('thead');
    const trh = el('tr');
    headers.forEach((h, ci) => {
      const th = el('th');
      const inp = el('input', {
        type: 'text', class: 'dde-cell-input', value: h, placeholder: '헤더',
        oninput: (e) => { headers[ci] = e.target.value; scheduleBlockSave(b.id, getCurrent); },
        onpaste: (e) => handleTablePaste(e, -1, ci),
      });
      const delBtn = el('button', {
        class: 'dde-col-del', type: 'button', title: '열 삭제',
        onclick: () => {
          if (headers.length <= 1) { alert('최소 1개 열이 필요합니다.'); return; }
          headers.splice(ci, 1);
          rows.forEach(r => r.splice(ci, 1));
          colWidths.splice(ci, 1);
          rebuild();
          scheduleBlockSave(b.id, getCurrent);
        }}, '✕');
      th.append(inp, delBtn);

      // 컬럼 리사이즈 핸들 — 모든 컬럼에 (마지막 포함) 표시
      // 마지막 컬럼은 실제 픽셀 너비를 측정해두기만 하고, 드래그는 안 되게
      if (ci < headers.length - 1) {
        const handle = el('div', {
          class: 'dde-col-resize',
          onmousedown: (e) => startColResize(e, ci, tbl, () => scheduleBlockSave(b.id, getCurrent)),
        });
        th.append(handle);
      }
      trh.append(th);
    });
    trh.append(el('th', { class: 'dde-tbl-ctrl-col' }));
    thead.append(trh);
    tbl.append(thead);

    const tbody = el('tbody');
    rows.forEach((row, ri) => {
      const tr = el('tr');
      row.forEach((cell, ci) => {
        const td = el('td');
        const ta = el('textarea', {
          class: 'dde-cell-textarea', rows: 1, placeholder: '',
          oninput: (e) => {
            rows[ri][ci] = e.target.value;
            autoResize(e.target);
            scheduleBlockSave(b.id, getCurrent);
          },
          onpaste: (e) => handleTablePaste(e, ri, ci),
        });
        ta.value = cell;
        setTimeout(() => autoResize(ta), 0);
        td.append(ta);
        tr.append(td);
      });
      tr.append(el('td', { class: 'dde-tbl-ctrl-col' },
        el('button', { class: 'dde-row-del', type: 'button', title: '행 삭제',
          onclick: () => {
            if (rows.length <= 1) { alert('최소 1개 행이 필요합니다.'); return; }
            rows.splice(ri, 1);
            rebuild();
            scheduleBlockSave(b.id, getCurrent);
          }}, '✕')));
      tbody.append(tr);
    });
    tbl.append(tbody);
    tblWrap.append(tbl);
  }

  function startColResize(ev, colIndex, tbl, onDone) {
    ev.preventDefault();
    const cols = tbl.querySelectorAll('colgroup col');
    const startX = ev.clientX;
    const allHeaderCells = tbl.querySelectorAll('thead th');
    const startW = allHeaderCells[colIndex].getBoundingClientRect().width;
    document.body.classList.add('dde-col-resizing');

    function onMove(mv) {
      const dx = mv.clientX - startX;
      const newW = Math.max(50, startW + dx);
      cols[colIndex].style.width = newW + 'px';
      colWidths[colIndex] = newW;
    }
    function onUp() {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      document.body.classList.remove('dde-col-resizing');
      onDone();
    }
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  }

  // ─── 엑셀/구글시트 표 붙여넣기 핸들러 ───
  // 클립보드의 TSV(탭+개행)나 다중 셀 텍스트를 감지해서
  // 표를 자동으로 확장하고 데이터 채워넣음.
  //
  // ri = -1 이면 헤더 행, 0+ 이면 본문 행
  function handleTablePaste(ev, ri, ci) {
    const cd = ev.clipboardData || window.clipboardData;
    if (!cd) return;
    // HTML 안에 <table>이 있으면 그걸 우선 파싱 (포맷 보존 더 좋음)
    const html = cd.getData('text/html');
    const txt  = cd.getData('text/plain') || '';

    let grid = null;
    if (html && /<t(able|r|d|h)\b/i.test(html)) {
      grid = parseHtmlTable(html);
    }
    if (!grid || grid.length === 0) {
      grid = parseTsv(txt);
    }
    if (!grid) return;

    // 단일 셀(한 행 한 열)이면 기본 동작 유지 (그냥 텍스트로 붙여넣기)
    if (grid.length === 1 && grid[0].length === 1) return;

    ev.preventDefault();

    // 사용자가 표 형태를 인지 못 할 수도 있으니 첫 사용 시에만 확인
    const rowsCount = grid.length;
    const colsCount = Math.max(...grid.map(r => r.length));
    const where = ri === -1
      ? `헤더 [${ci+1}열]부터`
      : `[${ri+1}행, ${ci+1}열]부터`;
    if (!confirm(
      `표 형식의 데이터를 감지했습니다 (${rowsCount}행 × ${colsCount}열).\n\n` +
      `${where} 자동으로 표를 확장하여 채워넣을까요?\n\n` +
      `· '예'(확인): 표 확장 + 데이터 채우기\n` +
      `· '아니오'(취소): 일반 텍스트로 현재 셀에만 붙여넣기`
    )) {
      // 취소 → 현재 셀에 plain text만 삽입
      insertPlainTextAtFocused(ev.target, txt);
      return;
    }

    // ─── 표 확장 ───
    if (ri === -1) {
      // 헤더에서 시작: 첫 그리드 행 → 헤더, 나머지 → 본문 시작 (0행부터)
      const newColsNeeded = ci + colsCount;
      while (headers.length < newColsNeeded) {
        headers.push('');
        rows.forEach(r => r.push(''));
        colWidths.push(0);
      }
      // 헤더 행 적용
      for (let cj = 0; cj < grid[0].length; cj++) {
        headers[ci + cj] = grid[0][cj];
      }
      // 본문 행 적용 (그리드 2번째 줄부터)
      const bodyGrid = grid.slice(1);
      while (rows.length < bodyGrid.length) {
        rows.push(headers.map(() => ''));
      }
      bodyGrid.forEach((gr, gi) => {
        for (let cj = 0; cj < gr.length; cj++) {
          rows[gi][ci + cj] = gr[cj];
        }
      });
    } else {
      // 본문 셀에서 시작
      const newColsNeeded = ci + colsCount;
      while (headers.length < newColsNeeded) {
        headers.push('');
        rows.forEach(r => r.push(''));
        colWidths.push(0);
      }
      const newRowsNeeded = ri + rowsCount;
      while (rows.length < newRowsNeeded) {
        rows.push(headers.map(() => ''));
      }
      grid.forEach((gr, gi) => {
        for (let cj = 0; cj < gr.length; cj++) {
          rows[ri + gi][ci + cj] = gr[cj];
        }
      });
    }

    rebuild();
    scheduleBlockSave(b.id, getCurrent);
    setSaveStatus(`표 데이터 ${rowsCount}×${colsCount} 붙여넣기 완료`, 'ok');
  }

  rebuild();

  const ctrls = el('div', { class: 'dde-table-ctrls' },
    el('button', { class: 'btn btn-outline btn-sm', type: 'button',
      onclick: () => { rows.push(headers.map(() => '')); rebuild(); scheduleBlockSave(b.id, getCurrent); }},
      '+ 행 추가'),
    el('button', { class: 'btn btn-outline btn-sm', type: 'button',
      onclick: () => {
        headers.push('');
        rows.forEach(r => r.push(''));
        colWidths.push(0);
        rebuild();
        scheduleBlockSave(b.id, getCurrent);
      }}, '+ 열 추가'),
    el('span', { class: 'dde-table-hint' },
      '💡 Excel/구글시트에서 표를 복사 후 셀에 붙여넣으면 자동 확장 · 열 경계선 드래그로 너비 조정'),
  );

  body.append(tblWrap, ctrls);
}

// ─── image gallery ───────────────────────────────────────────
function normalizeImageContent(content) {
  if (!content) return { images: [], columns: 2 };
  if (Array.isArray(content.images)) {
    return { images: content.images.slice(), columns: content.columns || 2 };
  }
  // 옛 단일 이미지 포맷
  if (content.url) {
    return {
      images: [{ filename: content.filename, url: content.url, caption: content.caption || '' }],
      columns: 1,
    };
  }
  return { images: [], columns: 2 };
}

function renderImageGallery(body, b) {
  const data = normalizeImageContent(b.content);
  let images = data.images.slice();
  let columns = data.columns;

  const wrap = el('div', { class: 'dde-image-block' });

  function getCurrent() {
    return { images: images.slice(), columns };
  }

  function rebuild() {
    wrap.innerHTML = '';

    const opts = el('div', { class: 'dde-img-opts' },
      el('span', { class: 'dde-img-opts-label' }, '열 수:'));
    [1, 2, 3, 4].forEach(n => {
      opts.append(el('button', {
        class: 'dde-col-btn' + (n === columns ? ' active' : ''),
        type: 'button',
        onclick: () => { columns = n; rebuild(); scheduleBlockSave(b.id, getCurrent); },
      }, n));
    });
    opts.append(
      el('span', { class: 'flex-spacer' }),
      el('button', {
        class: 'btn btn-outline btn-sm', type: 'button',
        onclick: () => triggerAddImages(),
      }, '+ 사진 추가'),
    );
    wrap.append(opts);

    if (images.length === 0) {
      wrap.append(el('div', {
        class: 'dde-image-drop',
        onclick: () => triggerAddImages(),
      },
        el('div', { class: 'dde-image-drop-icon' }, '📷'),
        el('div', { class: 'dde-image-drop-text' }, '클릭해서 사진 추가'),
        el('div', { class: 'dde-image-drop-hint' }, '여러 장 한번에 선택 가능 (Ctrl/Shift)'),
      ));
      return;
    }

    const grid = el('div', { class: 'dde-img-grid',
      style: `grid-template-columns: repeat(${columns}, 1fr);` });

    images.forEach((img, idx) => {
      const cell = el('div', { class: 'dde-img-cell' });
      const cellInner = el('div', { class: 'dde-img-cell-inner' });

      cellInner.append(
        el('img', { class: 'dde-img-thumb', src: img.url, alt: img.caption || '' }),
        el('div', { class: 'dde-img-ctrl' },
          el('button', { class: 'dde-img-mv', type: 'button', title: '왼쪽으로',
            disabled: idx === 0,
            onclick: () => {
              [images[idx - 1], images[idx]] = [images[idx], images[idx - 1]];
              rebuild();
              scheduleBlockSave(b.id, getCurrent);
            }}, '◀'),
          el('button', { class: 'dde-img-mv', type: 'button', title: '오른쪽으로',
            disabled: idx === images.length - 1,
            onclick: () => {
              [images[idx], images[idx + 1]] = [images[idx + 1], images[idx]];
              rebuild();
              scheduleBlockSave(b.id, getCurrent);
            }}, '▶'),
          el('button', { class: 'dde-img-x', type: 'button', title: '제거',
            onclick: () => {
              if (!confirm('이 사진을 제거하시겠습니까?')) return;
              images.splice(idx, 1);
              rebuild();
              scheduleBlockSave(b.id, getCurrent);
            }}, '✕'),
        ),
      );
      cell.append(cellInner);
      cell.append(el('input', {
        type: 'text', class: 'dde-img-caption-inp',
        placeholder: '캡션 (선택)...',
        value: img.caption || '',
        oninput: (e) => {
          images[idx].caption = e.target.value;
          scheduleBlockSave(b.id, getCurrent);
        },
      }));
      grid.append(cell);
    });
    wrap.append(grid);
  }

  async function triggerAddImages() {
    const inp = $('#dde-img-input');
    const onChange = async () => {
      inp.removeEventListener('change', onChange);
      const files = [...(inp.files || [])];
      inp.value = '';
      if (!files.length) return;
      try {
        let totalOrig = 0, totalFinal = 0;
        let done = 0;
        for (const f of files) {
          setSaveStatus(`사진 압축·업로드 중 (${done + 1}/${files.length})...`, 'busy');
          const fd = new FormData();
          fd.append('file', f);
          const res = await api(`/api/dock-reports/${E.reportId}/upload-image`, {
            method: 'POST', body: fd,
          });
          images.push({ filename: res.filename, url: res.url, caption: '' });
          totalOrig  += res.original_kb || 0;
          totalFinal += res.final_kb || 0;
          done += 1;
        }
        await api(`/api/dock-blocks/${b.id}`, {
          method: 'PUT', body: JSON.stringify({ content: getCurrent() }),
        });
        rebuild();
        // 압축 결과 노출
        if (totalOrig > 0) {
          const pct = Math.round((1 - totalFinal / totalOrig) * 100);
          const origMb  = (totalOrig  / 1024).toFixed(1);
          const finalMb = (totalFinal / 1024).toFixed(1);
          setSaveStatus(`저장됨 (${origMb}MB → ${finalMb}MB, ${pct}% 절감)`, 'ok');
        } else {
          setSaveStatus('저장됨', 'ok');
        }
      } catch (e) {
        setSaveStatus('업로드 실패: ' + e.message, 'err');
        alert('이미지 업로드 실패: ' + e.message);
      }
    };
    inp.addEventListener('change', onChange);
    inp.click();
  }

  rebuild();
  body.append(wrap);
}

// ─────────────────────────────────────────────────────────────
//  Save / Block actions
// ─────────────────────────────────────────────────────────────
function scheduleBlockSave(blockId, getContent) {
  clearTimeout(E.saveTimer);
  setSaveStatus('저장 대기...', 'busy');
  E.saveTimer = setTimeout(async () => {
    setSaveStatus('저장 중...', 'busy');
    try {
      await api(`/api/dock-blocks/${blockId}`, {
        method: 'PUT', body: JSON.stringify({ content: getContent() }),
      });
      const info = E.byId.get(E.activeSecId);
      if (info) {
        const target = (info.section.blocks || []).find(b => b.id === blockId);
        if (target) target.content = getContent();
      }
      setSaveStatus('저장됨', 'ok');
    } catch (e) {
      setSaveStatus('저장 실패: ' + e.message, 'err');
    }
  }, 500);
}

async function addBlockAt(blockType, position) {
  if (!E.activeSecId) return;
  try {
    setSaveStatus('블록 추가 중...', 'busy');
    const res = await api(`/api/dock-sections/${E.activeSecId}/blocks`, {
      method: 'POST', body: JSON.stringify({ block_type: blockType }),
    });
    // 새 블록은 맨 뒤에 생성되므로 position까지 ↑ 이동
    const info = E.byId.get(E.activeSecId);
    const currentCount = (info?.section.blocks || []).length;   // 추가 전 개수
    const newBlockIdx = currentCount;                            // 추가 후 마지막 인덱스
    const movesUp = newBlockIdx - position;
    for (let i = 0; i < movesUp; i++) {
      await api(`/api/dock-blocks/${res.id}/move`, {
        method: 'POST', body: JSON.stringify({ direction: 'up' }),
      });
    }
    await loadReport();
    setSaveStatus('저장됨', 'ok');
  } catch (e) {
    setSaveStatus('블록 추가 실패: ' + e.message, 'err');
  }
}

async function deleteBlock(bid) {
  if (!confirm('이 블록을 삭제하시겠습니까?')) return;
  try {
    await api(`/api/dock-blocks/${bid}`, { method: 'DELETE' });
    const info = E.byId.get(E.activeSecId);
    if (info && info.section.blocks) {
      info.section.blocks = info.section.blocks.filter(b => b.id !== bid);
    }
    renderEditor();
    setSaveStatus('저장됨', 'ok');
  } catch (e) { alert('삭제 실패: ' + e.message); }
}

async function moveBlock(bid, direction) {
  try {
    await api(`/api/dock-blocks/${bid}/move`, {
      method: 'POST', body: JSON.stringify({ direction }),
    });
    await loadReport();
  } catch (e) { alert('순서 변경 실패: ' + e.message); }
}

// ─────────────────────────────────────────────────────────────
//  Events
// ─────────────────────────────────────────────────────────────
function bindEvents() {
  $('#dde-btn-add-section').addEventListener('click', () => addSection(null));
  $('#dde-btn-add-sub').addEventListener('click', () => {
    if (!E.activeSecId) return;
    addSection(E.activeSecId);
  });

  // 일괄 추가
  $('#dde-btn-bulk-add').addEventListener('click', openBulkAddDialog);
  $('#dde-bulk-apply').addEventListener('click', applyBulkAdd);
  $('#dde-bulk-modal').addEventListener('click', (ev) => {
    if (ev.target.dataset.close === '1') closeBulkAddDialog();
  });
  // Esc로 모달 닫기
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !$('#dde-bulk-modal').hidden) closeBulkAddDialog();
  });
  // textarea에서 Tab 키 → 실제 탭 문자 삽입 (포커스 이동 방지)
  $('#dde-bulk-text').addEventListener('keydown', (e) => {
    if (e.key === 'Tab') {
      e.preventDefault();
      const ta = e.target;
      const s = ta.selectionStart, t = ta.selectionEnd;
      ta.value = ta.value.slice(0, s) + '\t' + ta.value.slice(t);
      ta.selectionStart = ta.selectionEnd = s + 1;
    }
  });

  let titleTimer;
  $('#dde-section-title').addEventListener('input', (e) => {
    if (!E.activeSecId) return;
    clearTimeout(titleTimer);
    setSaveStatus('저장 대기...', 'busy');
    titleTimer = setTimeout(() => saveSectionTitle(E.activeSecId, e.target.value), 500);
  });

  $('#dde-btn-del-section').addEventListener('click', () => {
    if (E.activeSecId) deleteSection(E.activeSecId);
  });

  // 다중 이미지 선택 허용
  $('#dde-img-input').setAttribute('multiple', '');

  $('#dde-btn-export-docx').addEventListener('click', () => {
    setSaveStatus('Word 생성 중...', 'busy');
    // 새 탭에서 다운로드 시작 (이 탭은 유지)
    const url = `/api/dock-reports/${E.reportId}/export/docx`;
    // 다운로드는 같은 탭에서 — Content-Disposition: attachment 이므로 페이지 이동 안 됨
    window.location = url;
    setTimeout(() => setSaveStatus('저장됨', 'ok'), 1500);
  });
  $('#dde-btn-export-pdf').addEventListener('click', () => {
    setSaveStatus('PDF 변환 중... (10~20초 소요)', 'busy');
    const url = `/api/dock-reports/${E.reportId}/export/pdf`;
    window.location = url;
    setTimeout(() => setSaveStatus('저장됨', 'ok'), 3000);
  });
}

init();
