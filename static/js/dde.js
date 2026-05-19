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

  $('#dde-title').textContent = r.title || '제목 없음';
  const subs = [];
  if (r.vessel_name) subs.push(r.vessel_name);
  if (r.dock_no)     subs.push(r.dock_no);
  if (r.shipyard)    subs.push(r.shipyard);
  if (r.period_start || r.period_end) {
    subs.push(`${(r.period_start||'').replace(/-/g,'.')} ~ ${(r.period_end||'').replace(/-/g,'.')}`);
  }
  $('#dde-subtitle').textContent = subs.join('   ·   ');

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
      el('div', { class: 'dde-toc-actions' },
        el('button', { class: 'dde-toc-btn', title: '위로',
          onclick: (e) => { e.stopPropagation(); moveSection(n.id, 'up'); }}, '↑'),
        el('button', { class: 'dde-toc-btn', title: '아래로',
          onclick: (e) => { e.stopPropagation(); moveSection(n.id, 'down'); }}, '↓'),
      ),
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
    // 빈 섹션 — 큰 안내 버튼 (4종 모두 선택 가능)
    blocksWrap.append(renderEmptyInserter());
    return;
  }

  // 블록이 있을 때
  // - 맨 위와 블록 사이는 인라인 inserter (호버형)
  // - 맨 마지막에는 항상 보이는 "+ 블록 추가" 영역
  blocksWrap.append(renderInserter(0));
  blocks.forEach((b, idx) => {
    blocksWrap.append(renderBlock(b, idx, blocks.length));
    if (idx < blocks.length - 1) {
      blocksWrap.append(renderInserter(idx + 1));
    }
  });
  blocksWrap.append(renderTailAdder(blocks.length));
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

  const controls = el('div', { class: 'dde-block-controls' },
    el('button', { class: 'dde-block-btn', title: '위로', disabled: idx === 0,
      onclick: () => moveBlock(b.id, 'up') }, '↑'),
    el('button', { class: 'dde-block-btn', title: '아래로', disabled: idx === total - 1,
      onclick: () => moveBlock(b.id, 'down') }, '↓'),
    el('button', { class: 'dde-block-btn dde-block-del', title: '삭제',
      onclick: () => deleteBlock(b.id) }, '✕'),
  );
  wrap.append(controls);

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
  autoResize(ta);
  ta.addEventListener('input', () => {
    autoResize(ta);
    scheduleBlockSave(b.id, () => ({ text: ta.value }));
  });
  body.append(ta);
}

function autoResize(ta) {
  ta.style.height = 'auto';
  ta.style.height = Math.max(ta.scrollHeight, 40) + 'px';
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

// 들여쓰기 레벨별 카운터로 마커 계산
function computeBulletMarkers(items, kind) {
  const markers = [];
  const counters = [0, 0, 0, 0];   // level 0~3
  for (const it of items) {
    const lv = Math.max(0, Math.min(MAX_INDENT, it.indent || 0));
    counters[lv]++;
    for (let i = lv + 1; i <= MAX_INDENT; i++) counters[i] = 0;
    if (kind === 'dash')        markers.push('–');
    else if (kind === 'number') markers.push(`${counters[lv]})`);
    else if (kind === 'alpha')  markers.push(`${String.fromCharCode(97 + (counters[lv] - 1) % 26)})`);
    else                        markers.push('•');
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
      const inp = el('input', {
        type: 'text', class: 'dde-bullet-input', placeholder: '항목...', value: it.text,
        oninput: (e) => {
          items[i].text = e.target.value;
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
          if (e.key === 'Enter') {
            e.preventDefault();
            // 새 항목은 현재 항목과 같은 indent
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
      setSaveStatus(`사진 ${files.length}장 업로드 중...`, 'busy');
      try {
        for (const f of files) {
          const fd = new FormData();
          fd.append('file', f);
          const res = await api(`/api/dock-reports/${E.reportId}/upload-image`, {
            method: 'POST', body: fd,
          });
          images.push({ filename: res.filename, url: res.url, caption: '' });
        }
        await api(`/api/dock-blocks/${b.id}`, {
          method: 'PUT', body: JSON.stringify({ content: getCurrent() }),
        });
        rebuild();
        setSaveStatus('저장됨', 'ok');
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
    alert('Word 추출은 Step 3에서 활성화됩니다.');
  });
  $('#dde-btn-export-pdf').addEventListener('click', () => {
    alert('PDF 추출은 Step 3에서 활성화됩니다.');
  });
}

init();
