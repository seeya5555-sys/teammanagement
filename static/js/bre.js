// ════════════════════════════════════════════════════════════════
//  Boarding Report Editor — Step 2
//  · Dry Dock 편집기 구조 그대로 + 신규 블록 2종:
//    - info_table  (Label-Value 쌍 표; 방선보고서 헤더용)
//    - defect_table (Defect List 표; 사진 + 발견사항 + 조치 + Risk Level)
//  · 권한 시스템, 일괄 섹션 추가, Tab 들여쓰기, 컬럼 리사이즈, 이미지 갤러리 등 모두 동일
// ════════════════════════════════════════════════════════════════
const E = {
  reportId: window.BRE_REPORT_ID,
  report: null,
  sectionsFlat: [],
  tree: [],
  byId: new Map(),
  activeSecId: null,
  saveTimer: null,
  canEdit: true,
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
  const s = $('#bre-save-status');
  s.textContent = text;
  s.className = 'dde-save-status' + (kind ? ' dde-save-' + kind : '');
}

// ─────────────────────────────────────────────────────────────
async function init() {
  try {
    await loadReport();
    bindEvents();
  } catch (e) {
    alert('보고서 로드 실패: ' + e.message);
    window.location = '/boarding';
  }
}

async function loadReport() {
  const r = await api(`/api/boarding-reports/${E.reportId}`);
  E.report = r;
  E.sectionsFlat = r.sections || [];
  E.canEdit = !!r.can_edit;

  $('#bre-title').textContent = r.title || '제목 없음';
  const subs = [];
  if (r.vessel_name) subs.push(r.vessel_name);
  if (r.port)        subs.push('🏭 ' + r.port);
  if (r.boarding_start || r.boarding_end) {
    subs.push(`${(r.boarding_start||'').replace(/-/g,'.')} ~ ${(r.boarding_end||'').replace(/-/g,'.')}`);
  }
  $('#bre-subtitle').textContent = subs.join('   ·   ');

  document.body.classList.toggle('dde-readonly', !E.canEdit);
  const ro = $('#bre-readonly-banner');
  if (ro) ro.hidden = E.canEdit;

  ['bre-btn-add-section','bre-btn-add-sub','bre-btn-bulk-add','bre-btn-del-section']
    .forEach(id => { const b = $('#' + id); if (b) b.style.display = E.canEdit ? '' : 'none'; });
  const titleInp = $('#bre-section-title');
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

// ─── TOC ─────────────────────────────────────────────────────
function renderTOC() {
  const root = $('#bre-toc');
  root.innerHTML = '';
  if (E.tree.length === 0) {
    root.append(el('div', { class: 'dde-toc-empty' },
      '아직 섹션이 없습니다.', el('br'), '+ 섹션 버튼으로 시작하세요.'));
  } else {
    renderTOCNodes(E.tree, root);
  }
  const subBtn = $('#bre-btn-add-sub');
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
      oncontextmenu: E.canEdit ? (ev) => {
        ev.preventDefault();
        showTocCtxMenu(ev, n.id);
      } : null,
    });
    item.append(
      el('span', { class: 'dde-toc-no' }, info.number + '.'),
      el('span', { class: 'dde-toc-title' }, n.title || '(제목 없음)'),
      E.canEdit ? el('div', { class: 'dde-toc-actions' },
        el('button', { class: 'dde-toc-btn', title: '위로',
          onclick: (e) => { e.stopPropagation(); moveSection(n.id, 'up'); }}, '↑'),
        el('button', { class: 'dde-toc-btn', title: '아래로',
          onclick: (e) => { e.stopPropagation(); moveSection(n.id, 'down'); }}, '↓'),
        el('button', { class: 'dde-toc-btn', title: '다른 섹션으로 이동…',
          onclick: (e) => { e.stopPropagation(); openReparentModal(n.id); }}, '↗'),
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

function showTocCtxMenu(ev, sid) {
  document.querySelectorAll('.dde-toc-ctx-menu').forEach(m => m.remove());
  const menu = el('div', { class: 'dde-table-ctx-menu dde-toc-ctx-menu' });
  const info = E.byId.get(sid);
  const addItem = (label, fn, opts = {}) => {
    menu.append(el('button', {
      class: 'dde-ctx-item' + (opts.disabled ? ' disabled' : ''),
      type: 'button', disabled: opts.disabled,
      onclick: () => { menu.remove(); fn(); },
    }, label));
  };
  const addSep = () => menu.append(el('div', { class: 'dde-ctx-sep' }));

  addItem('↑ 위로', () => moveSection(sid, 'up'));
  addItem('↓ 아래로', () => moveSection(sid, 'down'));
  addSep();
  addItem('↗ 다른 섹션으로 이동…', () => openReparentModal(sid));
  if (info && info.depth > 0) {
    addItem('⤴ 최상위로 이동', () => reparentSection(sid, null));
  }
  addSep();
  addItem('이름 변경', () => renameSection(sid));
  addItem('🗑 삭제', () => deleteSection(sid));

  document.body.append(menu);
  menu.style.position = 'fixed';
  menu.style.top  = (ev.clientY + 4) + 'px';
  menu.style.left = (ev.clientX + 4) + 'px';
  setTimeout(() => {
    const onDoc = (e) => {
      if (!menu.contains(e.target)) {
        menu.remove();
        document.removeEventListener('click', onDoc);
      }
    };
    document.addEventListener('click', onDoc);
  }, 0);
}

function openReparentModal(sid) {
  const info = E.byId.get(sid);
  if (!info) return;

  const descendants = new Set();
  (function collect(node) {
    descendants.add(node.id);
    (node.children || []).forEach(collect);
  })(info.section);

  let maxRelDepth = 0;
  (function md(node, d) {
    maxRelDepth = Math.max(maxRelDepth, d);
    (node.children || []).forEach(c => md(c, d + 1));
  })(info.section, 0);

  const maxAllowedDepth = 2 - maxRelDepth;
  const candidates = [];
  if (info.section.parent_id != null && maxAllowedDepth >= 0) {
    candidates.push({ id: null, label: '— 최상위 (depth 0) —', depth: -1 });
  }
  for (const [id, item] of E.byId.entries()) {
    if (descendants.has(id)) continue;
    if (item.depth > maxAllowedDepth) continue;
    if (id === info.section.parent_id) continue;
    candidates.push({
      id,
      label: `${'  '.repeat(item.depth)}${item.number}. ${item.section.title || '(제목 없음)'}`,
      depth: item.depth,
    });
  }

  if (candidates.length === 0) {
    alert('이동할 수 있는 부모 섹션이 없습니다.\n(자기 자신과 자손은 제외됩니다.)');
    return;
  }

  const backdrop = el('div', { class: 'dde-modal-backdrop' });
  const dialog = el('div', { class: 'dde-modal' });
  dialog.append(
    el('div', { class: 'dde-modal-title' },
      `"${info.section.title}" 을(를) 이동할 위치 선택`),
    el('div', { class: 'dde-modal-hint' },
      '아래에서 새 부모 섹션을 선택하세요. 자기 자신과 자손은 표시되지 않습니다.'),
  );

  const list = el('div', { class: 'dde-modal-list' });
  let selected;
  candidates.forEach(c => {
    const opt = el('div', {
      class: 'dde-modal-option',
      onclick: () => {
        list.querySelectorAll('.dde-modal-option.selected').forEach(o =>
          o.classList.remove('selected'));
        opt.classList.add('selected');
        selected = c.id;
      },
    }, c.label);
    list.append(opt);
  });
  dialog.append(list);

  const actions = el('div', { class: 'dde-modal-actions' },
    el('button', {
      class: 'btn btn-outline',
      onclick: () => backdrop.remove(),
    }, '취소'),
    el('button', {
      class: 'btn btn-primary',
      onclick: async () => {
        if (selected === undefined) {
          alert('이동할 위치를 선택하세요.');
          return;
        }
        backdrop.remove();
        await reparentSection(sid, selected);
      },
    }, '이동'),
  );
  dialog.append(actions);

  backdrop.append(dialog);
  backdrop.addEventListener('click', (e) => {
    if (e.target === backdrop) backdrop.remove();
  });
  document.body.append(backdrop);
}

async function reparentSection(sid, newParentId) {
  try {
    await api(`/api/boarding-sections/${sid}/reparent`, {
      method: 'POST',
      body: JSON.stringify({ new_parent_id: newParentId }),
    });
    await loadReport();
    setSaveStatus('이동 완료', 'ok');
  } catch (e) {
    alert('이동 실패: ' + e.message);
  }
}

async function renameSection(sid) {
  const info = E.byId.get(sid);
  if (!info) return;
  const newTitle = prompt('새 제목:', info.section.title);
  if (newTitle === null) return;
  const t = newTitle.trim();
  if (!t) return;
  try {
    await api(`/api/boarding-sections/${sid}`, {
      method: 'PUT',
      body: JSON.stringify({ title: t }),
    });
    await loadReport();
  } catch (e) { alert('이름 변경 실패: ' + e.message); }
}

async function addSection(parentId = null) {
  const title = prompt(parentId ? '새 하위 섹션 제목:' : '새 섹션 제목:', '');
  if (title === null) return;
  const t = title.trim() || '새 섹션';
  try {
    const r = await api(`/api/boarding-reports/${E.reportId}/sections`, {
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
    await api(`/api/boarding-sections/${sid}`, { method: 'DELETE' });
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
    await api(`/api/boarding-sections/${sid}/move`, {
      method: 'POST', body: JSON.stringify({ direction }),
    });
    await loadReport();
  } catch (e) { alert('순서 변경 실패: ' + e.message); }
}

// ─── 일괄 추가 ───────────────────────────────────────────────
function parseBulkText(text) {
  const out = [];
  const lines = text.split(/\r?\n/);
  for (const raw of lines) {
    if (!raw.trim()) continue;
    if (raw.trim().startsWith('#')) continue;
    let indent = 0;
    let i = 0;
    while (i < raw.length) {
      if (raw[i] === '\t') { indent += 1; i += 1; }
      else if (raw[i] === ' ') {
        let sp = 0;
        while (i < raw.length && raw[i] === ' ' && sp < 4) { sp++; i++; }
        if (sp === 4) indent += 1;
        else break;
      } else break;
    }
    indent = Math.min(2, indent);
    const title = raw.slice(i).trim();
    if (!title) continue;
    out.push({ indent, title });
  }
  return out;
}

function openBulkAddDialog() {
  const m = $('#bre-bulk-modal');
  $('#bre-bulk-text').value = '';
  const underRadio = $('input[name="bre-bulk-target"][value="under"]');
  const underLabel = $('#bre-bulk-under-label');
  const curTitle = $('#bre-bulk-current-title');
  const info = E.activeSecId ? E.byId.get(E.activeSecId) : null;
  if (info && info.depth < 2) {
    underRadio.disabled = false;
    underLabel.style.opacity = '1';
    curTitle.textContent = `"${info.section.title}"`;
  } else {
    underRadio.disabled = true;
    underLabel.style.opacity = '0.4';
    curTitle.textContent = '—';
  }
  $('input[name="bre-bulk-target"][value="root"]').checked = true;
  m.hidden = false;
  document.body.classList.add('modal-open');
  setTimeout(() => $('#bre-bulk-text').focus(), 50);
}

function closeBulkAddDialog() {
  $('#bre-bulk-modal').hidden = true;
  document.body.classList.remove('modal-open');
}

async function applyBulkAdd() {
  const text = $('#bre-bulk-text').value;
  const parsed = parseBulkText(text);
  if (parsed.length === 0) { alert('추가할 섹션이 없습니다.'); return; }
  if (parsed[0].indent > 0) parsed[0].indent = 0;

  const targetMode = document.querySelector('input[name="bre-bulk-target"]:checked').value;
  let basePid = null;
  let baseDepth = 0;
  if (targetMode === 'under' && E.activeSecId) {
    const info = E.byId.get(E.activeSecId);
    if (info && info.depth < 2) {
      basePid = E.activeSecId;
      baseDepth = info.depth + 1;
    }
  }

  if (parsed.some(p => baseDepth + p.indent > 2)) {
    alert('최대 3단계까지만 추가할 수 있습니다.');
    return;
  }

  const btn = $('#bre-bulk-apply');
  btn.disabled = true;
  btn.textContent = `추가 중... (0/${parsed.length})`;

  const parents = [basePid, null, null];

  try {
    let done = 0;
    for (const item of parsed) {
      const lv = item.indent;
      const parentId = lv === 0 ? basePid : parents[lv - 1];
      const r = await api(`/api/boarding-reports/${E.reportId}/sections`, {
        method: 'POST',
        body: JSON.stringify({ title: item.title, parent_id: parentId }),
      });
      parents[lv] = r.id;
      for (let i = lv + 1; i < parents.length; i++) parents[i] = null;
      done += 1;
      btn.textContent = `추가 중... (${done}/${parsed.length})`;
    }
    closeBulkAddDialog();
    await loadReport();
    setSaveStatus(`섹션 ${parsed.length}개 추가됨`, 'ok');
  } catch (e) {
    alert('일괄 추가 중 오류: ' + e.message);
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
    await api(`/api/boarding-sections/${sid}`, {
      method: 'PUT', body: JSON.stringify({ title: title.trim() }),
    });
    const info = E.byId.get(sid);
    if (info) info.section.title = title.trim();
    renderTOC();
    setSaveStatus('저장됨', 'ok');
  } catch (e) { setSaveStatus('저장 실패: ' + e.message, 'err'); }
}

// ─── Editor (우측) ───────────────────────────────────────────
function renderEditor() {
  const empty = $('#bre-main-empty');
  const editor = $('#bre-section-edit');
  if (!E.activeSecId || !E.byId.has(E.activeSecId)) {
    empty.hidden = false;
    editor.hidden = true;
    return;
  }
  const info = E.byId.get(E.activeSecId);
  const sec = info.section;

  empty.hidden = true;
  editor.hidden = false;
  $('#bre-section-no').textContent = info.number + '.';
  $('#bre-section-title').value = sec.title || '';

  const blocksWrap = $('#bre-blocks');
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

  if (E.canEdit) blocksWrap.append(renderInserter(0));
  blocks.forEach((b, idx) => {
    blocksWrap.append(renderBlock(b, idx, blocks.length));
    if (E.canEdit && idx < blocks.length - 1) {
      blocksWrap.append(renderInserter(idx + 1));
    }
  });
  if (E.canEdit) blocksWrap.append(renderTailAdder(blocks.length));
}

// 빈 섹션 - 6종 블록 큰 버튼 (info_table, defect_table 추가)
function renderEmptyInserter() {
  const wrap = el('div', { class: 'dde-empty-add' });
  wrap.append(el('div', { class: 'dde-empty-add-hint' },
    '이 섹션에 추가할 블록 종류를 선택하세요'));
  const grid = el('div', { class: 'dde-empty-add-grid bre-add-grid' });
  for (const it of getBlockMenu()) {
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

function getBlockMenu() {
  return [
    { type: 'paragraph',    icon: 'T',  label: '텍스트',      desc: '단락 본문' },
    { type: 'bullet_list',  icon: '•',  label: '불릿 리스트', desc: '항목 나열' },
    { type: 'table',        icon: '▦',  label: '표',          desc: '행/열 데이터' },
    { type: 'image',        icon: '🖼', label: '사진 갤러리', desc: '여러 장' },
    { type: 'info_table',   icon: '📋', label: '정보 표',     desc: 'Label-Value' },
    { type: 'defect_table', icon: '⚠️', label: 'Defect List', desc: '결함 항목 표' },
  ];
}

function renderInserter(position) {
  const ins = el('div', { class: 'dde-inserter' });
  const btn = el('button', {
    class: 'dde-inserter-btn', type: 'button', title: '여기에 블록 추가',
    onclick: (e) => { e.stopPropagation(); showInsertMenu(btn, position); },
  }, '+');
  ins.append(btn);
  return ins;
}

function showInsertMenu(anchor, position) {
  $$('.dde-insert-menu').forEach(m => m.remove());
  const menu = el('div', { class: 'dde-insert-menu' });
  for (const it of getBlockMenu()) {
    menu.append(el('button', {
      class: 'dde-insert-item', type: 'button',
      onclick: () => { menu.remove(); addBlockAt(it.type, position); },
    }, el('span', { class: 'dde-insert-icon' }, it.icon),
       el('span', {}, it.label)));
  }
  document.body.append(menu);
  const r = anchor.getBoundingClientRect();
  const menuWidth = 220;
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

function renderTailAdder(position) {
  const wrap = el('div', { class: 'dde-tail-add' });
  wrap.append(el('span', { class: 'dde-tail-add-label' }, '+ 블록 추가:'));
  for (const it of getBlockMenu()) {
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

function renderBlock(b, idx, total) {
  const wrap = el('div', { class: `dde-block dde-block-${b.block_type}`, 'data-id': b.id });
  if (E.canEdit) {
    wrap.append(el('div', { class: 'dde-block-controls' },
      el('button', { class: 'dde-block-btn', title: '위로', disabled: idx === 0,
        onclick: () => moveBlock(b.id, 'up') }, '↑'),
      el('button', { class: 'dde-block-btn', title: '아래로', disabled: idx === total - 1,
        onclick: () => moveBlock(b.id, 'down') }, '↓'),
      el('button', { class: 'dde-block-btn dde-block-del', title: '삭제',
        onclick: () => deleteBlock(b.id) }, '✕'),
    ));
  }

  const body = el('div', { class: 'dde-block-body' });
  if (b.block_type === 'paragraph')          renderParagraph(body, b);
  else if (b.block_type === 'bullet_list')   renderBulletList(body, b);
  else if (b.block_type === 'table')         renderTable(body, b);
  else if (b.block_type === 'image')         renderImageGallery(body, b);
  else if (b.block_type === 'info_table')    renderInfoTable(body, b);
  else if (b.block_type === 'defect_table')  renderDefectTable(body, b);
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
  setTimeout(() => autoResize(ta), 0);
}

function autoResize(ta) {
  ta.style.height = 'auto';
  ta.style.height = Math.max(ta.scrollHeight + 2, 40) + 'px';
}

// ─── bullet_list (마커 4종 + 들여쓰기) ───────────────────────
const MAX_INDENT = 3;
const CIRCLED_NUMS = ['①','②','③','④','⑤','⑥','⑦','⑧','⑨','⑩',
                       '⑪','⑫','⑬','⑭','⑮','⑯','⑰','⑱','⑲','⑳'];

function numberMarkerByDepth(depth, n) {
  if (depth === 0) return `${n}.`;
  if (depth === 1) return `${n})`;
  if (depth === 2) return CIRCLED_NUMS[(n - 1) % CIRCLED_NUMS.length];
  return `${String.fromCharCode(96 + ((n - 1) % 26) + 1)})`;
}
function alphaMarkerByDepth(depth, n) {
  if (depth === 0) return `${String.fromCharCode(96 + ((n - 1) % 26) + 1)}.`;
  if (depth === 1) return `${String.fromCharCode(96 + ((n - 1) % 26) + 1)})`;
  if (depth === 2) return CIRCLED_NUMS[(n - 1) % CIRCLED_NUMS.length];
  return `${n})`;
}

function normalizeBulletItems(items) {
  return (items || []).map(it =>
    typeof it === 'string' ? { text: it, indent: 0 } : {
      text: it.text || '',
      indent: Math.max(0, Math.min(3, it.indent || 0)),
    });
}

function computeBulletMarkers(items, kind) {
  const markers = [];
  const counters = [0, 0, 0, 0];
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

  function focusItem(i, where) {
    const inputs = list.querySelectorAll('.dde-bullet-input');
    const t = inputs[i];
    if (!t) return;
    t.focus();
    if (where === 'end') t.setSelectionRange(t.value.length, t.value.length);
  }

  function rebuild() {
    list.innerHTML = '';
    const markers = computeBulletMarkers(items, marker);
    items.forEach((it, i) => {
      const row = el('div', {
        class: `dde-bullet-row dde-bullet-${marker} indent-${it.indent}`,
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
            e.preventDefault();
            if (e.shiftKey) {
              if (items[i].indent > 0) {
                items[i].indent -= 1;
                rebuild(); focusItem(i);
                scheduleBlockSave(b.id, getCurrent);
              }
            } else {
              if (items[i].indent < MAX_INDENT) {
                items[i].indent += 1;
                rebuild(); focusItem(i);
                scheduleBlockSave(b.id, getCurrent);
              }
            }
            return;
          }
          if (e.key === 'Enter' && !e.shiftKey) {
            // Enter: 새 항목 / Shift+Enter: 같은 항목 내 줄바꿈
            e.preventDefault();
            items.splice(i + 1, 0, { text: '', indent: items[i].indent });
            rebuild(); focusItem(i + 1);
            scheduleBlockSave(b.id, getCurrent);
            return;
          }
          if (e.key === 'Backspace' && !e.target.value) {
            if (items[i].indent > 0) {
              e.preventDefault();
              items[i].indent -= 1;
              rebuild(); focusItem(i);
              scheduleBlockSave(b.id, getCurrent);
            } else if (items.length > 1) {
              e.preventDefault();
              items.splice(i, 1);
              rebuild(); focusItem(Math.max(0, i - 1), 'end');
              scheduleBlockSave(b.id, getCurrent);
            }
          }
        },
      });
      inp.value = it.text || '';
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

  const opts = el('div', { class: 'dde-bullet-opts' },
    el('span', { class: 'dde-bullet-opts-label' }, '마커:'));
  const markerOptions = [
    { v: 'bullet', icon: '•',  title: '점 (•)' },
    { v: 'dash',   icon: '–',  title: '대시 (–)' },
    { v: 'number', icon: '1)', title: '숫자' },
    { v: 'alpha',  icon: 'a)', title: '알파벳' },
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
      const last = items[items.length - 1];
      items.push({ text: '', indent: last ? last.indent : 0 });
      rebuild(); focusItem(items.length - 1);
      scheduleBlockSave(b.id, getCurrent);
    },
  }, '+ 항목 추가');

  body.append(opts, list, addBtn);
}

// ─── table (기존 일반 표) ────────────────────────────────────
function renderTable(body, b) {
  const c = b.content || { headers: ['항목', '내용'], rows: [['', '']], col_widths: [] };
  const headers = (c.headers || []).slice();
  const rows = (c.rows || []).map(r => r.slice());
  let colWidths = (c.col_widths && c.col_widths.length === headers.length)
                   ? c.col_widths.slice()
                   : new Array(headers.length).fill(0);

  const tblWrap = el('div', { class: 'dde-table-wrap' });

  function getCurrent() {
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
      if (ci < headers.length - 1) {
        th.append(el('div', {
          class: 'dde-col-resize',
          onmousedown: (e) => startColResize(e, ci, tbl, () => scheduleBlockSave(b.id, getCurrent)),
        }));
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
    el('span', { class: 'dde-table-hint' }, '열 경계선을 드래그해서 너비 조정'),
  );
  body.append(tblWrap, ctrls);
}

// ─── image gallery ───────────────────────────────────────────
function normalizeImageContent(content) {
  if (!content) return { images: [], columns: 2 };
  if (Array.isArray(content.images)) {
    return { images: content.images.slice(), columns: content.columns || 2 };
  }
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

  function getCurrent() { return { images: images.slice(), columns }; }

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
        class: 'dde-image-drop', onclick: () => triggerAddImages(),
      },
        el('div', { class: 'dde-image-drop-icon' }, '📷'),
        el('div', { class: 'dde-image-drop-text' }, '클릭하거나 사진을 끌어다 놓기'),
        el('div', { class: 'dde-image-drop-hint' }, '여러 장 한번에 선택·드롭 가능'),
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
        placeholder: '캡션 (선택)...', value: img.caption || '',
        oninput: (e) => {
          images[idx].caption = e.target.value;
          scheduleBlockSave(b.id, getCurrent);
        },
      }));
      grid.append(cell);
    });
    wrap.append(grid);
  }

  // 파일 목록(File[])을 순차 압축·업로드 — 파일 선택과 드래그앤드롭이 공유
  async function uploadFiles(fileList) {
    const files = [...(fileList || [])].filter(f => f && (f.type || '').startsWith('image/'));
    if (!files.length) {
      setSaveStatus('이미지 파일만 추가할 수 있습니다', 'err');
      return;
    }
    try {
      let totalOrig = 0, totalFinal = 0;
      let done = 0;
      for (const f of files) {
        setSaveStatus(`사진 압축·업로드 중 (${done + 1}/${files.length})...`, 'busy');
        const fd = new FormData();
        fd.append('file', f);
        const res = await api(`/api/boarding-reports/${E.reportId}/upload-image`, {
          method: 'POST', body: fd,
        });
        images.push({ filename: res.filename, url: res.url, caption: '' });
        totalOrig  += res.original_kb || 0;
        totalFinal += res.final_kb || 0;
        done += 1;
      }
      await api(`/api/boarding-blocks/${b.id}`, {
        method: 'PUT', body: JSON.stringify({ content: getCurrent() }),
      });
      rebuild();
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
  }

  function triggerAddImages() {
    const inp = $('#bre-img-input');
    const onChange = async () => {
      inp.removeEventListener('change', onChange);
      const files = [...(inp.files || [])];
      inp.value = '';
      if (!files.length) return;   // 파일 선택 취소 시 조용히 종료
      await uploadFiles(files);
    };
    inp.addEventListener('change', onChange);
    inp.click();
  }

  // ── 드래그앤드롭: 갤러리 블록 전체를 드롭 영역으로 ──
  let dragDepth = 0;
  const dndHasFiles = (e) =>
    e.dataTransfer && Array.from(e.dataTransfer.types || []).includes('Files');
  wrap.addEventListener('dragenter', (e) => {
    if (!dndHasFiles(e)) return;
    e.preventDefault();
    dragDepth += 1;
    wrap.classList.add('dde-dnd-over');
  });
  wrap.addEventListener('dragover', (e) => {
    if (!dndHasFiles(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
  });
  wrap.addEventListener('dragleave', (e) => {
    if (!dndHasFiles(e)) return;
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) wrap.classList.remove('dde-dnd-over');
  });
  wrap.addEventListener('drop', (e) => {
    if (!e.dataTransfer) return;
    e.preventDefault();
    dragDepth = 0;
    wrap.classList.remove('dde-dnd-over');
    uploadFiles(e.dataTransfer.files);
  });

  installDndNavGuard();
  inp_setMultiple();
  rebuild();
  body.append(wrap);
}

// 갤러리 밖에 파일을 떨어뜨렸을 때 브라우저가 그 파일로 페이지를 벗어나
// 편집 중인 내용이 날아가는 것을 방지 (앱 전체에서 1회만 등록)
function installDndNavGuard() {
  if (window.__trmtDndNavGuard) return;
  window.__trmtDndNavGuard = true;
  ['dragover', 'drop'].forEach((evt) => {
    document.addEventListener(evt, (e) => {
      if (e.dataTransfer
          && Array.from(e.dataTransfer.types || []).includes('Files')
          && !(e.target.closest && e.target.closest('.dde-image-block'))) {
        e.preventDefault();
      }
    });
  });
}

function inp_setMultiple() {
  $('#bre-img-input').setAttribute('multiple', '');
}

// ─── 신규: info_table (Label-Value 표) ──────────────────────
function renderInfoTable(body, b) {
  const c = b.content || { rows: [] };
  let rows = (c.rows || []).map(r => ({ label: r.label || '', value: r.value || '' }));
  if (rows.length === 0) rows = [{ label: '', value: '' }];

  const wrap = el('div', { class: 'bre-info-table-wrap' });
  function getCurrent() {
    return { rows: rows.map(r => ({ label: r.label, value: r.value })) };
  }

  function rebuild() {
    wrap.innerHTML = '';
    const tbl = el('table', { class: 'bre-info-table' });
    const tbody = el('tbody');
    rows.forEach((r, i) => {
      const tr = el('tr');
      // Label 셀 (음영, 굵게)
      const tdL = el('td', { class: 'bre-info-label' });
      tdL.append(el('input', {
        type: 'text', class: 'bre-info-input',
        value: r.label, placeholder: '항목명 (예: Vessel)',
        oninput: (e) => { rows[i].label = e.target.value; scheduleBlockSave(b.id, getCurrent); },
      }));
      // Value 셀
      const tdV = el('td', { class: 'bre-info-value' });
      const ta = el('textarea', {
        class: 'bre-info-input bre-info-textarea', rows: 1, placeholder: '내용',
        oninput: (e) => {
          rows[i].value = e.target.value;
          autoResize(e.target);
          scheduleBlockSave(b.id, getCurrent);
        },
      });
      ta.value = r.value;
      setTimeout(() => autoResize(ta), 0);
      tdV.append(ta);

      // 행 삭제 버튼
      const tdC = el('td', { class: 'dde-tbl-ctrl-col' },
        el('button', { class: 'dde-row-del', type: 'button', title: '행 삭제',
          onclick: () => {
            if (rows.length <= 1) { rows[0] = { label: '', value: '' }; }
            else rows.splice(i, 1);
            rebuild();
            scheduleBlockSave(b.id, getCurrent);
          }}, '✕'));

      tr.append(tdL, tdV, tdC);
      tbody.append(tr);
    });
    tbl.append(tbody);
    wrap.append(tbl);
  }
  rebuild();

  const ctrls = el('div', { class: 'dde-table-ctrls' },
    el('button', {
      class: 'btn btn-outline btn-sm', type: 'button',
      onclick: () => {
        rows.push({ label: '', value: '' });
        rebuild();
        scheduleBlockSave(b.id, getCurrent);
      },
    }, '+ 항목 추가'),
    el('span', { class: 'dde-table-hint' },
      '방선보고서 헤더용. Label(좌) / Value(우) 형식'),
  );

  body.append(wrap, ctrls);
}

// ─── 신규: defect_table (Defect List) ───────────────────────
const RISK_OPTIONS = [
  { v: 'L', label: 'L  (Low Risk)',    color: '#d1fae5', textColor: '#065f46' },
  { v: 'M', label: 'M  (Medium Risk)', color: '#fef3c7', textColor: '#92400e' },
  { v: 'H', label: 'H  (High Risk)',   color: '#fee2e2', textColor: '#991b1b' },
];

function renderDefectTable(body, b) {
  const c = b.content || { items: [] };
  let items = (c.items || []).map(it => ({
    item:   it.item || '',
    desc:   it.desc || '',
    fix:    it.fix  || '',
    risk:   it.risk || 'L',
    images: Array.isArray(it.images) ? it.images.slice() : [],
  }));
  if (items.length === 0) {
    items = [{ item: '', desc: '', fix: '', risk: 'L', images: [] }];
  }

  const wrap = el('div', { class: 'bre-defect-wrap' });

  function getCurrent() {
    return { items: items.map(it => ({
      item: it.item, desc: it.desc, fix: it.fix, risk: it.risk,
      images: it.images.slice(),
    })) };
  }

  function rebuild() {
    wrap.innerHTML = '';
    // 헤더
    const tbl = el('table', { class: 'bre-defect-table' });
    const thead = el('thead');
    thead.append(el('tr', {},
      el('th', { class: 'bre-defect-th-no' }, 'No'),
      el('th', { class: 'bre-defect-th-photo' }, 'Photo'),
      el('th', { class: 'bre-defect-th-desc' }, 'Description (Findings)'),
      el('th', { class: 'bre-defect-th-fix' }, 'Rectification'),
      el('th', { class: 'bre-defect-th-risk' }, 'Risk'),
      el('th', { class: 'dde-tbl-ctrl-col' }),
    ));
    tbl.append(thead);

    const tbody = el('tbody');
    items.forEach((it, idx) => {
      const tr = el('tr', { class: `bre-defect-row bre-defect-risk-${it.risk}` });

      // 번호
      tr.append(el('td', { class: 'bre-defect-no' }, String(idx + 1)));

      // 사진 (드롭/업로드)
      const photoCell = el('td', { class: 'bre-defect-photo-cell' });
      renderDefectPhoto(photoCell, items, idx, b, getCurrent, rebuild);
      tr.append(photoCell);

      // Description (Item + Findings)
      const descCell = el('td', { class: 'bre-defect-desc-cell' });
      const itemInp = el('input', {
        type: 'text', class: 'bre-defect-item-input',
        placeholder: '결함 제목 (예: Hull Scratch Damage & Rusty condition)',
        value: it.item,
        oninput: (e) => { items[idx].item = e.target.value; scheduleBlockSave(b.id, getCurrent); },
      });
      const descTa = el('textarea', {
        class: 'bre-defect-desc-input', rows: 3,
        placeholder: '발견 사항 (Findings) — 한 줄당 한 항목',
        oninput: (e) => {
          items[idx].desc = e.target.value;
          autoResize(e.target);
          scheduleBlockSave(b.id, getCurrent);
        },
      });
      descTa.value = it.desc;
      setTimeout(() => autoResize(descTa), 0);
      descCell.append(itemInp, descTa);
      tr.append(descCell);

      // Rectification
      const fixCell = el('td', { class: 'bre-defect-fix-cell' });
      const fixTa = el('textarea', {
        class: 'bre-defect-fix-input', rows: 3,
        placeholder: '조치 사항 (Rectification)',
        oninput: (e) => {
          items[idx].fix = e.target.value;
          autoResize(e.target);
          scheduleBlockSave(b.id, getCurrent);
        },
      });
      fixTa.value = it.fix;
      setTimeout(() => autoResize(fixTa), 0);
      fixCell.append(fixTa);
      tr.append(fixCell);

      // Risk Level (드롭다운)
      const riskCell = el('td', { class: 'bre-defect-risk-cell' });
      const riskSel = el('select', {
        class: 'bre-defect-risk-select',
        onchange: (e) => {
          items[idx].risk = e.target.value;
          rebuild();   // 행 색깔 갱신
          scheduleBlockSave(b.id, getCurrent);
        },
      });
      for (const opt of RISK_OPTIONS) {
        const o = el('option', { value: opt.v }, opt.label);
        if (opt.v === it.risk) o.setAttribute('selected', '');
        riskSel.append(o);
      }
      riskCell.append(riskSel);
      tr.append(riskCell);

      // 행 삭제
      tr.append(el('td', { class: 'dde-tbl-ctrl-col' },
        el('button', { class: 'dde-row-del', type: 'button', title: '결함 삭제',
          onclick: () => {
            if (items.length <= 1) {
              items[0] = { item: '', desc: '', fix: '', risk: 'L', images: [] };
            } else items.splice(idx, 1);
            rebuild();
            scheduleBlockSave(b.id, getCurrent);
          }}, '✕')));

      tbody.append(tr);
    });
    tbl.append(tbody);
    wrap.append(tbl);

    // Risk Legend
    wrap.append(el('div', { class: 'bre-defect-legend' },
      el('strong', {}, 'Level of Risk:  '),
      el('span', { class: 'bre-defect-legend-L' }, 'L : Low'),
      el('span', {}, '   '),
      el('span', { class: 'bre-defect-legend-M' }, 'M : Medium'),
      el('span', {}, '   '),
      el('span', { class: 'bre-defect-legend-H' }, 'H : High'),
    ));
  }
  rebuild();

  const ctrls = el('div', { class: 'dde-table-ctrls' },
    el('button', {
      class: 'btn btn-outline btn-sm', type: 'button',
      onclick: () => {
        items.push({ item: '', desc: '', fix: '', risk: 'L', images: [] });
        rebuild();
        scheduleBlockSave(b.id, getCurrent);
      },
    }, '+ 결함 항목 추가'),
  );
  body.append(wrap, ctrls);
}

// defect 항목의 사진 셀 렌더링
function renderDefectPhoto(photoCell, items, idx, b, getCurrent, rebuild) {
  const imgs = items[idx].images;

  // ── 드래그앤드롭: 이 결함 행의 사진 셀에 이미지 드롭 → 추가 ──
  installDndNavGuard();
  let dragDepth = 0;
  const dndHasFiles = (e) =>
    e.dataTransfer && Array.from(e.dataTransfer.types || []).includes('Files');
  photoCell.addEventListener('dragenter', (e) => {
    if (!dndHasFiles(e)) return;
    e.preventDefault();
    dragDepth += 1;
    photoCell.classList.add('bre-defect-dnd-over');
  });
  photoCell.addEventListener('dragover', (e) => {
    if (!dndHasFiles(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
  });
  photoCell.addEventListener('dragleave', (e) => {
    if (!dndHasFiles(e)) return;
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) photoCell.classList.remove('bre-defect-dnd-over');
  });
  photoCell.addEventListener('drop', (e) => {
    if (!e.dataTransfer) return;
    e.preventDefault();
    dragDepth = 0;
    photoCell.classList.remove('bre-defect-dnd-over');
    uploadDefectFiles(items, idx, b, getCurrent, rebuild, e.dataTransfer.files);
  });

  if (imgs.length === 0) {
    photoCell.append(el('button', {
      class: 'bre-defect-photo-add', type: 'button',
      title: '클릭 또는 사진을 끌어다 놓기',
      onclick: () => uploadDefectImage(items, idx, b, getCurrent, rebuild),
    }, '📷', el('br'), el('span', {}, '사진 추가')));
    return;
  }

  // 첫 번째 사진만 셀 안에 큼지막하게, 나머지는 작게 thumb
  const main = imgs[0];
  photoCell.append(el('img', {
    class: 'bre-defect-photo-main', src: main.url, alt: '',
    onclick: () => uploadDefectImage(items, idx, b, getCurrent, rebuild, 0),
  }));

  if (imgs.length > 1) {
    const thumbs = el('div', { class: 'bre-defect-photo-thumbs' });
    imgs.slice(1).forEach((im, ti) => {
      thumbs.append(el('img', {
        class: 'bre-defect-photo-thumb', src: im.url, alt: '',
        onclick: () => uploadDefectImage(items, idx, b, getCurrent, rebuild, ti + 1),
      }));
    });
    photoCell.append(thumbs);
  }

  photoCell.append(el('div', { class: 'bre-defect-photo-actions' },
    el('button', {
      class: 'bre-defect-photo-add-btn', type: 'button', title: '사진 추가',
      onclick: () => uploadDefectImage(items, idx, b, getCurrent, rebuild),
    }, '+'),
    el('button', {
      class: 'bre-defect-photo-del-btn', type: 'button', title: '마지막 사진 제거',
      onclick: () => {
        if (!confirm('마지막 사진을 제거하시겠습니까?')) return;
        items[idx].images.pop();
        rebuild();
        scheduleBlockSave(b.id, getCurrent);
      },
    }, '−'),
  ));
}

function uploadDefectImage(items, idx, b, getCurrent, rebuild, replaceAt) {
  const inp = $('#bre-img-input');
  inp.removeAttribute('multiple');  // 한 번에 1장씩
  const onChange = async () => {
    inp.removeEventListener('change', onChange);
    inp.setAttribute('multiple', '');  // 원복
    const file = inp.files?.[0];
    inp.value = '';
    if (!file) return;
    setSaveStatus('사진 업로드 중...', 'busy');
    try {
      const fd = new FormData();
      fd.append('file', file);
      const res = await api(`/api/boarding-reports/${E.reportId}/upload-image`, {
        method: 'POST', body: fd,
      });
      if (typeof replaceAt === 'number') {
        items[idx].images[replaceAt] = { filename: res.filename, url: res.url };
      } else {
        items[idx].images.push({ filename: res.filename, url: res.url });
      }
      await api(`/api/boarding-blocks/${b.id}`, {
        method: 'PUT', body: JSON.stringify({ content: getCurrent() }),
      });
      rebuild();
      setSaveStatus('저장됨', 'ok');
    } catch (e) {
      setSaveStatus('업로드 실패: ' + e.message, 'err');
      alert('업로드 실패: ' + e.message);
    }
  };
  inp.addEventListener('change', onChange);
  inp.click();
}

// 결함 사진 셀에 드롭된 여러 이미지를 순차 업로드해 해당 행에 추가
async function uploadDefectFiles(items, idx, b, getCurrent, rebuild, fileList) {
  const files = [...(fileList || [])].filter(f => f && (f.type || '').startsWith('image/'));
  if (!files.length) {
    setSaveStatus('이미지 파일만 추가할 수 있습니다', 'err');
    return;
  }
  try {
    let done = 0;
    for (const f of files) {
      setSaveStatus(`사진 업로드 중 (${done + 1}/${files.length})...`, 'busy');
      const fd = new FormData();
      fd.append('file', f);
      const res = await api(`/api/boarding-reports/${E.reportId}/upload-image`, {
        method: 'POST', body: fd,
      });
      items[idx].images.push({ filename: res.filename, url: res.url });
      done += 1;
    }
    await api(`/api/boarding-blocks/${b.id}`, {
      method: 'PUT', body: JSON.stringify({ content: getCurrent() }),
    });
    rebuild();
    setSaveStatus('저장됨', 'ok');
  } catch (e) {
    setSaveStatus('업로드 실패: ' + e.message, 'err');
    alert('업로드 실패: ' + e.message);
  }
}
function scheduleBlockSave(blockId, getContent) {
  clearTimeout(E.saveTimer);
  setSaveStatus('저장 대기...', 'busy');
  E.saveTimer = setTimeout(async () => {
    setSaveStatus('저장 중...', 'busy');
    try {
      await api(`/api/boarding-blocks/${blockId}`, {
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
    const res = await api(`/api/boarding-sections/${E.activeSecId}/blocks`, {
      method: 'POST', body: JSON.stringify({ block_type: blockType }),
    });
    const info = E.byId.get(E.activeSecId);
    const currentCount = (info?.section.blocks || []).length;
    const movesUp = currentCount - position;
    for (let i = 0; i < movesUp; i++) {
      await api(`/api/boarding-blocks/${res.id}/move`, {
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
    await api(`/api/boarding-blocks/${bid}`, { method: 'DELETE' });
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
    await api(`/api/boarding-blocks/${bid}/move`, {
      method: 'POST', body: JSON.stringify({ direction }),
    });
    await loadReport();
  } catch (e) { alert('순서 변경 실패: ' + e.message); }
}

// ─── Events ──────────────────────────────────────────────────
function bindEvents() {
  $('#bre-btn-add-section').addEventListener('click', () => addSection(null));
  $('#bre-btn-add-sub').addEventListener('click', () => {
    if (!E.activeSecId) return;
    addSection(E.activeSecId);
  });

  $('#bre-btn-bulk-add').addEventListener('click', openBulkAddDialog);
  $('#bre-bulk-apply').addEventListener('click', applyBulkAdd);
  $('#bre-bulk-modal').addEventListener('click', (ev) => {
    if (ev.target.dataset.close === '1') closeBulkAddDialog();
  });
  $('#bre-bulk-text').addEventListener('keydown', (e) => {
    if (e.key === 'Tab') {
      e.preventDefault();
      const ta = e.target;
      const s = ta.selectionStart, t = ta.selectionEnd;
      ta.value = ta.value.slice(0, s) + '\t' + ta.value.slice(t);
      ta.selectionStart = ta.selectionEnd = s + 1;
    }
  });

  let titleTimer;
  $('#bre-section-title').addEventListener('input', (e) => {
    if (!E.activeSecId) return;
    clearTimeout(titleTimer);
    setSaveStatus('저장 대기...', 'busy');
    titleTimer = setTimeout(() => saveSectionTitle(E.activeSecId, e.target.value), 500);
  });

  $('#bre-btn-del-section').addEventListener('click', () => {
    if (E.activeSecId) deleteSection(E.activeSecId);
  });

  $('#bre-img-input').setAttribute('multiple', '');

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !$('#bre-bulk-modal').hidden) closeBulkAddDialog();
  });

  $('#bre-btn-export-docx').addEventListener('click', () => {
    setSaveStatus('Word 생성 중...', 'busy');
    window.location = `/api/boarding-reports/${E.reportId}/export/docx`;
    setTimeout(() => setSaveStatus('저장됨', 'ok'), 1500);
  });
  $('#bre-btn-export-pdf').addEventListener('click', () => {
    setSaveStatus('PDF 변환 중... (10~20초 소요)', 'busy');
    window.location = `/api/boarding-reports/${E.reportId}/export/pdf`;
    setTimeout(() => setSaveStatus('저장됨', 'ok'), 3000);
  });
}

init();
