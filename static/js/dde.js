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

  // 블록이 있을 때 — 인라인 inserter
  blocksWrap.append(renderInserter(0));
  blocks.forEach((b, idx) => {
    blocksWrap.append(renderBlock(b, idx, blocks.length));
    blocksWrap.append(renderInserter(idx + 1));
  });
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
function renderBulletList(body, b) {
  const list = el('div', { class: 'dde-bullet-list' });
  const items = (b.content?.items || ['']).slice();

  function rebuild() {
    list.innerHTML = '';
    items.forEach((it, i) => {
      const row = el('div', { class: 'dde-bullet-row' });
      const inp = el('input', {
        type: 'text', class: 'dde-bullet-input', placeholder: '항목...', value: it,
        oninput: (e) => {
          items[i] = e.target.value;
          scheduleBlockSave(b.id, () => ({ items: items.slice() }));
        },
        onkeydown: (e) => {
          if (e.key === 'Enter') {
            e.preventDefault();
            items.splice(i + 1, 0, '');
            rebuild();
            const next = list.querySelectorAll('.dde-bullet-input')[i + 1];
            if (next) next.focus();
            scheduleBlockSave(b.id, () => ({ items: items.slice() }));
          } else if (e.key === 'Backspace' && !e.target.value && items.length > 1) {
            e.preventDefault();
            items.splice(i, 1);
            rebuild();
            const prev = list.querySelectorAll('.dde-bullet-input')[Math.max(0, i - 1)];
            if (prev) { prev.focus(); prev.setSelectionRange(prev.value.length, prev.value.length); }
            scheduleBlockSave(b.id, () => ({ items: items.slice() }));
          }
        },
      });
      row.append(
        el('span', { class: 'dde-bullet-marker' }, '•'), inp,
        el('button', { class: 'dde-bullet-x', type: 'button', title: '항목 삭제',
          onclick: () => {
            if (items.length <= 1) items[0] = '';
            else items.splice(i, 1);
            rebuild();
            scheduleBlockSave(b.id, () => ({ items: items.slice() }));
          }}, '✕'),
      );
      list.append(row);
    });
  }
  rebuild();

  const addBtn = el('button', {
    class: 'dde-bullet-add-link', type: 'button',
    onclick: () => {
      items.push('');
      rebuild();
      const last = list.querySelectorAll('.dde-bullet-input');
      if (last.length) last[last.length - 1].focus();
      scheduleBlockSave(b.id, () => ({ items: items.slice() }));
    },
  }, '+ 항목 추가');

  body.append(list, addBtn);
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
