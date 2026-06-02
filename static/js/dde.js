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

// 목차 항목 우클릭 메뉴 - 빠른 액션
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

// "다른 섹션으로 이동" 모달
function openReparentModal(sid) {
  const info = E.byId.get(sid);
  if (!info) return;

  // 후보 부모: 자신과 자손 제외, 깊이가 너무 깊지 않은 것만
  // 깊이 제한: 새 부모의 depth + 1(이동할 섹션) + 최대 자손 깊이 ≤ 3
  const descendants = new Set();
  (function collect(node) {
    descendants.add(node.id);
    (node.children || []).forEach(collect);
  })(info.section);

  // 자손 중 가장 깊은 깊이 (info.depth 기준 상대값)
  let maxRelDepth = 0;
  (function md(node, d) {
    maxRelDepth = Math.max(maxRelDepth, d);
    (node.children || []).forEach(c => md(c, d + 1));
  })(info.section, 0);

  // 후보: 깊이가 (2 - maxRelDepth) 이하인 섹션 + "최상위"
  const maxAllowedDepth = 2 - maxRelDepth;

  const candidates = []; // { id, label, depth }
  // 최상위 옵션
  if (info.section.parent_id != null && maxAllowedDepth >= 0) {
    candidates.push({ id: null, label: '— 최상위 (depth 0) —', depth: -1 });
  }
  for (const [id, item] of E.byId.entries()) {
    if (descendants.has(id)) continue;
    if (item.depth > maxAllowedDepth) continue;
    if (item.section.parent_id === null && info.section.parent_id === null
        && id === sid) continue;
    // 자기 자신은 이미 descendants에 들어있어서 제외됨
    if (id === info.section.parent_id) continue;  // 이미 그 부모임
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

  // 모달 UI
  const backdrop = el('div', { class: 'dde-modal-backdrop' });
  const dialog = el('div', { class: 'dde-modal' });
  dialog.append(
    el('div', { class: 'dde-modal-title' },
      `"${info.section.title}" 을(를) 이동할 위치 선택`),
    el('div', { class: 'dde-modal-hint' },
      '아래에서 새 부모 섹션을 선택하세요. 자기 자신과 자손은 표시되지 않습니다.'),
  );

  const list = el('div', { class: 'dde-modal-list' });
  let selected = null;
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
    await api(`/api/dock-sections/${sid}/reparent`, {
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
    await api(`/api/dock-sections/${sid}`, {
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
  // 기존 호환 함수: rich 파서로 가서 cells의 text만 추출
  const rich = parseHtmlTableRich(html);
  if (!rich) return null;
  return rich.cells.map(row => row.map(c => c ? c.text : ''));
}

/**
 * HTML 안의 <table>을 cells 2D 배열 + 헤더 행 수와 함께 반환.
 *   {
 *     cells: [[ {text, rowspan, colspan} | null, ... ], ...],
 *     header_row_count: number,
 *   }
 * rowspan/colspan으로 가려지는 위치는 null로 표시.
 */
function parseHtmlTableRich(html) {
  try {
    const doc = new DOMParser().parseFromString(html, 'text/html');
    const tbl = doc.querySelector('table');
    if (!tbl) return null;

    const trs = Array.from(tbl.querySelectorAll('tr'));
    if (trs.length === 0) return null;

    // 최대 열 수 계산 (colspan 고려)
    let maxCols = 0;
    {
      // pseudo-render로 최대 col 인덱스 계산
      const occ = [];
      trs.forEach((tr, ri) => {
        occ[ri] = occ[ri] || [];
        let ci = 0;
        const cellEls = Array.from(tr.querySelectorAll('th, td'));
        for (const c of cellEls) {
          while (occ[ri][ci]) ci++;
          const rs = parseInt(c.getAttribute('rowspan') || '1', 10) || 1;
          const cs = parseInt(c.getAttribute('colspan') || '1', 10) || 1;
          for (let r = ri; r < ri + rs; r++) {
            occ[r] = occ[r] || [];
            for (let cc = ci; cc < ci + cs; cc++) {
              occ[r][cc] = true;
            }
          }
          if (ci + cs > maxCols) maxCols = ci + cs;
          ci += cs;
        }
      });
    }

    // cells 2D 배열 생성
    const nRows = trs.length;
    const cells = Array.from({ length: nRows }, () => new Array(maxCols).fill(undefined));
    // undefined = 아직 미할당, null = 병합으로 가려진 셀, object = 마스터

    function getCellText(c) {
      let text = c.innerHTML
        .replace(/<br\s*\/?>/gi, ' ')
        .replace(/<[^>]+>/g, '')
        .replace(/&nbsp;/g, ' ')
        .replace(/&amp;/g, '&')
        .replace(/&lt;/g, '<')
        .replace(/&gt;/g, '>');
      return text.replace(/[\s\u00A0]+/g, ' ').trim();
    }

    let headerRowCount = 0;
    trs.forEach((tr, ri) => {
      let ci = 0;
      const cellEls = Array.from(tr.querySelectorAll('th, td'));
      // 이 행에 th가 하나라도 있고 ri가 연속이면 헤더 행
      const hasTh = cellEls.some(e => e.tagName === 'TH');
      if (hasTh && ri === headerRowCount) headerRowCount = ri + 1;

      for (const c of cellEls) {
        while (cells[ri][ci] !== undefined) ci++;
        const rs = parseInt(c.getAttribute('rowspan') || '1', 10) || 1;
        const cs = parseInt(c.getAttribute('colspan') || '1', 10) || 1;
        cells[ri][ci] = { text: getCellText(c), rowspan: rs, colspan: cs };
        // 병합으로 가려지는 셀들은 null로
        for (let r = ri; r < ri + rs; r++) {
          for (let cc = ci; cc < ci + cs; cc++) {
            if (r === ri && cc === ci) continue;
            cells[r][cc] = null;
          }
        }
        ci += cs;
      }
    });

    // 남은 undefined를 빈 셀로 채움
    for (let r = 0; r < nRows; r++) {
      for (let c = 0; c < maxCols; c++) {
        if (cells[r][c] === undefined) {
          cells[r][c] = { text: '', rowspan: 1, colspan: 1 };
        }
      }
    }

    return {
      cells,
      header_row_count: Math.max(1, headerRowCount || 1),
    };
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

// ─── table (cells 기반: 다중 헤더 + 셀 병합 지원) ───────────
//
// 데이터 구조:
//   {
//     header_row_count: number,    // 헤더 행 수 (1 이상, 첫 N행이 헤더)
//     cells: [                     // 2D 배열, 각 셀이 객체 or null
//       [
//         { text, rowspan?, colspan? },  // 마스터 셀
//         null,                          // 병합으로 가려진 셀
//         ...
//       ],
//       ...
//     ],
//     col_widths: [...]
//   }
//
// 호환성: 기존 { headers, rows, col_widths } 구조 → 자동 변환

function normalizeTableContent(c) {
  if (c && Array.isArray(c.cells)) {
    // 이미 새 구조 — 정리만
    const cells = c.cells.map(row => row.map(cell =>
      cell === null ? null : {
        text: cell.text || '',
        rowspan: Math.max(1, cell.rowspan || 1),
        colspan: Math.max(1, cell.colspan || 1),
      }));
    return {
      header_row_count: Math.max(1, c.header_row_count || 1),
      cells,
      col_widths: Array.isArray(c.col_widths) ? c.col_widths.slice() : [],
    };
  }
  // 옛 구조 변환
  const headers = (c?.headers || ['항목', '내용']);
  const rows = (c?.rows || [['', '']]);
  const cells = [
    headers.map(h => ({ text: h || '', rowspan: 1, colspan: 1 })),
    ...rows.map(r => r.map(v => ({ text: v || '', rowspan: 1, colspan: 1 }))),
  ];
  return {
    header_row_count: 1,
    cells,
    col_widths: Array.isArray(c?.col_widths) ? c.col_widths.slice() : [],
  };
}

function tableContentToOld(content) {
  // 다른 시스템 호환성을 위해 (예: Word 출력) 기존 헤더/행 형식으로도 변환 제공
  // 단, 병합 정보가 있으면 그대로 cells 사용 권장
  const { cells, header_row_count, col_widths } = content;
  const nCols = cells[0]?.length || 0;
  const headers = [];
  // 첫 헤더 행을 단순 평탄화 (병합된 부분은 첫 셀 text로)
  if (cells[0]) {
    for (let ci = 0; ci < nCols; ci++) {
      const c = cells[0][ci];
      headers.push(c ? c.text : '');
    }
  }
  const rows = cells.slice(header_row_count).map(row =>
    row.map(c => c ? c.text : ''));
  return { headers, rows, col_widths };
}

// cells에서 (ri, ci) 위치를 차지하는 마스터 셀 좌표를 반환
function findMasterCell(cells, ri, ci) {
  const nRows = cells.length;
  const nCols = cells[0]?.length || 0;
  if (ri < 0 || ri >= nRows || ci < 0 || ci >= nCols) return null;
  if (cells[ri][ci] !== null) return { ri, ci };
  // null이면 위/왼쪽으로 마스터를 찾음
  for (let r = ri; r >= 0; r--) {
    for (let c = ci; c >= 0; c--) {
      const cell = cells[r][c];
      if (cell !== null) {
        if (r + (cell.rowspan || 1) > ri && c + (cell.colspan || 1) > ci) {
          return { ri: r, ci: c };
        }
      }
    }
  }
  return null;
}

function renderTable(body, b) {
  const content = normalizeTableContent(b.content);
  let cells = content.cells;
  let headerRowCount = content.header_row_count;
  let colWidths = content.col_widths;

  const nCols = () => cells[0]?.length || 0;
  const nRows = () => cells.length;

  // colWidths 길이 보정
  if (colWidths.length !== nCols()) {
    colWidths = new Array(nCols()).fill(0);
  }

  const tblWrap = el('div', { class: 'dde-table-wrap' });
  let selectedCells = new Set();   // 선택된 셀 좌표 (`r,c` 문자열) — 병합용

  function getCurrent() {
    // 0(auto) 컬럼 너비를 실제 렌더링된 너비로 채움 (Word 출력 비율 정확)
    const liveTbl = tblWrap.querySelector('.dde-table');
    if (liveTbl) {
      const cols = liveTbl.querySelectorAll('colgroup col');
      for (let i = 0; i < nCols(); i++) {
        if (!colWidths[i] || colWidths[i] <= 0) {
          const col = cols[i];
          if (col) {
            const w = Math.round(col.getBoundingClientRect().width);
            if (w > 0) colWidths[i] = w;
          }
        }
      }
    }
    // 셀 깊은 복사
    return {
      header_row_count: headerRowCount,
      cells: cells.map(row => row.map(c => c === null ? null : {
        text: c.text || '',
        rowspan: c.rowspan || 1,
        colspan: c.colspan || 1,
      })),
      col_widths: colWidths.slice(),
    };
  }

  function selKey(r, c) { return `${r},${c}`; }

  function rebuild() {
    tblWrap.innerHTML = '';
    const tbl = el('table', { class: 'dde-table' });

    // colgroup
    const cg = el('colgroup');
    for (let ci = 0; ci < nCols(); ci++) {
      const w = colWidths[ci] || 0;
      const col = el('col');
      if (w > 0) col.style.width = w + 'px';
      cg.append(col);
    }
    cg.append(el('col', { class: 'dde-tbl-ctrl-col-c' }));
    tbl.append(cg);

    // 헤더 영역 (첫 N행)
    const thead = el('thead');
    for (let ri = 0; ri < headerRowCount && ri < nRows(); ri++) {
      const tr = el('tr');
      renderCellsInRow(tr, ri, true, tbl);
      // 컨트롤 칸 (헤더 첫 행에만 표시)
      if (ri === 0) {
        const ctrl = el('th', { class: 'dde-tbl-ctrl-col', rowspan: headerRowCount });
        tr.append(ctrl);
      }
      thead.append(tr);
    }
    tbl.append(thead);

    // 본문 영역
    const tbody = el('tbody');
    for (let ri = headerRowCount; ri < nRows(); ri++) {
      const tr = el('tr');
      renderCellsInRow(tr, ri, false, tbl);
      tr.append(el('td', { class: 'dde-tbl-ctrl-col' },
        el('button', { class: 'dde-row-del', type: 'button', title: '행 삭제',
          onclick: () => deleteRow(ri) }, '✕')));
      tbody.append(tr);
    }
    tbl.append(tbody);
    tblWrap.append(tbl);
  }

  function renderCellsInRow(tr, ri, isHeader, tbl) {
    for (let ci = 0; ci < nCols(); ci++) {
      const cell = cells[ri][ci];
      if (cell === null) continue;  // 병합으로 가려진 셀 → 건너뜀

      const tagName = isHeader ? 'th' : 'td';
      const td = el(tagName, {
        class: selectedCells.has(selKey(ri, ci)) ? 'dde-cell-selected' : '',
      });
      if (cell.rowspan && cell.rowspan > 1) td.setAttribute('rowspan', cell.rowspan);
      if (cell.colspan && cell.colspan > 1) td.setAttribute('colspan', cell.colspan);

      // 셀 클릭: 선택 토글 (Ctrl/Cmd 누르면 다중 선택)
      td.addEventListener('mousedown', (e) => {
        if (e.shiftKey || e.ctrlKey || e.metaKey) {
          e.preventDefault();
          const k = selKey(ri, ci);
          if (selectedCells.has(k)) selectedCells.delete(k);
          else selectedCells.add(k);
          td.classList.toggle('dde-cell-selected', selectedCells.has(k));
        } else if (selectedCells.size > 0 && !td.contains(e.target.closest('textarea,input,button'))) {
          // 빈 곳 클릭 시 선택 해제
          selectedCells.clear();
          tblWrap.querySelectorAll('.dde-cell-selected').forEach(n =>
            n.classList.remove('dde-cell-selected'));
        }
      });

      // 우클릭 메뉴
      td.addEventListener('contextmenu', (e) => {
        e.preventDefault();
        showCellContextMenu(e, ri, ci);
      });

      // 헤더 셀: 컬럼 삭제 버튼 + 리사이즈 (첫 헤더 행, colspan=1만)
      if (isHeader && ri === 0 && (cell.colspan || 1) === 1) {
        const delBtn = el('button', {
          class: 'dde-col-del', type: 'button', title: '열 삭제',
          onclick: (e) => { e.stopPropagation(); deleteCol(ci); },
        }, '✕');
        td.append(_makeCellEditor(ri, ci, true));
        td.append(delBtn);
        if (ci < nCols() - 1) {
          const handle = el('div', {
            class: 'dde-col-resize',
            onmousedown: (e) => startColResize(e, ci, tbl, () => scheduleBlockSave(b.id, getCurrent)),
          });
          td.append(handle);
        }
      } else {
        td.append(_makeCellEditor(ri, ci, isHeader));
      }

      tr.append(td);
    }
  }

  function _makeCellEditor(ri, ci, isHeader) {
    const cell = cells[ri][ci];
    if (isHeader && (cell.colspan || 1) === 1 && (cell.rowspan || 1) === 1) {
      // 단일 헤더 셀: input
      const inp = el('input', {
        type: 'text', class: 'dde-cell-input',
        value: cell.text || '', placeholder: '헤더',
        oninput: (e) => { cells[ri][ci].text = e.target.value; scheduleBlockSave(b.id, getCurrent); },
        onpaste: (e) => handleTablePaste(e, ri, ci),
      });
      return inp;
    }
    // 그 외(병합되었거나 본문): textarea
    const ta = el('textarea', {
      class: 'dde-cell-textarea', rows: 1, placeholder: '',
      oninput: (e) => {
        cells[ri][ci].text = e.target.value;
        autoResize(e.target);
        scheduleBlockSave(b.id, getCurrent);
      },
      onpaste: (e) => handleTablePaste(e, ri, ci),
    });
    ta.value = cell.text || '';
    setTimeout(() => autoResize(ta), 0);
    return ta;
  }

  // ─── 행/열 삭제 ───
  function deleteRow(ri) {
    if (nRows() <= 1) { alert('최소 1개 행이 필요합니다.'); return; }
    // 이 행 안에 마스터인 병합 셀이 있으면 → 그 셀의 rowspan을 줄임
    for (let ci = 0; ci < nCols(); ci++) {
      const cell = cells[ri][ci];
      if (cell && (cell.rowspan || 1) > 1) {
        // 마스터를 한 칸 아래로 이동
        if (ri + 1 < nRows()) {
          cells[ri + 1][ci] = { text: cell.text, rowspan: cell.rowspan - 1, colspan: cell.colspan };
        }
      }
    }
    // 이 행을 가리는 위쪽 병합 셀들의 rowspan 감소
    for (let ci = 0; ci < nCols(); ci++) {
      if (cells[ri][ci] === null) {
        const master = findMasterCell(cells, ri, ci);
        if (master && master.ri < ri) {
          cells[master.ri][master.ci].rowspan -= 1;
        }
      }
    }
    cells.splice(ri, 1);
    if (headerRowCount > nRows()) headerRowCount = Math.max(1, nRows());
    if (ri < headerRowCount) headerRowCount = Math.max(1, headerRowCount - 1);
    rebuild();
    scheduleBlockSave(b.id, getCurrent);
  }

  function deleteCol(ci) {
    if (nCols() <= 1) { alert('최소 1개 열이 필요합니다.'); return; }
    for (let ri = 0; ri < nRows(); ri++) {
      const cell = cells[ri][ci];
      if (cell && (cell.colspan || 1) > 1) {
        // 마스터를 한 칸 오른쪽으로 이동
        if (ci + 1 < nCols()) {
          cells[ri][ci + 1] = { text: cell.text, rowspan: cell.rowspan, colspan: cell.colspan - 1 };
        }
      }
      if (cell === null) {
        const master = findMasterCell(cells, ri, ci);
        if (master && master.ci < ci) {
          cells[master.ri][master.ci].colspan -= 1;
        }
      }
    }
    for (let ri = 0; ri < nRows(); ri++) cells[ri].splice(ci, 1);
    colWidths.splice(ci, 1);
    rebuild();
    scheduleBlockSave(b.id, getCurrent);
  }

  // ─── 컨텍스트 메뉴 ───
  function showCellContextMenu(ev, ri, ci) {
    document.querySelectorAll('.dde-table-ctx-menu').forEach(m => m.remove());
    const menu = el('div', { class: 'dde-table-ctx-menu' });

    const selectedArr = [...selectedCells].map(k => k.split(',').map(Number));
    const canMerge = selectedCells.size >= 2 && isRectangularSelection(selectedArr);
    const cell = cells[ri][ci];
    const canSplit = cell && ((cell.rowspan || 1) > 1 || (cell.colspan || 1) > 1);

    function addItem(label, fn, opts = {}) {
      menu.append(el('button', {
        class: 'dde-ctx-item' + (opts.disabled ? ' disabled' : ''),
        type: 'button', disabled: opts.disabled,
        onclick: () => { menu.remove(); fn(); },
      }, label));
    }
    function addSep() { menu.append(el('div', { class: 'dde-ctx-sep' })); }

    addItem(`헤더 행 수: ${headerRowCount}  (변경)`, () => {
      const v = prompt('헤더로 사용할 첫 N행을 입력하세요 (1~' + nRows() + '):', String(headerRowCount));
      if (v === null) return;
      const n = parseInt(v, 10);
      if (!isNaN(n) && n >= 1 && n <= nRows()) {
        headerRowCount = n;
        rebuild();
        scheduleBlockSave(b.id, getCurrent);
      }
    });
    addSep();
    addItem(`셀 병합 (${selectedCells.size}개 선택됨)`, mergeSelected, { disabled: !canMerge });
    addItem('셀 분할', () => splitCell(ri, ci), { disabled: !canSplit });
    addSep();
    addItem('위에 행 추가', () => insertRow(ri));
    addItem('아래에 행 추가', () => insertRow(ri + 1));
    addItem('왼쪽에 열 추가', () => insertCol(ci));
    addItem('오른쪽에 열 추가', () => insertCol(ci + 1));
    addSep();
    addItem('이 행 삭제', () => deleteRow(ri));
    addItem('이 열 삭제', () => deleteCol(ci));

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

  function isRectangularSelection(arr) {
    if (arr.length < 2) return false;
    const rs = arr.map(p => p[0]);
    const cs = arr.map(p => p[1]);
    const r0 = Math.min(...rs), r1 = Math.max(...rs);
    const c0 = Math.min(...cs), c1 = Math.max(...cs);
    const expected = (r1 - r0 + 1) * (c1 - c0 + 1);
    if (arr.length !== expected) return false;
    // 모든 셀이 영역 안에 있어야 함
    for (const [r, c] of arr) {
      if (r < r0 || r > r1 || c < c0 || c > c1) return false;
    }
    return true;
  }

  function mergeSelected() {
    const arr = [...selectedCells].map(k => k.split(',').map(Number));
    if (arr.length < 2 || !isRectangularSelection(arr)) {
      alert('직사각형 영역으로 셀들을 선택하세요.');
      return;
    }
    const rs = arr.map(p => p[0]);
    const cs = arr.map(p => p[1]);
    const r0 = Math.min(...rs), r1 = Math.max(...rs);
    const c0 = Math.min(...cs), c1 = Math.max(...cs);
    // 선택 영역 내 마스터들의 텍스트를 한 줄로 합치고 첫 셀에만 남김
    const texts = [];
    for (let r = r0; r <= r1; r++) {
      for (let c = c0; c <= c1; c++) {
        const cl = cells[r][c];
        if (cl && cl.text) texts.push(cl.text);
        if (!(r === r0 && c === c0)) cells[r][c] = null;
      }
    }
    cells[r0][c0] = {
      text: texts.join(' '),
      rowspan: r1 - r0 + 1,
      colspan: c1 - c0 + 1,
    };
    selectedCells.clear();
    rebuild();
    scheduleBlockSave(b.id, getCurrent);
  }

  function splitCell(ri, ci) {
    const cell = cells[ri][ci];
    if (!cell) return;
    const rs = cell.rowspan || 1;
    const cs = cell.colspan || 1;
    if (rs === 1 && cs === 1) return;
    // 마스터는 1×1로 줄이고, 나머지 자리에 빈 셀 채움
    cells[ri][ci] = { text: cell.text, rowspan: 1, colspan: 1 };
    for (let r = ri; r < ri + rs; r++) {
      for (let c = ci; c < ci + cs; c++) {
        if (r === ri && c === ci) continue;
        cells[r][c] = { text: '', rowspan: 1, colspan: 1 };
      }
    }
    rebuild();
    scheduleBlockSave(b.id, getCurrent);
  }

  function insertRow(at) {
    const newRow = new Array(nCols()).fill(null).map(() => ({ text: '', rowspan: 1, colspan: 1 }));
    // 삽입 위치를 가로지르는 병합 셀들은 rowspan 확장
    if (at > 0 && at < nRows()) {
      for (let ci = 0; ci < nCols(); ci++) {
        if (cells[at][ci] === null) {
          const m = findMasterCell(cells, at, ci);
          if (m && m.ri < at) {
            cells[m.ri][m.ci].rowspan += 1;
            // 새 행 자리에 null 채움 (병합 유지)
            newRow[ci] = null;
          }
        }
      }
    }
    cells.splice(at, 0, newRow);
    if (at < headerRowCount) headerRowCount += 1;
    rebuild();
    scheduleBlockSave(b.id, getCurrent);
  }

  function insertCol(at) {
    for (let ri = 0; ri < nRows(); ri++) {
      const newCell = { text: '', rowspan: 1, colspan: 1 };
      // 삽입 위치를 가로지르는 병합 셀은 colspan 확장
      if (at > 0 && at < nCols() && cells[ri][at] === null) {
        const m = findMasterCell(cells, ri, at);
        if (m && m.ci < at) {
          cells[m.ri][m.ci].colspan += 1;
          cells[ri].splice(at, 0, null);
          continue;
        }
      }
      cells[ri].splice(at, 0, newCell);
    }
    colWidths.splice(at, 0, 0);
    rebuild();
    scheduleBlockSave(b.id, getCurrent);
  }

  function startColResize(ev, colIndex, tbl, onDone) {
    ev.preventDefault();
    ev.stopPropagation();
    const cols = tbl.querySelectorAll('colgroup col');
    if (!cols[colIndex]) {
      console.warn('[resize] col not found at', colIndex);
      return;
    }
    const startX = ev.clientX;
    const startW = cols[colIndex].getBoundingClientRect().width;
    document.body.classList.add('dde-col-resizing');
    console.log('[resize] start col=', colIndex, 'startW=', startW);

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
      console.log('[resize] end col=', colIndex, 'newW=', colWidths[colIndex]);
      onDone();
    }
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  }

  // ─── 엑셀/구글시트 표 붙여넣기 핸들러 (cells 기반 + rowspan/colspan) ───
  function handleTablePaste(ev, ri, ci) {
    const cd = ev.clipboardData || window.clipboardData;
    if (!cd) return;
    const html = cd.getData('text/html');
    const txt  = cd.getData('text/plain') || '';

    // HTML에 <table>이 있으면 우선 사용 (rowspan/colspan 인식하기 위해)
    let pasted = null;   // { cells: 2D, header_row_count: number }
    if (html && /<t(able|r|d|h)\b/i.test(html)) {
      pasted = parseHtmlTableRich(html);
    }
    if (!pasted) {
      // TSV 폴백
      const grid = parseTsv(txt);
      if (!grid) return;
      pasted = {
        cells: grid.map(row => row.map(t => ({ text: t, rowspan: 1, colspan: 1 }))),
        header_row_count: 0,
      };
    }
    if (!pasted.cells || pasted.cells.length === 0) return;

    const pasteRows = pasted.cells.length;
    const pasteCols = Math.max(...pasted.cells.map(r => r.length));
    if (pasteRows === 1 && pasteCols === 1) {
      // 단일 셀 — 기본 paste 동작
      return;
    }

    ev.preventDefault();

    const hasMerge = pasted.cells.some(row =>
      row.some(c => c && ((c.rowspan || 1) > 1 || (c.colspan || 1) > 1)));
    const mergeNote = hasMerge ? '\n· 셀 병합 정보가 포함되어 있습니다.' : '';

    if (!confirm(
      `표 형식의 데이터를 감지했습니다 (${pasteRows}행 × ${pasteCols}열).${mergeNote}\n\n` +
      `현재 표를 이 데이터로 교체하시겠습니까?\n\n` +
      `· 예: 표 전체 교체 (헤더 행 수도 자동 인식)\n` +
      `· 아니오: 일반 텍스트로 현재 셀에만 붙여넣기`
    )) {
      insertPlainTextAtFocused(ev.target, txt);
      return;
    }

    // 표 전체 교체 (좀 더 단순/안전한 모델)
    cells = pasted.cells.map(row => {
      // 부족한 열을 null로 채우기
      const padded = row.slice();
      while (padded.length < pasteCols) padded.push({ text: '', rowspan: 1, colspan: 1 });
      return padded;
    });
    headerRowCount = Math.max(1, pasted.header_row_count || 1);
    colWidths = new Array(pasteCols).fill(0);

    rebuild();
    scheduleBlockSave(b.id, getCurrent);
    setSaveStatus(`표 ${pasteRows}×${pasteCols} 붙여넣기 완료` + (hasMerge ? ' (병합 포함)' : ''), 'ok');
  }

  rebuild();

  const ctrls = el('div', { class: 'dde-table-ctrls' },
    el('button', { class: 'btn btn-outline btn-sm', type: 'button',
      onclick: () => insertRow(nRows()) },
      '+ 행 추가'),
    el('button', { class: 'btn btn-outline btn-sm', type: 'button',
      onclick: () => insertCol(nCols()) }, '+ 열 추가'),
    el('span', { class: 'dde-table-hint' },
      '💡 Excel 표를 복사·붙여넣기 · 셀 우클릭으로 병합/분할/헤더 행 수 변경 · 셀 클릭 + Ctrl로 다중 선택'),
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
        el('div', { class: 'dde-image-drop-text' }, '클릭하거나 사진을 끌어다 놓기'),
        el('div', { class: 'dde-image-drop-hint' }, '여러 장 한번에 선택·드롭 가능 (Ctrl/Shift)'),
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
  }

  function triggerAddImages() {
    const inp = $('#dde-img-input');
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
