// ════════════════════════════════════════════════════════════════
//  Dry Dock Report Editor — Step 2
//  · 목차 트리 관리 (1, 1-1, 1-1-1; 깊이 3단계)
//  · 4종 블록 인라인 편집: paragraph / bullet_list / table / image
//  · 자동 저장 (debounce 500ms)
// ════════════════════════════════════════════════════════════════
const E = {
  reportId: window.DDE_REPORT_ID,
  report: null,            // 메타 정보 (vessel_name, title, ...)
  sectionsFlat: [],        // DB에서 받은 평면 리스트
  tree: [],                // parent_id 기준으로 빌드한 트리
  byId: new Map(),         // id → {section, parent, depth, number(예 "1-2")}
  activeSecId: null,       // 우측에 표시 중인 섹션
  saveTimer: null,
  saveBusy: false,
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

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  }[c]));
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
//  Load
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

  // 상단 제목
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

  // 활성 섹션이 사라졌으면 초기화
  if (E.activeSecId && !E.byId.has(E.activeSecId)) {
    E.activeSecId = null;
  }
  // 처음 진입 + 섹션 있으면 첫 섹션 선택
  if (!E.activeSecId && E.tree.length > 0) {
    E.activeSecId = E.tree[0].id;
  }
  renderEditor();
}

function buildTree() {
  E.byId.clear();
  const map = new Map();
  for (const s of E.sectionsFlat) {
    map.set(s.id, { ...s, children: [] });
  }
  const roots = [];
  for (const s of map.values()) {
    if (s.parent_id && map.has(s.parent_id)) {
      map.get(s.parent_id).children.push(s);
    } else {
      roots.push(s);
    }
  }
  // 모든 children을 display_order로 정렬
  function sortRec(arr) {
    arr.sort((a, b) => a.display_order - b.display_order || a.id - b.id);
    for (const x of arr) sortRec(x.children);
  }
  sortRec(roots);

  // 번호링 (1, 1-1, 1-1-1) + byId map
  function walk(nodes, prefix = '', depth = 0, parent = null) {
    nodes.forEach((n, i) => {
      const num = prefix ? `${prefix}-${i + 1}` : `${i + 1}`;
      E.byId.set(n.id, {
        section: n, parent, depth, number: num,
        siblings: nodes,
        indexInSiblings: i,
      });
      walk(n.children, num, depth + 1, n);
    });
  }
  walk(roots);
  E.tree = roots;
}

// ─────────────────────────────────────────────────────────────
//  TOC (목차)
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

  // "하위 섹션 추가" 버튼 활성 여부: 활성 섹션이 있고, depth < 2 (최대 3단계)
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
        el('button', {
          class: 'dde-toc-btn',
          title: '위로',
          onclick: (e) => { e.stopPropagation(); moveSection(n.id, 'up'); },
        }, '↑'),
        el('button', {
          class: 'dde-toc-btn',
          title: '아래로',
          onclick: (e) => { e.stopPropagation(); moveSection(n.id, 'down'); },
        }, '↓'),
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
  } catch (e) {
    alert('섹션 추가 실패: ' + e.message);
  }
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
  } catch (e) {
    alert('삭제 실패: ' + e.message);
  }
}

function countDescendants(node) {
  let c = node.children.length;
  for (const ch of node.children) c += countDescendants(ch);
  return c;
}

async function moveSection(sid, direction) {
  try {
    await api(`/api/dock-sections/${sid}/move`, {
      method: 'POST',
      body: JSON.stringify({ direction }),
    });
    await loadReport();
  } catch (e) {
    alert('순서 변경 실패: ' + e.message);
  }
}

async function saveSectionTitle(sid, title) {
  if (!title.trim()) return;
  setSaveStatus('저장 중...', 'busy');
  try {
    await api(`/api/dock-sections/${sid}`, {
      method: 'PUT',
      body: JSON.stringify({ title: title.trim() }),
    });
    // 트리 즉시 업데이트 (전체 재로드 안 함 — focus 유지)
    const info = E.byId.get(sid);
    if (info) info.section.title = title.trim();
    renderTOC();
    setSaveStatus('저장됨', 'ok');
  } catch (e) {
    setSaveStatus('저장 실패: ' + e.message, 'err');
  }
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

  // 블록 목록 렌더링
  const blocksWrap = $('#dde-blocks');
  blocksWrap.innerHTML = '';
  const blocks = (sec.blocks || []).slice().sort((a, b) =>
    a.display_order - b.display_order || a.id - b.id
  );
  blocks.forEach((b, idx) => {
    blocksWrap.append(renderBlock(b, idx, blocks.length));
  });
}

function renderBlock(b, idx, total) {
  const wrap = el('div', { class: `dde-block dde-block-${b.block_type}`, 'data-id': b.id });

  // 블록 헤더 (타입 라벨 + 이동/삭제)
  const head = el('div', { class: 'dde-block-head' },
    el('span', { class: 'dde-block-type' }, blockTypeLabel(b.block_type)),
    el('span', { class: 'flex-spacer' }),
    el('button', {
      class: 'dde-block-btn', title: '위로', disabled: idx === 0,
      onclick: () => moveBlock(b.id, 'up'),
    }, '↑'),
    el('button', {
      class: 'dde-block-btn', title: '아래로', disabled: idx === total - 1,
      onclick: () => moveBlock(b.id, 'down'),
    }, '↓'),
    el('button', {
      class: 'dde-block-btn dde-block-del', title: '삭제',
      onclick: () => deleteBlock(b.id),
    }, '✕'),
  );
  wrap.append(head);

  // 본문
  const body = el('div', { class: 'dde-block-body' });
  if (b.block_type === 'paragraph')   renderParagraph(body, b);
  else if (b.block_type === 'bullet_list') renderBulletList(body, b);
  else if (b.block_type === 'table')  renderTable(body, b);
  else if (b.block_type === 'image')  renderImage(body, b);
  wrap.append(body);

  return wrap;
}

function blockTypeLabel(t) {
  return { paragraph: '텍스트', bullet_list: '불릿 리스트',
           table: '표', image: '사진' }[t] || t;
}

// — paragraph —
function renderParagraph(body, b) {
  const ta = el('textarea', {
    class: 'dde-p-input',
    placeholder: '내용을 입력하세요...',
    rows: 3,
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

// — bullet_list —
function renderBulletList(body, b) {
  const list = el('div', { class: 'dde-bullet-list' });
  const items = (b.content?.items || ['']).slice();

  function rebuild() {
    list.innerHTML = '';
    items.forEach((it, i) => {
      const row = el('div', { class: 'dde-bullet-row' });
      row.append(
        el('span', { class: 'dde-bullet-marker' }, '•'),
        el('input', {
          type: 'text',
          class: 'dde-bullet-input',
          placeholder: '항목...',
          value: it,
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
              if (prev) {
                prev.focus();
                prev.setSelectionRange(prev.value.length, prev.value.length);
              }
              scheduleBlockSave(b.id, () => ({ items: items.slice() }));
            }
          },
        }),
        el('button', {
          class: 'dde-bullet-x',
          type: 'button',
          title: '항목 삭제',
          onclick: () => {
            if (items.length <= 1) { items[0] = ''; }
            else items.splice(i, 1);
            rebuild();
            scheduleBlockSave(b.id, () => ({ items: items.slice() }));
          },
        }, '✕'),
      );
      list.append(row);
    });
  }
  rebuild();

  const addBtn = el('button', {
    class: 'btn btn-outline btn-sm dde-bullet-add',
    type: 'button',
    onclick: () => {
      items.push('');
      rebuild();
      const last = list.querySelectorAll('.dde-bullet-input');
      if (last.length) last[last.length - 1].focus();
      scheduleBlockSave(b.id, () => ({ items: items.slice() }));
    },
  }, '+ 항목');

  body.append(list, addBtn);
}

// — table —
function renderTable(body, b) {
  const c = b.content || { headers: ['항목', '내용'], rows: [['', '']] };
  const headers = c.headers || [];
  const rows = c.rows || [];

  const tblWrap = el('div', { class: 'dde-table-wrap' });

  function getCurrent() {
    return { headers: headers.slice(), rows: rows.map(r => r.slice()) };
  }

  function rebuild() {
    tblWrap.innerHTML = '';
    const tbl = el('table', { class: 'dde-table' });

    // header row
    const thead = el('thead');
    const trh = el('tr');
    headers.forEach((h, ci) => {
      const th = el('th');
      const inp = el('input', {
        type: 'text',
        class: 'dde-cell-input',
        value: h,
        placeholder: '헤더',
        oninput: (e) => {
          headers[ci] = e.target.value;
          scheduleBlockSave(b.id, getCurrent);
        },
      });
      // 열 삭제 버튼
      const delBtn = el('button', {
        class: 'dde-col-del',
        type: 'button',
        title: '열 삭제',
        onclick: () => {
          if (headers.length <= 1) { alert('최소 1개 열이 필요합니다.'); return; }
          headers.splice(ci, 1);
          rows.forEach(r => r.splice(ci, 1));
          rebuild();
          scheduleBlockSave(b.id, getCurrent);
        },
      }, '✕');
      th.append(inp, delBtn);
      trh.append(th);
    });
    // 우측 컨트롤 칼럼 (행 삭제 버튼용 자리 — 헤더에는 빈 셀)
    trh.append(el('th', { class: 'dde-tbl-ctrl-col' }));
    thead.append(trh);
    tbl.append(thead);

    // body rows
    const tbody = el('tbody');
    rows.forEach((row, ri) => {
      const tr = el('tr');
      row.forEach((cell, ci) => {
        const td = el('td');
        const ta = el('textarea', {
          class: 'dde-cell-textarea',
          rows: 1,
          value: cell,
          placeholder: '',
          oninput: (e) => {
            rows[ri][ci] = e.target.value;
            autoResize(e.target);
            scheduleBlockSave(b.id, getCurrent);
          },
        });
        ta.value = cell;
        // 초기 사이즈
        setTimeout(() => autoResize(ta), 0);
        td.append(ta);
        tr.append(td);
      });
      // 행 삭제
      const ctrl = el('td', { class: 'dde-tbl-ctrl-col' },
        el('button', {
          class: 'dde-row-del',
          type: 'button',
          title: '행 삭제',
          onclick: () => {
            if (rows.length <= 1) { alert('최소 1개 행이 필요합니다.'); return; }
            rows.splice(ri, 1);
            rebuild();
            scheduleBlockSave(b.id, getCurrent);
          },
        }, '✕')
      );
      tr.append(ctrl);
      tbody.append(tr);
    });
    tbl.append(tbody);

    tblWrap.append(tbl);
  }
  rebuild();

  const ctrls = el('div', { class: 'dde-table-ctrls' },
    el('button', {
      class: 'btn btn-outline btn-sm',
      type: 'button',
      onclick: () => {
        rows.push(headers.map(() => ''));
        rebuild();
        scheduleBlockSave(b.id, getCurrent);
      },
    }, '+ 행 추가'),
    el('button', {
      class: 'btn btn-outline btn-sm',
      type: 'button',
      onclick: () => {
        headers.push('');
        rows.forEach(r => r.push(''));
        rebuild();
        scheduleBlockSave(b.id, getCurrent);
      },
    }, '+ 열 추가'),
  );

  body.append(tblWrap, ctrls);
}

// — image —
function renderImage(body, b) {
  const c = b.content || {};
  const wrap = el('div', { class: 'dde-image-block' });

  function rebuild() {
    wrap.innerHTML = '';
    if (c.url) {
      const img = el('img', { class: 'dde-image-preview', src: c.url, alt: c.caption || '' });
      const captionInp = el('input', {
        type: 'text',
        class: 'dde-image-caption',
        placeholder: '캡션을 입력하세요 (선택)...',
        value: c.caption || '',
        oninput: (e) => {
          c.caption = e.target.value;
          scheduleBlockSave(b.id, () => ({
            filename: c.filename, url: c.url,
            caption: c.caption, width_pct: c.width_pct || 100
          }));
        },
      });
      const replaceBtn = el('button', {
        class: 'btn btn-outline btn-sm',
        type: 'button',
        onclick: () => triggerUpload(b.id, c),
      }, '이미지 교체');

      wrap.append(img, captionInp,
        el('div', { class: 'dde-image-actions' }, replaceBtn));
    } else {
      const dropZone = el('div', {
        class: 'dde-image-drop',
        onclick: () => triggerUpload(b.id, c),
      },
        el('div', { class: 'dde-image-drop-icon' }, '📷'),
        el('div', { class: 'dde-image-drop-text' }, '클릭해서 이미지 선택'),
        el('div', { class: 'dde-image-drop-hint' }, 'JPG, PNG, GIF, WebP, HEIC'),
      );
      wrap.append(dropZone);
    }
  }
  rebuild();
  // 외부에서 갱신 가능하도록 노출
  wrap._rebuildImage = rebuild;
  body.append(wrap);
}

function triggerUpload(blockId, content) {
  const inp = $('#dde-img-input');
  // 일회성 핸들러
  const onChange = async () => {
    inp.removeEventListener('change', onChange);
    const f = inp.files?.[0];
    inp.value = '';   // 리셋 (같은 파일 재선택 가능)
    if (!f) return;
    setSaveStatus('이미지 업로드 중...', 'busy');
    try {
      const fd = new FormData();
      fd.append('file', f);
      const res = await api(`/api/dock-reports/${E.reportId}/upload-image`, {
        method: 'POST', body: fd,
      });
      content.filename = res.filename;
      content.url = res.url;
      content.width_pct = content.width_pct || 100;
      // 저장 (즉시)
      await api(`/api/dock-blocks/${blockId}`, {
        method: 'PUT',
        body: JSON.stringify({ content: {
          filename: content.filename, url: content.url,
          caption: content.caption || '', width_pct: content.width_pct,
        }}),
      });
      setSaveStatus('저장됨', 'ok');
      // DOM 갱신: 해당 블록의 image-block만 새로 그리기
      const blockEl = $(`.dde-block[data-id="${blockId}"]`);
      if (blockEl) {
        const imgWrap = blockEl.querySelector('.dde-image-block');
        if (imgWrap && imgWrap._rebuildImage) imgWrap._rebuildImage();
      }
    } catch (e) {
      setSaveStatus('업로드 실패: ' + e.message, 'err');
      alert('이미지 업로드 실패: ' + e.message);
    }
  };
  inp.addEventListener('change', onChange);
  inp.click();
}

// ─────────────────────────────────────────────────────────────
//  Block save (debounced)
// ─────────────────────────────────────────────────────────────
function scheduleBlockSave(blockId, getContent) {
  clearTimeout(E.saveTimer);
  setSaveStatus('저장 대기...', 'busy');
  E.saveTimer = setTimeout(async () => {
    setSaveStatus('저장 중...', 'busy');
    try {
      await api(`/api/dock-blocks/${blockId}`, {
        method: 'PUT',
        body: JSON.stringify({ content: getContent() }),
      });
      // 메모리상 블록 content도 갱신 (다음 렌더에서 정확하게 보이도록)
      const info = E.byId.get(E.activeSecId);
      if (info) {
        const blocks = info.section.blocks || [];
        const target = blocks.find(b => b.id === blockId);
        if (target) target.content = getContent();
      }
      setSaveStatus('저장됨', 'ok');
    } catch (e) {
      setSaveStatus('저장 실패: ' + e.message, 'err');
    }
  }, 500);
}

async function addBlock(blockType) {
  if (!E.activeSecId) return;
  try {
    setSaveStatus('블록 추가 중...', 'busy');
    const res = await api(`/api/dock-sections/${E.activeSecId}/blocks`, {
      method: 'POST',
      body: JSON.stringify({ block_type: blockType }),
    });
    // 메모리 갱신 후 부분 렌더링
    const info = E.byId.get(E.activeSecId);
    if (info) {
      info.section.blocks = info.section.blocks || [];
      info.section.blocks.push({
        id: res.id, section_id: E.activeSecId,
        block_type: blockType, content: res.content,
        display_order: (info.section.blocks.reduce(
          (mx, b) => Math.max(mx, b.display_order), -1) + 1),
      });
    }
    renderEditor();
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
  } catch (e) {
    alert('삭제 실패: ' + e.message);
  }
}

async function moveBlock(bid, direction) {
  try {
    await api(`/api/dock-blocks/${bid}/move`, {
      method: 'POST',
      body: JSON.stringify({ direction }),
    });
    // display_order가 바뀌었으니 server에서 다시 fetch — 간단하게 전체 reload
    await loadReport();
  } catch (e) {
    alert('순서 변경 실패: ' + e.message);
  }
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

  // 섹션 제목 편집
  let titleTimer;
  $('#dde-section-title').addEventListener('input', (e) => {
    if (!E.activeSecId) return;
    clearTimeout(titleTimer);
    setSaveStatus('저장 대기...', 'busy');
    titleTimer = setTimeout(() => {
      saveSectionTitle(E.activeSecId, e.target.value);
    }, 500);
  });

  $('#dde-btn-del-section').addEventListener('click', () => {
    if (E.activeSecId) deleteSection(E.activeSecId);
  });

  // 블록 추가 버튼들
  $$('.dde-block-add [data-add-block]').forEach(btn => {
    btn.addEventListener('click', () => addBlock(btn.dataset.addBlock));
  });

  // Word / PDF (Step 3)
  $('#dde-btn-export-docx').addEventListener('click', () => {
    alert('Word 추출은 Step 3에서 활성화됩니다.');
  });
  $('#dde-btn-export-pdf').addEventListener('click', () => {
    alert('PDF 추출은 Step 3에서 활성화됩니다.');
  });
}

init();
