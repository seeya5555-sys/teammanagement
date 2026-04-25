'use strict';

/* ═══════════════════════════════════════════════════════════════
   TRMT3  —  Daily 업무관리 (rev.3)
     · 셀 클릭 → 인라인 편집 (모달 X)
     · ✏ 편집 버튼    → 전체 편집 모달
     · 📎 첨부 버튼    → 첨부 전용 모달 (미리보기/다운로드/삭제)
     · 🗑 삭제 버튼
     · 인라인 추가 행 (툴바의 "+ 신규 이슈" or 각 날짜 그룹의 "+ 이 날짜로 추가")
   ═══════════════════════════════════════════════════════════════ */

// ───────────── State ─────────────
const S = {
  user:         window.TRMT?.user || {},
  supervisors:  [],
  vessels:      [],
  activeTab:    'all',
  issues:       [],
  filters: { q:'', vessel_id:'', vessel_type:'', status:'', priority:'' },

  editingId:      null,
  editingActions: [],

  collapsedMonths: new Set(),
  collapsedDates:  new Set(),
  expandedActions: new Set(),

  // 첨부 모달
  attachIssue:  null,

  // 인라인 추가
  inlineAdd:    null,      // { date, supervisor_id, vessel_id, item_topic, priority, status }
  _editing:     null,      // 현재 인라인 편집 중인 element (중복 방지)
};

// ───────────── Utils ─────────────
const $ = (sel, el = document) => el.querySelector(sel);

function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class')      e.className = v;
    else if (k === 'html')  e.innerHTML = v;
    else if (k.startsWith('on') && typeof v === 'function')
                             e.addEventListener(k.slice(2).toLowerCase(), v);
    else if (v === true)    e.setAttribute(k, '');
    else                    e.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    e.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
  return e;
}

function escHtml(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function todayISO() {
  const t = new Date();
  return t.toISOString().slice(0, 10);
}

function dDay(due) {
  if (!due) return null;
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const d = new Date(due + 'T00:00:00');
  return Math.round((d - today) / 86400000);
}
function dDayBadge(due) {
  const n = dDay(due);
  if (n === null) return null;
  let cls, txt;
  if (n < 0)       { cls = 'dday-overdue'; txt = `D${n}`; }
  else if (n === 0){ cls = 'dday-today';   txt = 'D-DAY'; }
  else if (n <= 3) { cls = 'dday-soon';    txt = `D+${n}`; }
  else             { cls = 'dday-later';   txt = `D+${n}`; }
  return el('span', { class: `dday ${cls}`, title: `마감: ${due}` }, txt);
}

const PRI_MAP = {
const PRI_MAP = {
  'COC & Flag': { cls: 'pri-cocflag', label: 'COC & Flag' },
  Urgent:       { cls: 'pri-urgent',  label: 'Urgent'     },
  Normal:       { cls: 'pri-normal',  label: 'Normal'     },
};
const STAT_MAP = {
  Open:       { cls: 'status-open', label: 'Open'   },
  InProgress: { cls: 'status-prog', label: '진행중' },
  Closed:     { cls: 'status-done', label: 'Closed' },
};
function priBadge(p) {
  const m = PRI_MAP[p] || PRI_MAP.Normal;
  return el('span', { class: `bd ${m.cls}` }, m.label);
}
function statBadge(s) {
  const m = STAT_MAP[s] || STAT_MAP.Open;
  return el('span', { class: `bd ${m.cls}` }, m.label);
}

function monthKey(s) { return s ? s.slice(0, 7) : '(미정)'; }

function groupByMonthAndDate(issues) {
  const months = new Map();
  for (const i of issues) {
    const mk = monthKey(i.issue_date);
    const dk = i.issue_date || '(미정)';
    if (!months.has(mk)) months.set(mk, new Map());
    const dayMap = months.get(mk);
    (dayMap.get(dk) || dayMap.set(dk, []).get(dk)).push(i);
  }
  return [...months.entries()].map(([month, dayMap]) => ({
    month,
    items: [...dayMap.entries()].map(([date, issues]) => ({ date, issues })),
  }));
}

function formatFileSize(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / 1048576).toFixed(1) + ' MB';
}

function isImageFile(name) {
  return /\.(jpe?g|png|gif|webp|heic|heif|bmp)$/i.test(name);
}

// ───────────── API ─────────────
async function api(url, opts = {}) {
  const isForm = opts.body instanceof FormData;
  const headers = isForm ? {} : { 'Content-Type': 'application/json' };
  const res = await fetch(url, {
    credentials: 'same-origin',
    headers: { ...headers, ...(opts.headers || {}) },
    ...opts,
  });
  if (res.status === 401) {
    location.href = '/login?next=' + encodeURIComponent(location.pathname);
    throw new Error('unauthorized');
  }
  const text = await res.text();
  let data; try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!res.ok) {
    const msg = data?.error || text || ('HTTP ' + res.status);
    throw new Error(msg);
  }
  return data;
}

async function loadSupervisors() { S.supervisors = await api('/api/supervisors'); }
async function loadVessels(supId) {
  const url = supId && supId !== 'all' ? `/api/vessels?supervisor_id=${supId}` : '/api/vessels';
  S.vessels = await api(url);
}
async function loadIssues() {
  const p = new URLSearchParams();
  if (S.activeTab !== 'all') p.set('supervisor_id', S.activeTab);
  if (S.filters.q)           p.set('q', S.filters.q);
  if (S.filters.vessel_id)   p.set('vessel_id', S.filters.vessel_id);
  if (S.filters.vessel_type) p.set('vessel_type', S.filters.vessel_type);
  if (S.filters.status)      p.set('status', S.filters.status);
  if (S.filters.priority)    p.set('priority', S.filters.priority);
  S.issues = await api('/api/issues?' + p);
}

// ───────────── Tabs / Filter / Summary ─────────────
function renderTabs() {
  const bar = $('#tab-bar');
  bar.innerHTML = '';
  const total = S.supervisors.reduce((a, s) => a + s.total, 0);
  bar.append(tabEl('all', '전체', 'gray', total, S.activeTab === 'all'));
  for (const s of S.supervisors) {
    bar.append(tabEl(s.id, s.name, s.color, s.total, S.activeTab == s.id));
  }
}
function tabEl(id, name, color, count, active) {
  const t = el('div', { class: 'tab' + (active ? ' active' : ''), 'data-id': id },
    el('span', { class: `tab-dot dot-${color}` }),
    name,
    el('span', { class: 'tab-count' }, count));
  t.addEventListener('click', () => switchTab(id));
  return t;
}
async function switchTab(id) {
  S.activeTab = id;
  S.inlineAdd = null;
  renderTabs();
  await loadVessels(id);
  renderVesselFilter();
  renderTabContext();
  await loadIssues();
  render();
}
function renderVesselFilter() {
  const sel = $('#filter-vessel');
  const cur = sel.value;
  sel.innerHTML = '';
  sel.append(el('option', { value: '' }, 'All 선박'));
  for (const v of S.vessels) sel.append(el('option', { value: v.id }, v.name));
  sel.value = S.vessels.find(v => v.id == cur) ? cur : '';
  S.filters.vessel_id = sel.value;
}
function renderTabContext() {
  const c = $('#tab-context');
  c.innerHTML = '';
  if (S.activeTab === 'all') {
    const open = S.supervisors.reduce((a,s)=>a+s.open_count, 0);
    const prog = S.supervisors.reduce((a,s)=>a+s.progress_count, 0);
    const done = S.supervisors.reduce((a,s)=>a+s.closed_count, 0);
    c.innerHTML = `전체 감독 · <strong>${S.supervisors.length}</strong>명 ·
                   Open <strong>${open}</strong> ·
                   진행중 <strong>${prog}</strong> ·
                   Closed <strong>${done}</strong>`;
    return;
  }
  const s = S.supervisors.find(x => x.id == S.activeTab);
  if (!s) return;

  const vesCount = (s.vessels || '').split(',').filter(x => x.trim()).length;
  const trigger = el('button', {
    class: 'myves-trigger',
    title: `${s.name} 담당 선박 상세 보기`,
    onclick: openMyVessels,
  },
    el('span', { class: 'ves-icon' }, '🛥'),
    `담당 선박 ${vesCount}척`,
    el('span', { class: 'caret' }, '▸'));
  c.append(trigger);
  c.append(el('span', { style: 'margin-left: 10px;' },
    '· Open ', el('strong', {}, String(s.open_count)),
    ' · 진행중 ', el('strong', {}, String(s.progress_count)),
    ' · Closed ', el('strong', {}, String(s.closed_count))
  ));
}
function renderSummary() {
  const n  = S.issues.length;
  const op = S.issues.filter(i => i.status === 'Open').length;
  const pg = S.issues.filter(i => i.status === 'InProgress').length;
  const cl = S.issues.filter(i => i.status === 'Closed').length;
  $('#summary-row').innerHTML = `
    <span>총 <strong>${n}</strong>건</span>
    <span>· Open <strong>${op}</strong></span>
    <span>· 진행중 <strong>${pg}</strong></span>
    <span>· Closed <strong>${cl}</strong></span>`;
  $('#count-label').textContent = `${n} items`;
}

// ───────────── Render — main ─────────────
function render() {
  const hasIssues = S.issues.length > 0;
  $('#empty-state').hidden = hasIssues || !!S.inlineAdd;
  renderTable();
  renderCards();
  renderSummary();
  updateToggleAllButton();
}

function renderTable() {
  const tbody = $('#issue-tbody');
  tbody.innerHTML = '';

  // 이슈가 없는데 인라인 추가만 있는 경우
  if (!S.issues.length) {
    if (S.inlineAdd) tbody.append(inlineAddRow());
    return;
  }

  const addDate = S.inlineAdd?.date;
  let addedInline = false;
  let no = 0;

  const groups = groupByMonthAndDate(S.issues);
  for (const mg of groups) {
    const mCollapsed = S.collapsedMonths.has(mg.month);
    const mTotalCnt = mg.items.reduce((a,d) => a + d.issues.length, 0);
    tbody.append(monthBarRow(mg.month, mCollapsed, mTotalCnt));
    if (mCollapsed) continue;

    for (const dg of mg.items) {
      const dCollapsed = S.collapsedDates.has(dg.date);
      tbody.append(dateBarRow(dg.date, dCollapsed, dg.issues.length));
      if (dCollapsed) continue;
      for (const i of dg.issues) {
        no++;
        tbody.append(rowEl(i, no));
      }
      // 인라인 추가 행 — 해당 날짜 그룹 "맨 아래"에
      if (S.inlineAdd && dg.date === addDate) {
        tbody.append(inlineAddRow());
        addedInline = true;
      }
    }
  }

  // 날짜 그룹에 없으면 (신규 날짜) — 최상단에 폴백
  if (S.inlineAdd && !addedInline) {
    tbody.insertBefore(inlineAddRow(), tbody.firstChild);
  }
}

function monthBarRow(month, collapsed, count) {
  const tr = el('tr', { class: 'month-bar' });
  const td = el('td', { colspan: '8' },
    el('div', { class: 'group-bar-inner' },
      el('span', { class: 'gb-caret' }, collapsed ? '▶' : '▼'),
      el('span', { class: 'gb-date' }, month),
      el('span', { class: 'gb-count' }, `${count} items`)));
  tr.append(td);
  tr.addEventListener('click', () => toggleMonth(month));
  return tr;
}

function dateBarRow(date, collapsed, count) {
  const tr = el('tr', { class: 'group-bar nested' });
  const inner = el('div', { class: 'group-bar-inner' },
    el('span', { class: 'gb-caret' }, collapsed ? '▶' : '▼'),
    el('span', { class: 'gb-date' }, date),
    el('span', { class: 'gb-count' }, `${count} item${count>1?'s':''}`));

  // + Add Issue 트리거
  const addBtn = el('span', {
    class: 'inline-add-trigger',
    title: `Add issue for ${date}`,
    onclick: (e) => {
      e.stopPropagation();
      openInlineAdd(date);
    },
  }, '+ Add Issue');
  inner.append(addBtn);

  const td = el('td', { colspan: '8' }, inner);
  tr.append(td);
  // 셀 전체 클릭 → 접기. 단, 트리거 버튼 클릭은 stopPropagation 덕에 무시
  tr.addEventListener('click', (e) => {
    if (e.target.closest('.inline-add-trigger')) return;
    toggleDate(date);
  });
  return tr;
}

function toggleMonth(m) {
  if (S.collapsedMonths.has(m)) S.collapsedMonths.delete(m);
  else S.collapsedMonths.add(m);
  renderTable(); renderCards();
}
function toggleDate(d) {
  if (S.collapsedDates.has(d)) S.collapsedDates.delete(d);
  else S.collapsedDates.add(d);
  renderTable(); renderCards();
}

// ───────────── Row 렌더 (셀별 인라인 편집) ─────────────
function rowEl(i, no) {
  const tr = el('tr', { class: 'data-row', 'data-id': i.id });

  // NO
  tr.append(el('td', { class: 'no-cell' }, String(no)));

  // 선박 — 클릭 시 select, 풀네임 + 공백 줄바꿈
  const vTd = el('td', { class: 'vessel-cell cell-edit', title: '클릭하여 선박 변경' },
    i.vessel_name);
  vTd.addEventListener('click', (ev) => {
    ev.stopPropagation();
    startEditVessel(vTd, i);
  });
  tr.append(vTd);

  // ITEM (topic)
  const topicTd = el('td', { class: 'topic-cell cell-edit', title: '클릭하여 제목 편집' });
  if (S.activeTab === 'all') {
    topicTd.append(
      el('div', { class: `sup-chip c-${i.supervisor_color}` },
        el('span', { class: `tab-dot dot-${i.supervisor_color}` }),
        i.supervisor_name),
      el('div', { class: 'topic-text' }, i.item_topic));
  } else {
    topicTd.append(el('div', { class: 'topic-text' }, i.item_topic));
  }
  topicTd.addEventListener('click', (ev) => {
    ev.stopPropagation();
    const target = topicTd.querySelector('.topic-text');
    startEditInline(target, i, 'item_topic', 'text');
  });
  tr.append(topicTd);

  // Description — textarea
  const descTd = el('td', { class: 'desc-cell cell-edit', title: '클릭하여 상세 편집' },
    i.description || '—');
  descTd.addEventListener('click', (ev) => {
    ev.stopPropagation();
    startEditInline(descTd, i, 'description', 'textarea');
  });
  tr.append(descTd);

  // Action Plan — 각 entry 별 편집
  tr.append(el('td', { class: 'action-cell' }, renderActionCell(i)));

  // Priority + D-day (priority 클릭 → select, due 클릭 → date input)
  const priTd = el('td', { class: 'cell-edit', title: '클릭하여 우선순위 / 마감일 편집' });
  const priStack = el('div', { class: 'pri-stack' }, priBadge(i.priority));
  const ddBd = dDayBadge(i.due_date);
  if (ddBd) priStack.append(ddBd); else {
    priStack.append(el('span', {
      class: 'dday dday-later',
      style: 'opacity:0.5; cursor:pointer',
      title: '마감일 설정',
    }, '+ 마감'));
  }
  priTd.append(priStack);
  priTd.addEventListener('click', (ev) => {
    ev.stopPropagation();
    // D-day 뱃지 클릭 → 마감일 편집
    if (ev.target.closest('.dday')) {
      startEditInline(priTd, i, 'due_date', 'date');
    } else {
      // 나머지 클릭 → 우선순위 select
      startEditSelect(priTd, i, 'priority', [
        ['Normal', 'Normal'], ['Urgent', 'Urgent'], ['COC & Flag', 'COC & Flag'],
      ]);
    }
  });
  tr.append(priTd);

  // Status
  const statTd = el('td', { class: 'cell-edit', title: '클릭하여 상태 변경' }, statBadge(i.status));
  statTd.addEventListener('click', (ev) => {
    ev.stopPropagation();
    startEditSelect(statTd, i, 'status', [
      ['Open', 'Open'], ['InProgress', '진행중'], ['Closed', 'Closed'],
    ]);
  });
  tr.append(statTd);

  // Actions (edit / attach / delete)
  const editBtn = mkIconBtn('edit', '전체 편집', () => openEdit(i.id));
  const attBtn  = mkIconBtn('attach', '첨부 관리', () => openAttach(i.id));
  if (i.att_count > 0) {
    attBtn.classList.add('has-attach');
    attBtn.append(el('span', { class: 'att-count-badge' }, String(i.att_count)));
  }
  const delBtn  = mkIconBtn('delete', '삭제', () => confirmDelete(i.id));

  tr.append(el('td', {}, el('div', { class: 'row-actions' }, editBtn, attBtn, delBtn)));
  return tr;
}

function mkIconBtn(kind, title, onclick) {
  const svg = {
    edit: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
      <path d="M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7"/>
      <path d="M18.5 2.5a2.121 2.121 0 013 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>`,
    attach: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
      <path d="M21.44 11.05l-9.19 9.19a6 6 0 01-8.49-8.49l9.19-9.19a4 4 0 015.66 5.66l-9.2 9.19a2 2 0 01-2.83-2.83l8.49-8.48"/></svg>`,
    delete: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
      <path d="M3 6h18"/><path d="M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2"/>
      <path d="M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6"/></svg>`,
  };
  const b = el('button', {
    class: 'icon-btn' + (kind === 'delete' ? ' danger' : kind === 'attach' ? ' attach' : ''),
    title,
    onclick: (ev) => { ev.stopPropagation(); onclick(); },
  });
  b.innerHTML = svg[kind];
  return b;
}

// ───────────── Action cell (entries + 인라인 편집) ─────────────
function renderActionCell(issue) {
  const list = Array.isArray(issue.actions) ? issue.actions : [];
  const expanded = S.expandedActions.has(issue.id);
  const showAll = list.length <= 1 || expanded;

  const wrap = el('div', { class: 'act-cell-wrap' });
  const entries = el('div', {
    class: 'act-entries' + (showAll ? '' : ' collapsed'),
  });

  if (!list.length) {
    entries.append(el('div', { class: 'act-empty', style: 'font-size:11px; color:var(--text-tertiary)' }, '—'));
  } else {
    for (let idx = 0; idx < list.length; idx++) {
      const a = list[idx];
      const entry = el('div', {
        class: 'act-entry' + (a.important ? ' important' : ''),
        'data-idx': idx,
      });

      // Arrow (접기/펼치기) — 2+이면 최신 entry에만, 펼치면 첫 entry에
      if (list.length > 1) {
        const shouldShowArrow = expanded ? (idx === 0) : (idx === list.length - 1);
        if (shouldShowArrow) {
          entry.append(el('span', {
            class: 'act-arrow',
            title: expanded ? '접기' : '모두 보기',
            onclick: (ev) => { ev.stopPropagation(); toggleActionExpand(issue.id); },
          }, expanded ? '▼' : '▶'));
        } else {
          entry.append(el('span', { class: 'act-arrow', style: 'visibility:hidden' }, '▶'));
        }
      }

      if (a.date) entry.append(el('span', { class: 'act-date' }, a.date));
      else        entry.append(el('span', { class: 'act-date', style: 'visibility:hidden' }, '-'));
      entry.append(el('span', { class: 'act-progress' }, a.progress || ''));

      // entry 본체(날짜/내용) 클릭 시 인라인 편집
      entry.addEventListener('click', (ev) => {
        if (ev.target.closest('.act-arrow')) return;
        ev.stopPropagation();
        startEditActionEntry(entry, issue, idx);
      });
      entries.append(entry);
    }
  }
  wrap.append(entries);

  // + 엔트리 추가 (항상 표시)
  wrap.append(el('button', {
    type: 'button',
    class: 'act-add-inline',
    title: '새 조치 엔트리 추가',
    onclick: (ev) => {
      ev.stopPropagation();
      addActionInline(issue);
    },
  }, '+ 추가'));

  return wrap;
}

function toggleActionExpand(issueId) {
  if (S.expandedActions.has(issueId)) S.expandedActions.delete(issueId);
  else S.expandedActions.add(issueId);
  renderTable(); renderCards();
}

// ───────────── 인라인 편집 — 공통 ─────────────
/** text / textarea / date 필드 인라인 편집 */
async function startEditInline(cellEl, issue, field, kind) {
  if (S._editing) return;
  S._editing = cellEl;
  const orig = issue[field] ?? '';
  const prevHTML = cellEl.innerHTML;

  let input;
  if (kind === 'textarea') {
    input = document.createElement('textarea');
    input.className = 'inline-textarea';
    input.value = orig || '';
    input.rows = Math.max(3, (orig.match(/\n/g) || []).length + 1);
  } else if (kind === 'date') {
    input = document.createElement('input');
    input.type = 'date';
    input.className = 'inline-input';
    input.value = orig || '';
  } else {
    input = document.createElement('input');
    input.type = 'text';
    input.className = 'inline-input';
    input.value = orig || '';
  }

  let done = false;
  const finish = async (save) => {
    if (done) return; done = true;
    S._editing = null;
    if (save) {
      const newVal = (kind === 'date' ? (input.value || null) : input.value);
      if (newVal !== orig && !(newVal === null && !orig)) {
        try {
          await api('/api/issues/' + issue.id, {
            method: 'PUT',
            body: JSON.stringify({ [field]: newVal }),
          });
          issue[field] = newVal;
          await reloadAll();
          return;
        } catch (err) {
          alert('저장 실패: ' + err.message);
        }
      }
    }
    cellEl.innerHTML = prevHTML;
  };

  // textarea 모드: 저장/취소 버튼 명시적 사용 (blur 자동저장 X)
  if (kind === 'textarea') {
    cellEl.innerHTML = '';
    const wrap = el('div', { class: 'inline-edit-wrap' });
    wrap.append(input);

    const saveBtn = el('button', { type: 'button', class: 'inline-save-btn' });
    saveBtn.innerHTML = `<svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2.5" style="display:inline-block;vertical-align:-1px;margin-right:2px">
      <polyline points="20 6 9 17 4 12"/></svg>저장`;
    saveBtn.addEventListener('click', (e) => { e.stopPropagation(); finish(true); });

    const cancelBtn = el('button', { type: 'button', class: 'inline-cancel-btn' }, '취소');
    cancelBtn.addEventListener('click', (e) => { e.stopPropagation(); finish(false); });

    wrap.append(el('div', { class: 'inline-edit-btns' }, saveBtn, cancelBtn));
    cellEl.append(wrap);
    input.focus();

    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); finish(true); }
      if (e.key === 'Escape') { e.preventDefault(); finish(false); }
    });
    return;   // textarea 모드는 여기서 끝 (blur 저장 사용 안 함)
  }

  // text / date 모드: blur 시 자동 저장
  cellEl.innerHTML = '';
  cellEl.append(input);
  input.focus();
  if (input.select) input.select();

  input.addEventListener('blur', () => finish(true));
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); finish(true); }
    if (e.key === 'Escape') {
      done = true; S._editing = null;
      cellEl.innerHTML = prevHTML;
    }
  });
}

/** select 인라인 편집 */
async function startEditSelect(cellEl, issue, field, options) {
  if (S._editing) return;
  S._editing = cellEl;
  const orig = issue[field] ?? '';

  const sel = document.createElement('select');
  sel.className = 'inline-select';
  for (const [v, label] of options) {
    const opt = document.createElement('option');
    opt.value = v; opt.textContent = label;
    if (v === orig) opt.selected = true;
    sel.append(opt);
  }
  const prevHTML = cellEl.innerHTML;
  cellEl.innerHTML = '';
  cellEl.append(sel);
  sel.focus();

  let done = false;
  const finish = async (save) => {
    if (done) return; done = true;
    S._editing = null;
    if (save && sel.value !== orig) {
      try {
        await api('/api/issues/' + issue.id, {
          method: 'PUT',
          body: JSON.stringify({ [field]: sel.value }),
        });
        issue[field] = sel.value;
        await reloadAll();
        return;
      } catch (err) { alert('저장 실패: ' + err.message); }
    }
    cellEl.innerHTML = prevHTML;
  };
  sel.addEventListener('change', () => finish(true));
  sel.addEventListener('blur', () => setTimeout(() => finish(true), 80));
  sel.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { done = true; S._editing = null; cellEl.innerHTML = prevHTML; }
  });
}

/** 선박 select — 감독 담당 선박만 */
async function startEditVessel(cellEl, issue) {
  if (S._editing) return;
  try {
    const vs = await api(`/api/vessels?supervisor_id=${issue.supervisor_id}`);
    const opts = vs.map(v => [v.id, v.short_name || v.name]);
    await startEditSelect(cellEl, issue, 'vessel_id', opts);
  } catch (err) { alert('선박 목록 로드 실패: ' + err.message); }
}

// ───────────── Action entry 인라인 편집 ─────────────
function startEditActionEntry(entryEl, issue, idx) {
  if (S._editing) return;
  S._editing = entryEl;

  const a = issue.actions[idx] || { date: '', progress: '', important: false };
  const orig = { date: a.date || '', progress: a.progress || '', important: !!a.important };
  let imp = orig.important;

  entryEl.innerHTML = '';
  entryEl.classList.add('editing');
  entryEl.classList.remove('important');

  const dateIn = el('input', { type: 'date', value: orig.date });
  const progIn = el('input', { type: 'text', value: orig.progress, placeholder: '조치 내용' });
  const impBtn = el('button', {
    type: 'button', class: 'mini-btn imp' + (imp ? ' on' : ''),
    title: '중요 표시', onclick: (ev) => {
      ev.stopPropagation();
      imp = !imp;
      impBtn.classList.toggle('on', imp);
      impBtn.textContent = imp ? '●' : '○';
    },
  }, imp ? '●' : '○');
  const okBtn = el('button', { type: 'button', class: 'mini-btn ok', title: '저장',
    onclick: (ev) => { ev.stopPropagation(); finish('save'); } }, '✓');
  const rmBtn = el('button', { type: 'button', class: 'mini-btn rm', title: '엔트리 삭제',
    onclick: (ev) => { ev.stopPropagation(); finish('remove'); } }, '×');

  entryEl.append(dateIn, progIn, impBtn, okBtn, rmBtn);
  setTimeout(() => { progIn.focus(); progIn.select(); }, 10);

  let done = false;
  const finish = async (mode) => {
    if (done) return; done = true;
    S._editing = null;

    if (mode === 'save') {
      const progVal = progIn.value.trim();
      if (!progVal) {       // 내용 비어있으면 삭제로 처리
        mode = 'remove';
      } else {
        issue.actions[idx] = {
          date: dateIn.value || null,
          progress: progVal,
          important: imp,
        };
      }
    }
    if (mode === 'remove') {
      issue.actions.splice(idx, 1);
    }

    if (mode === 'cancel') {
      renderTable(); renderCards();
      return;
    }

    try {
      await api('/api/issues/' + issue.id, {
        method: 'PUT',
        body: JSON.stringify({ actions: issue.actions }),
      });
      renderTable(); renderCards();
    } catch (err) {
      alert('저장 실패: ' + err.message);
      await reloadAll();
    }
  };

  progIn.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); finish('save'); }
    if (e.key === 'Escape') finish('cancel');
  });
  dateIn.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); finish('save'); }
    if (e.key === 'Escape') finish('cancel');
  });
}

async function addActionInline(issue) {
  if (S._editing) return;
  if (!Array.isArray(issue.actions)) issue.actions = [];
  // 임시 빈 entry 추가 후 그 entry 편집 진입
  issue.actions.push({ date: todayISO(), progress: '', important: false });
  if (!S.expandedActions.has(issue.id)) S.expandedActions.add(issue.id);
  renderTable(); renderCards();

  setTimeout(() => {
    const tr = document.querySelector(`tr[data-id="${issue.id}"]`);
    if (!tr) return;
    const entries = tr.querySelectorAll('.act-cell-wrap .act-entry');
    const last = entries[entries.length - 1];
    if (last) startEditActionEntry(last, issue, issue.actions.length - 1);
  }, 30);
}

// ───────────── Cards (모바일) ─────────────
function renderCards() {
  const list = $('#card-list');
  list.innerHTML = '';

  if (!S.issues.length) {
    if (S.inlineAdd) list.append(inlineAddCardHint());
    return;
  }

  const addDate = S.inlineAdd?.date;
  let addedInline = false;

  const groups = groupByMonthAndDate(S.issues);
  for (const mg of groups) {
    const mCollapsed = S.collapsedMonths.has(mg.month);
    const totalCnt = mg.items.reduce((a,d) => a + d.issues.length, 0);
    const mBar = el('div', {
      class: 'card-date-bar',
      style: 'background:#0F172A; font-size:13px; font-weight:700',
    },
      el('span', {}, mCollapsed ? '▶' : '▼'),
      el('span', {}, mg.month),
      el('span', { style: 'opacity:0.7' }, `${totalCnt} items`));
    mBar.addEventListener('click', () => toggleMonth(mg.month));
    list.append(mBar);
    if (mCollapsed) continue;

    for (const dg of mg.items) {
      const dCollapsed = S.collapsedDates.has(dg.date);
      const dBar = el('div', {
        class: 'card-date-bar',
        style: 'background:#1E293B; margin-left:12px',
      },
        el('span', {}, dCollapsed ? '▶' : '▼'),
        el('span', {}, dg.date),
        el('span', { style: 'opacity:0.7' }, `${dg.issues.length} item${dg.issues.length>1?'s':''}`));
      dBar.addEventListener('click', () => toggleDate(dg.date));
      list.append(dBar);
      if (dCollapsed) continue;
      for (const i of dg.issues) list.append(cardEl(i));

      if (S.inlineAdd && dg.date === addDate) {
        list.append(inlineAddCardHint());
        addedInline = true;
      }
    }
  }

  if (S.inlineAdd && !addedInline) {
    list.insertBefore(inlineAddCardHint(), list.firstChild);
  }
}

function inlineAddCardHint() {
  return el('div', {
    style: 'background:var(--blue-bg); border:1px solid var(--blue-border); padding:10px 12px; border-radius:8px; font-size:12px; color:var(--blue-text); margin-bottom:10px',
  }, '📝 데스크톱에서 상단 인라인 입력 폼을 이용해 새 이슈를 추가하세요.');
}

function cardEl(i) {
  const card = el('div', { class: 'issue-card', 'data-id': i.id });
  // 카드 click → edit 모달 (모바일은 인라인 편집 어려우므로 모달로)
  card.addEventListener('click', (ev) => {
    if (ev.target.closest('.icon-btn') || ev.target.closest('.act-arrow')) return;
    openEdit(i.id);
  });

  const head = el('div', { class: 'issue-card-head' });
  if (S.activeTab === 'all') {
    head.append(el('span', { class: `sup-chip c-${i.supervisor_color}` },
      el('span', { class: `tab-dot dot-${i.supervisor_color}` }),
      i.supervisor_name));
  }
  head.append(el('span', { class: 'vessel-cell' }, i.vessel_name));
  head.append(priBadge(i.priority));
  const dd = dDayBadge(i.due_date);
  if (dd) head.append(dd);
  head.append(statBadge(i.status));
  card.append(head);

  const body = el('div', { class: 'issue-card-body' },
    el('div', { class: 'issue-card-title' }, i.item_topic));
  if (i.description) body.append(el('div', { class: 'issue-card-desc' }, i.description));
  const actions = Array.isArray(i.actions) ? i.actions : [];
  if (actions.length) {
    body.append(el('div', { class: 'issue-card-action' }, renderActionCell(i)));
  }
  card.append(body);

  const foot = el('div', { class: 'issue-card-foot' });
  const editBtn = mkIconBtn('edit', '편집', () => openEdit(i.id));
  const attBtn  = mkIconBtn('attach', '첨부', () => openAttach(i.id));
  if (i.att_count > 0) {
    attBtn.classList.add('has-attach');
    attBtn.append(el('span', { class: 'att-count-badge' }, String(i.att_count)));
  }
  const delBtn  = mkIconBtn('delete', '삭제', () => confirmDelete(i.id));
  foot.append(editBtn, attBtn, delBtn);
  card.append(foot);
  return card;
}

// ───────────── Toggle All ─────────────
function getAllMonths() {
  return [...new Set(S.issues.map(i => monthKey(i.issue_date)))];
}
function getAllDates() {
  return [...new Set(S.issues.map(i => i.issue_date || '(미정)'))];
}
function isAllCollapsed() {
  const ms = getAllMonths();
  return ms.length > 0 && ms.every(m => S.collapsedMonths.has(m));
}
function updateToggleAllButton() {
  const collapsed = isAllCollapsed();
  $('#toggle-all-icon').textContent  = collapsed ? '▶' : '▼';
  $('#toggle-all-label').textContent = collapsed ? '전체 펼치기' : '전체 접기';
}
function toggleAll() {
  if (isAllCollapsed()) {
    S.collapsedMonths.clear();
    S.collapsedDates.clear();
  } else {
    getAllMonths().forEach(m => S.collapsedMonths.add(m));
    getAllDates().forEach(d => S.collapsedDates.add(d));
  }
  renderTable(); renderCards(); updateToggleAllButton();
}

// ═══════════════════════════════════════════════════════════
//  Inline Add (새 이슈 인라인 입력 행)
// ═══════════════════════════════════════════════════════════
function openInlineAdd(date = null) {
  S.inlineAdd = {
    date: date || todayISO(),
    supervisor_id: S.activeTab === 'all'
      ? (S.user.supervisor_id || (S.supervisors[0] && S.supervisors[0].id))
      : S.activeTab,
    vessel_id: null,
    item_topic: '',
    priority: 'Normal',
    status: 'Open',
  };
  renderTable(); renderCards();
  setTimeout(() => {
    const input = document.querySelector('.inline-add-row .ins-topic');
    input?.focus();
  }, 30);
}

function cancelInlineAdd() {
  S.inlineAdd = null;
  renderTable(); renderCards();
}

async function saveInlineAdd() {
  const add = S.inlineAdd;
  if (!add.item_topic.trim()) {
    alert('제목을 입력하세요.');
    document.querySelector('.inline-add-row .ins-topic')?.focus();
    return;
  }
  if (!add.vessel_id) {
    alert('선박을 선택하세요.');
    return;
  }
  try {
    await api('/api/issues', {
      method: 'POST',
      body: JSON.stringify({
        supervisor_id: add.supervisor_id,
        vessel_id:     add.vessel_id,
        issue_date:    add.date,
        item_topic:    add.item_topic.trim(),
        description:   '',
        actions:       [],
        priority:      add.priority,
        status:        add.status,
      }),
    });
    S.inlineAdd = null;
    await reloadAll();
  } catch (err) {
    alert('저장 실패: ' + err.message);
  }
}

function inlineAddRow() {
  const add = S.inlineAdd;
  const tr = el('tr', { class: 'inline-add-row' });

  // NO
  tr.append(el('td', { class: 'ins-num' }, '+'));

  // 선박 select
  const vSel = el('select', { class: 'inline-select' });
  vSel.append(el('option', { value: '' }, '선박 선택...'));
  vSel.addEventListener('change', (e) => {
    add.vessel_id = Number(e.target.value) || null;
  });
  // 비동기로 선박 옵션 로드
  loadVesselsForSupervisor(add.supervisor_id).then(vs => {
    vSel.innerHTML = '';
    vSel.append(el('option', { value: '' }, '선박 선택...'));
    for (const v of vs) vSel.append(el('option', { value: v.id }, v.short_name || v.name));
    if (add.vessel_id) vSel.value = add.vessel_id;
  });
  tr.append(el('td', {}, vSel));

  // ITEM cell — 감독 select + 제목 input
  const topicTd = el('td');
  if (S.activeTab === 'all') {
    const supSel = el('select', { class: 'inline-select', style: 'margin-bottom:4px; display:block; width:100%' });
    for (const s of S.supervisors) {
      supSel.append(el('option', { value: s.id }, s.name));
    }
    supSel.value = add.supervisor_id;
    supSel.addEventListener('change', async (e) => {
      add.supervisor_id = Number(e.target.value);
      add.vessel_id = null;
      const vs = await loadVesselsForSupervisor(add.supervisor_id);
      vSel.innerHTML = '';
      vSel.append(el('option', { value: '' }, '선박 선택...'));
      for (const v of vs) vSel.append(el('option', { value: v.id }, v.short_name || v.name));
    });
    topicTd.append(supSel);
  }
  const topicIn = el('input', {
    type: 'text', class: 'inline-input ins-topic',
    placeholder: '이슈 제목 입력...',
    value: add.item_topic,
  });
  topicIn.addEventListener('input', (e) => { add.item_topic = e.target.value; });
  topicIn.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); saveInlineAdd(); }
    if (e.key === 'Escape') cancelInlineAdd();
  });
  topicTd.append(topicIn);
  tr.append(topicTd);

  // DESC / ACTION placeholder
  tr.append(el('td', { class: 'desc-cell' },
    el('span', { class: 'ins-placeholder' }, '저장 후 클릭하여 추가')));
  tr.append(el('td', { class: 'action-cell' },
    el('span', { class: 'ins-placeholder' }, '저장 후 +추가 가능')));

  // Priority
  const priSel = el('select', { class: 'inline-select' });
  for (const [v, l] of [['Normal','Normal'], ['Urgent','Urgent'], ['COC & Flag','COC & Flag']]) {
    priSel.append(el('option', { value: v }, l));
  }
  priSel.value = add.priority;
  priSel.addEventListener('change', (e) => { add.priority = e.target.value; });
  tr.append(el('td', {}, priSel));

  // Status
  const statSel = el('select', { class: 'inline-select' });
  for (const [v, l] of [['Open','Open'], ['InProgress','진행중'], ['Closed','Closed']]) {
    statSel.append(el('option', { value: v }, l));
  }
  statSel.value = add.status;
  statSel.addEventListener('change', (e) => { add.status = e.target.value; });
  tr.append(el('td', {}, statSel));

  // Actions: ✓ 저장 / × 취소
  const okBtn = el('button', {
    class: 'icon-btn ok', title: '저장 (Enter)',
    onclick: saveInlineAdd,
  });
  okBtn.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
    <polyline points="20 6 9 17 4 12"/></svg>`;
  const cancelBtn = el('button', {
    class: 'icon-btn', title: '취소 (Esc)',
    onclick: cancelInlineAdd,
  });
  cancelBtn.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
    <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>`;
  tr.append(el('td', {}, el('div', { class: 'row-actions' }, okBtn, cancelBtn)));

  return tr;
}

// 간단한 vessel cache
const _vesselCache = new Map();
async function loadVesselsForSupervisor(supId) {
  if (!supId) return [];
  if (_vesselCache.has(supId)) return _vesselCache.get(supId);
  const vs = await api(`/api/vessels?supervisor_id=${supId}`);
  _vesselCache.set(supId, vs);
  return vs;
}

// ═══════════════════════════════════════════════════════════
//  Edit Modal (✏ 버튼 — 전체 편집)
// ═══════════════════════════════════════════════════════════
function fillFormSelects() {
  const sup = $('#f-supervisor');
  sup.innerHTML = '';
  for (const s of S.supervisors) {
    sup.append(el('option', { value: s.id }, s.name));
  }
  refillVesselSelect(S.activeTab === 'all' ? null : S.activeTab);
}

async function refillVesselSelect(supervisorId) {
  const vs = await loadVesselsForSupervisor(supervisorId);
  const v = $('#f-vessel');
  const cur = v.value;
  v.innerHTML = '';
  for (const vv of vs) v.append(el('option', { value: vv.id }, vv.name));
  if (vs.find(x => x.id == cur)) v.value = cur;
}

function renderActionEditor() {
  const box = $('#f-action-editor');
  box.innerHTML = '';
  if (!S.editingActions.length) return;

  S.editingActions.forEach((a, idx) => {
    const row = el('div', { class: 'act-edit-row' });
    const dateIn = el('input', {
      type: 'date', value: a.date || '',
      onchange: (e) => { S.editingActions[idx].date = e.target.value; },
    });
    const progIn = el('input', {
      type: 'text', value: a.progress || '',
      placeholder: '조치 / 팔로우업 내용',
      oninput: (e) => { S.editingActions[idx].progress = e.target.value; },
    });
    const impBtn = el('button', {
      type: 'button',
      class: 'imp-toggle' + (a.important ? ' on' : ''),
      title: '중요 표시',
      onclick: () => {
        S.editingActions[idx].important = !S.editingActions[idx].important;
        renderActionEditor();
      },
    }, a.important ? '● 중요' : '○ 중요');
    const rmBtn = el('button', {
      type: 'button', class: 'act-remove', title: '엔트리 삭제',
      onclick: () => {
        S.editingActions.splice(idx, 1);
        renderActionEditor();
      },
    }, '×');
    row.append(dateIn, progIn, impBtn, rmBtn);
    box.append(row);
  });
}

function addActionEntry() {
  S.editingActions.push({ date: todayISO(), progress: '', important: false });
  renderActionEditor();
  const rows = $('#f-action-editor').querySelectorAll('.act-edit-row');
  const last = rows[rows.length - 1];
  last?.querySelector('input[type="text"]')?.focus();
}

function openNew() {
  S.editingId = null;
  S.editingActions = [];
  $('#modal-title').textContent = '신규 이슈';
  $('#btn-delete').hidden = true;

  $('#f-id').value       = '';
  $('#f-topic').value    = '';
  $('#f-desc').value     = '';
  $('#f-priority').value = 'Normal';
  $('#f-status').value   = 'Open';
  $('#f-issue-date').value = todayISO();
  $('#f-due-date').value   = '';

  // 감독: 현재 탭 기준 (전체 탭이면 본인 감독 or 첫 감독)
  let supId = S.activeTab !== 'all' ? S.activeTab
            : (S.user.supervisor_id || (S.supervisors[0] && S.supervisors[0].id));
  if (supId) {
    $('#f-supervisor').value = supId;
    refillVesselSelect(supId);
  }

  renderActionEditor();
  showModal();
}

async function openEdit(iid) {
  try {
    const i = await api('/api/issues/' + iid);
    S.editingId = iid;
    S.editingActions = Array.isArray(i.actions)
      ? JSON.parse(JSON.stringify(i.actions))
      : [];

    $('#modal-title').textContent = `이슈 #${iid} 편집`;
    $('#btn-delete').hidden = false;

    $('#f-id').value       = i.id;
    $('#f-supervisor').value = i.supervisor_id;
    await refillVesselSelect(i.supervisor_id);
    $('#f-vessel').value   = i.vessel_id;
    $('#f-issue-date').value = i.issue_date;
    $('#f-due-date').value = i.due_date || '';
    $('#f-priority').value = i.priority;
    $('#f-status').value   = i.status;
    $('#f-topic').value    = i.item_topic;
    $('#f-desc').value     = i.description || '';

    renderActionEditor();
    showModal();
  } catch (err) {
    alert('이슈 로드 실패: ' + err.message);
  }
}

function showModal() { $('#issue-modal').hidden = false; document.body.style.overflow = 'hidden'; }
function closeModal() { $('#issue-modal').hidden = true; document.body.style.overflow = ''; }

async function saveIssue(ev) {
  ev.preventDefault();
  const cleanActions = S.editingActions
    .filter(a => (a.progress || '').trim() !== '')
    .map(a => ({
      date: (a.date || '').trim() || null,
      progress: (a.progress || '').trim(),
      important: !!a.important,
    }));

  const payload = {
    supervisor_id: Number($('#f-supervisor').value),
    vessel_id:     Number($('#f-vessel').value),
    issue_date:    $('#f-issue-date').value,
    due_date:      $('#f-due-date').value || null,
    item_topic:    $('#f-topic').value.trim(),
    description:   $('#f-desc').value,
    actions:       cleanActions,
    priority:      $('#f-priority').value,
    status:        $('#f-status').value,
  };
  if (!payload.item_topic) { alert('제목을 입력하세요.'); return; }
  if (!payload.vessel_id)  { alert('선박을 선택하세요.'); return; }

  try {
    if (S.editingId) {
      await api('/api/issues/' + S.editingId, {
        method: 'PUT',
        body: JSON.stringify(payload),
      });
    } else {
      await api('/api/issues', {
        method: 'POST',
        body: JSON.stringify(payload),
      });
    }
    closeModal();
    await reloadAll();
  } catch (err) {
    alert('저장 실패: ' + err.message);
  }
}

async function confirmDelete(iid) {
  if (!confirm(`이슈 #${iid}를 삭제하시겠습니까?\n첨부 파일도 모두 삭제됩니다.`)) return;
  try {
    await api('/api/issues/' + iid, { method: 'DELETE' });
    if (S.editingId === iid) closeModal();
    if (S.attachIssue?.id === iid) closeAttach();
    await reloadAll();
  } catch (err) { alert('삭제 실패: ' + err.message); }
}

// ═══════════════════════════════════════════════════════════
//  Attach Modal (📎 버튼)
// ═══════════════════════════════════════════════════════════
async function openAttach(iid) {
  try {
    const i = await api('/api/issues/' + iid);
    S.attachIssue = { id: iid, topic: i.item_topic, attachments: i.attachments || [] };
    $('#attach-issue-id').textContent = iid;
    $('#attach-issue-topic').textContent = i.item_topic;
    renderAttachGrid();
    $('#attach-modal').hidden = false;
    document.body.style.overflow = 'hidden';
  } catch (err) { alert('첨부 로드 실패: ' + err.message); }
}

async function closeAttach() {
  S.attachIssue = null;
  $('#attach-modal').hidden = true;
  document.body.style.overflow = '';
  // 리스트의 첨부 카운트 뱃지 업데이트
  await reloadAll();
}

function renderAttachGrid() {
  const grid = $('#attach-grid');
  grid.innerHTML = '';
  if (!S.attachIssue.attachments.length) {
    grid.append(el('div', { class: 'attach-empty' }, '첨부 파일이 없습니다. 위 영역으로 파일을 드래그하거나 클릭해 업로드하세요.'));
    return;
  }
  for (const a of S.attachIssue.attachments) {
    grid.append(attachItemEl(a));
  }
}

function attachItemEl(a) {
  const item = el('div', { class: 'attach-item' });

  const thumb = el('div', { class: 'attach-thumb' });
  if (isImageFile(a.filename)) {
    thumb.append(el('img', {
      src: `/api/attachments/${a.id}?inline=1`,
      alt: a.filename, loading: 'lazy',
    }));
  } else {
    thumb.append(fileIcon(a.filename));
  }
  item.append(thumb);

  item.append(el('div', { class: 'attach-name', title: a.filename }, a.filename));
  item.append(el('div', { class: 'attach-meta' },
    `${formatFileSize(a.file_size || 0)} · ${(a.uploaded_at || '').slice(0, 10)}`));

  const actions = el('div', { class: 'attach-actions' });
  // 미리보기 (이미지 + PDF)
  if (isImageFile(a.filename) || /\.pdf$/i.test(a.filename)) {
    const prevBtn = el('button', {
      class: 'icon-btn', title: '미리보기 (새 탭)',
      onclick: () => window.open(`/api/attachments/${a.id}?inline=1`, '_blank'),
    });
    prevBtn.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
      <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/>
      <circle cx="12" cy="12" r="3"/></svg>`;
    actions.append(prevBtn);
  }
  // 다운로드
  const dlLink = el('a', {
    class: 'icon-btn', title: '다운로드',
    href: `/api/attachments/${a.id}`,
  });
  dlLink.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
    <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/>
    <polyline points="7 10 12 15 17 10"/>
    <line x1="12" y1="15" x2="12" y2="3"/></svg>`;
  actions.append(dlLink);
  // 삭제
  const delBtn = el('button', {
    class: 'icon-btn danger', title: '삭제',
    onclick: () => deleteAttach(a.id),
  });
  delBtn.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
    <path d="M3 6h18"/><path d="M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2"/>
    <path d="M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6"/></svg>`;
  actions.append(delBtn);
  item.append(actions);

  return item;
}

function fileIcon(filename) {
  const ext = (filename.split('.').pop() || '').toLowerCase();
  let icon = '📄';
  if (ext === 'pdf') icon = '📕';
  else if (['doc','docx','rtf'].includes(ext)) icon = '📘';
  else if (['xls','xlsx','csv'].includes(ext))  icon = '📗';
  else if (['ppt','pptx'].includes(ext))         icon = '📙';
  else if (['zip','rar','7z'].includes(ext))     icon = '🗜';
  else if (['txt','md','log'].includes(ext))     icon = '📝';

  return el('div', { class: 'attach-fileicon' },
    el('span', { style: 'font-size:40px; line-height:1' }, icon),
    el('span', { style: 'font-size:10px; color:var(--text-tertiary); text-transform:uppercase; margin-top:4px; font-weight:600' }, ext || 'FILE'));
}

async function deleteAttach(aid) {
  if (!confirm('이 첨부파일을 삭제하시겠습니까?')) return;
  try {
    await api('/api/attachments/' + aid, { method: 'DELETE' });
    S.attachIssue.attachments = S.attachIssue.attachments.filter(a => a.id !== aid);
    renderAttachGrid();
  } catch (err) { alert('삭제 실패: ' + err.message); }
}

async function uploadAttachFile(file) {
  if (!S.attachIssue) return;
  const fd = new FormData();
  fd.append('file', file);
  try {
    const a = await api(`/api/issues/${S.attachIssue.id}/attachments`, {
      method: 'POST', body: fd,
    });
    S.attachIssue.attachments.push({
      id: a.id, filename: a.filename,
      stored_name: a.stored_name, file_size: a.file_size,
      uploaded_at: new Date().toISOString().slice(0, 19).replace('T', ' '),
    });
    renderAttachGrid();
  } catch (err) { alert(`업로드 실패 (${file.name}): ` + err.message); }
}

async function uploadAttachFiles(files) {
  for (const f of files) {
    await uploadAttachFile(f);
  }
}

// ═══════════════════════════════════════════════════════════
//  User Menu Dropdown (비밀번호 변경 / 로그아웃)
// ═══════════════════════════════════════════════════════════
function toggleUserMenu(force) {
  const dd = $('#user-dropdown');
  const show = force !== undefined ? force : dd.hidden;
  dd.hidden = !show;
}

// ═══════════════════════════════════════════════════════════
//  Password Change Modal
// ═══════════════════════════════════════════════════════════
function openPasswordModal() {
  $('#pw-old').value = '';
  $('#pw-new').value = '';
  $('#pw-new2').value = '';
  $('#password-modal').hidden = false;
  document.body.style.overflow = 'hidden';
  setTimeout(() => $('#pw-old').focus(), 40);
}
function closePasswordModal() {
  $('#password-modal').hidden = true;
  document.body.style.overflow = '';
}
async function submitPasswordChange(ev) {
  ev.preventDefault();
  const oldP = $('#pw-old').value;
  const newP = $('#pw-new').value;
  const new2 = $('#pw-new2').value;
  if (newP.length < 6) { alert('새 비밀번호는 6자 이상이어야 합니다.'); return; }
  if (newP !== new2)   { alert('새 비밀번호 확인이 일치하지 않습니다.'); return; }
  try {
    await api('/api/me/password', {
      method: 'POST',
      body: JSON.stringify({ old_password: oldP, new_password: newP }),
    });
    closePasswordModal();
    alert('비밀번호가 변경되었습니다. 다시 로그인하세요.');
    location.href = '/logout';
  } catch (err) {
    alert('변경 실패: ' + err.message);
  }
}

// ═══════════════════════════════════════════════════════════
//  Admin Modal (감독 / 선박 / 사용자)
// ═══════════════════════════════════════════════════════════
const ADMIN = {
  selectedColor:  'blue',
  selectedSupIds: new Set(),   // 선박 추가 시 선택된 감독들
  supervisors:    [],
  vessels:        [],
  users:          [],
};

function openAdminModal() {
  $('#admin-modal').hidden = false;
  document.body.style.overflow = 'hidden';
  switchAdminTab('supervisors');
}
function closeAdminModal() {
  $('#admin-modal').hidden = true;
  document.body.style.overflow = '';
  // 감독/선박이 바뀌었을 수 있으므로 목록 새로고침
  reloadAll();
}
function switchAdminTab(which) {
  document.querySelectorAll('.admin-tab').forEach(t => {
    t.classList.toggle('active', t.dataset.adminTab === which);
  });
  document.querySelectorAll('.admin-panel').forEach(p => {
    p.classList.toggle('active', p.dataset.adminPanel === which);
  });
  if (which === 'supervisors') loadAdminSupervisors();
  else if (which === 'vessels')  loadAdminVessels();
  else if (which === 'users')    loadAdminUsers();
}

// ---------- 감독 ----------
async function loadAdminSupervisors() {
  ADMIN.supervisors = await api('/api/supervisors');
  renderAdminSupList();
}
function renderAdminSupList() {
  const list = $('#admin-sup-list');
  list.innerHTML = '';
  if (!ADMIN.supervisors.length) {
    list.append(el('div', { class: 'attach-empty' }, '등록된 감독이 없습니다.'));
    return;
  }
  const total = ADMIN.supervisors.length;
  ADMIN.supervisors.forEach((s, idx) => {
    const item = el('div', { class: 'admin-list-item' });
    item.append(el('span', { class: `tab-dot dot-${s.color}`, style: 'width:10px;height:10px;flex-shrink:0' }));
    item.append(el('div', { class: 'item-main' },
      el('strong', {}, s.name),
      el('div', { class: 'item-sub' },
        `담당 선박: ${escHtml(s.vessels || '없음')} · 이슈 ${s.total}건`)));
    item.append(el('div', { class: 'item-tags' },
      el('span', { class: 'item-tag' }, s.email || '(이메일 없음)')));
    const actions = el('div', { class: 'item-actions' });

    // ↑ 위로
    const upBtn = el('button', {
      class: 'icon-btn', title: '위로 이동',
      disabled: idx === 0,
      onclick: () => moveSupervisor(s.id, 'up'),
    });
    upBtn.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" style="width:12px;height:12px">
      <polyline points="18 15 12 9 6 15"/></svg>`;
    actions.append(upBtn);

    // ↓ 아래로
    const downBtn = el('button', {
      class: 'icon-btn', title: '아래로 이동',
      disabled: idx === total - 1,
      onclick: () => moveSupervisor(s.id, 'down'),
    });
    downBtn.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" style="width:12px;height:12px">
      <polyline points="6 9 12 15 18 9"/></svg>`;
    actions.append(downBtn);

    // 편집
    const ed = el('button', {
      class: 'icon-btn', title: '감독 편집',
      onclick: () => openSupervisorEdit(s),
    });
    ed.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:12px;height:12px">
      <path d="M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7"/>
      <path d="M18.5 2.5a2.121 2.121 0 013 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>`;
    actions.append(ed);
    // 삭제
    const rm = el('button', {
      class: 'icon-btn danger', title: '감독 삭제',
      onclick: () => deleteSupervisor(s.id, s.name),
    });
    rm.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:12px;height:12px">
      <path d="M3 6h18"/><path d="M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2"/>
      <path d="M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6"/></svg>`;
    actions.append(rm);
    item.append(actions);
    list.append(item);
  });
}

// 감독 순서 ↑↓ 이동 (display_order 기준)
async function moveSupervisor(sid, direction) {
  const list = [...ADMIN.supervisors];
  const idx = list.findIndex(s => s.id === sid);
  if (idx < 0) return;
  const target = direction === 'up' ? idx - 1 : idx + 1;
  if (target < 0 || target >= list.length) return;

  // 배열에서 swap
  [list[idx], list[target]] = [list[target], list[idx]];

  // 전체 display_order를 1..N 으로 재정규화 (변경 필요한 것만 PUT)
  try {
    for (let i = 0; i < list.length; i++) {
      if (list[i].display_order !== i + 1) {
        await api(`/api/supervisors/${list[i].id}`, {
          method: 'PUT',
          body: JSON.stringify({ display_order: i + 1 }),
        });
      }
    }
    await loadAdminSupervisors();
    await reloadAll();   // 실제 화면 탭 바도 갱신
  } catch (err) {
    alert('순서 변경 실패: ' + err.message);
  }
}
async function addSupervisor() {
  const name = $('#sup-add-name').value.trim();
  if (!name) { alert('이름을 입력하세요.'); return; }
  try {
    await api('/api/supervisors', {
      method: 'POST',
      body: JSON.stringify({
        name,
        email: $('#sup-add-email').value.trim(),
        color: ADMIN.selectedColor,
      }),
    });
    $('#sup-add-name').value = '';
    $('#sup-add-email').value = '';
    await loadAdminSupervisors();
  } catch (err) { alert('추가 실패: ' + err.message); }
}
async function deleteSupervisor(id, name) {
  if (!confirm(`감독 "${name}"을(를) 삭제하시겠습니까?\n(이슈가 있으면 비활성 처리됩니다)`)) return;
  try {
    await api('/api/supervisors/' + id, { method: 'DELETE' });
    await loadAdminSupervisors();
  } catch (err) { alert('삭제 실패: ' + err.message); }
}

// ---------- 선박 ----------
async function loadAdminVessels() {
  [ADMIN.vessels, ADMIN.supervisors] = await Promise.all([
    api('/api/vessels/all'),
    api('/api/supervisors'),
  ]);
  renderAdminVesList();
  renderSupChipGroup();
}
function renderAdminVesList() {
  const list = $('#admin-ves-list');
  list.innerHTML = '';
  if (!ADMIN.vessels.length) {
    list.append(el('div', { class: 'attach-empty' }, '등록된 선박이 없습니다.'));
    return;
  }
  for (const v of ADMIN.vessels) {
    const item = el('div', { class: 'admin-list-item' + (v.active ? '' : ' inactive') });
    item.append(el('span', { class: 'item-tag type' }, v.vessel_type || '?'));
    item.append(el('div', { class: 'item-main' },
      el('strong', {}, v.name),
      el('div', { class: 'item-sub' },
        `${v.short_name ? v.short_name + ' · ' : ''}${v.imo ? 'IMO ' + v.imo + ' · ' : ''}담당: ${escHtml(v.supervisor_names || '없음')}`)));
    item.append(el('div', {}));

    const actions = el('div', { class: 'item-actions' });
    // 편집 버튼
    const ed = el('button', {
      class: 'icon-btn', title: '선박 편집',
      onclick: () => openVesselEdit(v.id, 'admin'),
    });
    ed.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:12px;height:12px">
      <path d="M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7"/>
      <path d="M18.5 2.5a2.121 2.121 0 013 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>`;
    actions.append(ed);
    // 삭제 버튼
    const rm = el('button', {
      class: 'icon-btn danger', title: '선박 삭제',
      onclick: () => deleteVessel(v.id, v.name),
    });
    rm.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:12px;height:12px">
      <path d="M3 6h18"/><path d="M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2"/>
      <path d="M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6"/></svg>`;
    actions.append(rm);
    item.append(actions);
    list.append(item);
  }
}
function renderSupChipGroup() {
  const box = $('#ves-add-sups');
  box.innerHTML = '';
  if (!ADMIN.supervisors.length) {
    box.append(el('span', { style: 'color:var(--text-tertiary); font-size:11.5px' }, '감독을 먼저 등록하세요.'));
    return;
  }
  for (const s of ADMIN.supervisors) {
    const chip = el('span', {
      class: 'admin-chip' + (ADMIN.selectedSupIds.has(s.id) ? ' selected' : ''),
      'data-sid': s.id,
      onclick: () => {
        if (ADMIN.selectedSupIds.has(s.id)) ADMIN.selectedSupIds.delete(s.id);
        else ADMIN.selectedSupIds.add(s.id);
        renderSupChipGroup();
      },
    }, s.name);
    box.append(chip);
  }
}
async function addVessel() {
  const name = $('#ves-add-name').value.trim();
  if (!name) { alert('선박명을 입력하세요.'); return; }
  if (!ADMIN.selectedSupIds.size) { alert('담당 감독을 최소 1명 선택하세요.'); return; }
  try {
    await api('/api/vessels', {
      method: 'POST',
      body: JSON.stringify({
        name,
        short_name:    $('#ves-add-short').value.trim() || name.slice(0, 12),
        vessel_type:   $('#ves-add-type').value,
        imo:           $('#ves-add-imo').value.trim(),
        class_society: $('#ves-add-class').value.trim(),
        supervisor_ids: [...ADMIN.selectedSupIds],
      }),
    });
    $('#ves-add-name').value = '';
    $('#ves-add-short').value = '';
    $('#ves-add-imo').value = '';
    $('#ves-add-class').value = '';
    ADMIN.selectedSupIds.clear();
    await loadAdminVessels();
  } catch (err) { alert('추가 실패: ' + err.message); }
}
async function deleteVessel(id, name) {
  if (!confirm(`선박 "${name}"을(를) 삭제하시겠습니까?\n(이슈가 있으면 비활성 처리됩니다)`)) return;
  try {
    await api('/api/vessels/' + id, { method: 'DELETE' });
    await loadAdminVessels();
  } catch (err) { alert('삭제 실패: ' + err.message); }
}

// ---------- 사용자 ----------
async function loadAdminUsers() {
  [ADMIN.users, ADMIN.supervisors] = await Promise.all([
    api('/api/users'),
    api('/api/supervisors'),
  ]);
  renderAdminUserList();
  renderUserAddSupSelect();
}
function renderAdminUserList() {
  const list = $('#admin-user-list');
  list.innerHTML = '';
  if (!ADMIN.users.length) {
    list.append(el('div', { class: 'attach-empty' }, '사용자가 없습니다.'));
    return;
  }
  for (const u of ADMIN.users) {
    const item = el('div', { class: 'admin-list-item' + (u.active ? '' : ' inactive') });
    item.append(el('span', { class: `role-pill role-${u.role === 'admin' ? 'admin' : 'user'}` },
      u.role === 'admin' ? 'ADMIN' : 'USER'));
    item.append(el('div', { class: 'item-main' },
      el('strong', {}, u.display_name || u.username),
      el('div', { class: 'item-sub' },
        `@${u.username}${u.supervisor_name ? ' · 담당: ' + u.supervisor_name : ''} · 마지막 로그인: ${u.last_login_at || '없음'}`)));
    item.append(el('div', {}));

    const actions = el('div', { class: 'item-actions' });
    // 편집
    const ed = el('button', {
      class: 'icon-btn', title: '사용자 편집',
      onclick: () => openUserEdit(u),
    });
    ed.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:12px;height:12px">
      <path d="M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7"/>
      <path d="M18.5 2.5a2.121 2.121 0 013 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>`;
    actions.append(ed);
    // 비밀번호 리셋
    const pwBtn = el('button', {
      class: 'icon-btn', title: '비밀번호 리셋',
      onclick: () => resetUserPassword(u.id, u.username),
    });
    pwBtn.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:12px;height:12px">
      <rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0110 0v4"/></svg>`;
    actions.append(pwBtn);

    if (u.active) {
      const rm = el('button', {
        class: 'icon-btn danger', title: '사용자 비활성',
        onclick: () => deleteUser(u.id, u.username),
      });
      rm.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:12px;height:12px">
        <path d="M3 6h18"/><path d="M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2"/>
        <path d="M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6"/></svg>`;
      actions.append(rm);
    }
    item.append(actions);
    list.append(item);
  }
}
function renderUserAddSupSelect() {
  const sel = $('#user-add-supervisor');
  sel.innerHTML = '';
  sel.append(el('option', { value: '' }, '(연결 없음)'));
  for (const s of ADMIN.supervisors) {
    sel.append(el('option', { value: s.id }, s.name));
  }
}
async function addUser() {
  const username = $('#user-add-username').value.trim();
  const password = $('#user-add-password').value;
  if (!username) { alert('사용자명을 입력하세요.'); return; }
  if (password.length < 6) { alert('비밀번호는 6자 이상이어야 합니다.'); return; }
  try {
    await api('/api/users', {
      method: 'POST',
      body: JSON.stringify({
        username, password,
        display_name:  $('#user-add-display').value.trim() || username,
        role:          $('#user-add-role').value,
        supervisor_id: Number($('#user-add-supervisor').value) || null,
      }),
    });
    $('#user-add-username').value = '';
    $('#user-add-password').value = '';
    $('#user-add-display').value  = '';
    await loadAdminUsers();
  } catch (err) { alert('추가 실패: ' + err.message); }
}
async function deleteUser(id, username) {
  if (!confirm(`사용자 "${username}"을(를) 비활성 처리하시겠습니까?`)) return;
  try {
    await api('/api/users/' + id, { method: 'DELETE' });
    await loadAdminUsers();
  } catch (err) { alert('처리 실패: ' + err.message); }
}
async function resetUserPassword(id, username) {
  const pw = prompt(`"${username}"의 새 비밀번호를 입력하세요 (6자 이상):`);
  if (!pw) return;
  if (pw.length < 6) { alert('비밀번호는 6자 이상이어야 합니다.'); return; }
  try {
    await api(`/api/users/${id}/password`, {
      method: 'POST',
      body: JSON.stringify({ new_password: pw }),
    });
    alert('비밀번호가 변경되었습니다.');
  } catch (err) { alert('실패: ' + err.message); }
}

// ═══════════════════════════════════════════════════════════
//  My Vessels Modal (담당 선박 조회/추가)
// ═══════════════════════════════════════════════════════════
async function openMyVessels() {
  if (S.activeTab === 'all') return;
  const sup = S.supervisors.find(s => s.id == S.activeTab);
  if (!sup) return;

  S.myVesSupId = sup.id;
  $('#myves-title').textContent = `${sup.name} 담당 선박`;
  await renderMyVesList();

  // 선박 추가 폼 표시 조건:
  //  - admin: 항상 표시
  //  - member: 본인 감독 탭일 때만 표시 (본인 담당 선박으로만 추가 가능)
  const canAdd = (S.user.role === 'admin')
                 || (S.user.supervisor_id && S.user.supervisor_id === sup.id);
  const addForm = $('#myves-add-form');
  if (addForm) {
    addForm.hidden = !canAdd;
    if (canAdd) {
      $('#myves-add-name').value = '';
      $('#myves-add-short').value = '';
      $('#myves-add-imo').value = '';
      $('#myves-add-class').value = '';
      $('#myves-add-type').value = 'VLCC';
    }
  }

  $('#myves-modal').hidden = false;
  document.body.style.overflow = 'hidden';
}

async function closeMyVessels() {
  $('#myves-modal').hidden = true;
  document.body.style.overflow = '';
  await reloadAll();   // 담당 선박 변경 반영
}

async function renderMyVesList() {
  const list = $('#myves-list');
  list.innerHTML = '';
  const vs = await api(`/api/vessels?supervisor_id=${S.myVesSupId}`);
  if (!vs.length) {
    const isAdmin    = S.user.role === 'admin';
    const isOwnerSup = S.user.supervisor_id && S.user.supervisor_id === S.myVesSupId;
    const msg = (isAdmin || isOwnerSup)
      ? '담당 선박이 없습니다. 아래에서 추가하세요.'
      : '담당 선박이 없습니다. 관리자에게 요청하세요.';
    list.append(el('div', { class: 'attach-empty' }, msg));
    return;
  }
  for (const v of vs) {
    const item = el('div', { class: 'admin-list-item' });
    item.append(el('span', { class: 'item-tag type' }, v.vessel_type || '?'));
    item.append(el('div', { class: 'item-main' },
      el('strong', {}, v.name),
      el('div', { class: 'item-sub' },
        [
          v.short_name && `${v.short_name}`,
          v.imo && `IMO ${v.imo}`,
          v.class_society,
        ].filter(Boolean).join(' · ') || '-')));
    item.append(el('div', {}));

    // 권한별 버튼 노출
    const isAdmin    = S.user.role === 'admin';
    const isOwnerSup = S.user.supervisor_id && S.user.supervisor_id === S.myVesSupId;

    if (isAdmin) {
      const actions = el('div', { class: 'item-actions' });
      // 편집
      const ed = el('button', {
        class: 'icon-btn', title: '선박 편집',
        onclick: () => openVesselEdit(v.id, 'myves'),
      });
      ed.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:12px;height:12px">
        <path d="M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7"/>
        <path d="M18.5 2.5a2.121 2.121 0 013 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>`;
      actions.append(ed);
      // 담당 해제
      const rm = el('button', {
        class: 'icon-btn danger', title: '이 감독의 담당에서 제외',
        onclick: () => unassignMyVessel(v.id, v.name),
      });
      rm.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:12px;height:12px">
        <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>`;
      actions.append(rm);
      item.append(actions);
    } else if (isOwnerSup) {
      // member: 본인 담당 탭에서 편집 + 삭제 가능
      const actions = el('div', { class: 'item-actions' });
      const ed = el('button', {
        class: 'icon-btn', title: '선박 편집',
        onclick: () => openVesselEdit(v.id, 'myves'),
      });
      ed.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:12px;height:12px">
        <path d="M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7"/>
        <path d="M18.5 2.5a2.121 2.121 0 013 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>`;
      actions.append(ed);
      const rm = el('button', {
        class: 'icon-btn danger', title: '선박 삭제',
        onclick: () => deleteMyVessel(v.id, v.name),
      });
      rm.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:12px;height:12px">
        <path d="M3 6h18"/><path d="M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2"/>
        <path d="M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6"/></svg>`;
      actions.append(rm);
      item.append(actions);
    } else {
      item.append(el('div', {}));
    }
    list.append(item);
  }
}

async function deleteMyVessel(vid, vname) {
  if (!confirm(`"${vname}"을(를) 삭제하시겠습니까?\n\n· 다른 감독도 담당 중이라면 → 본인 담당에서만 제외됩니다\n· 본인만 담당 + 이슈 있음 → 비활성 처리됩니다\n· 본인만 담당 + 이슈 없음 → 완전히 삭제됩니다`)) return;
  try {
    const r = await api('/api/vessels/' + vid, { method: 'DELETE' });
    if (r.unassigned_only) {
      alert('다른 감독이 담당 중이어서, 본인 담당에서만 제외되었습니다.');
    } else if (r.soft_delete) {
      alert(`이슈 ${r.issues}건이 있어 비활성 처리되었습니다.`);
    }
    await renderMyVesList();
    await reloadAll();
  } catch (err) { alert('삭제 실패: ' + err.message); }
}

async function unassignMyVessel(vid, vname) {
  if (!confirm(`"${vname}"을(를) 이 감독의 담당에서 제외하시겠습니까?\n(선박 자체는 삭제되지 않으며, 다른 감독의 담당이면 계속 유지됩니다)`)) return;
  try {
    const all = await api('/api/vessels/all');
    const v = all.find(x => x.id === vid);
    if (!v) throw new Error('선박을 찾을 수 없습니다.');
    const newSids = (v.supervisor_ids || []).filter(s => s !== S.myVesSupId);
    await api(`/api/vessels/${vid}`, {
      method: 'PUT',
      body: JSON.stringify({ supervisor_ids: newSids }),
    });
    await renderMyVesList();
  } catch (err) { alert('실패: ' + err.message); }
}

async function addVesselFromMyVes() {
  const name = $('#myves-add-name').value.trim();
  if (!name) { alert('선박명을 입력하세요.'); return; }
  try {
    await api('/api/vessels', {
      method: 'POST',
      body: JSON.stringify({
        name,
        short_name:    $('#myves-add-short').value.trim() || name.slice(0, 12),
        vessel_type:   $('#myves-add-type').value,
        imo:           $('#myves-add-imo').value.trim(),
        class_society: $('#myves-add-class').value.trim(),
        supervisor_ids: [S.myVesSupId],
      }),
    });
    $('#myves-add-name').value = '';
    $('#myves-add-short').value = '';
    $('#myves-add-imo').value = '';
    $('#myves-add-class').value = '';
    await renderMyVesList();
  } catch (err) { alert('추가 실패: ' + err.message); }
}

// ═══════════════════════════════════════════════════════════
//  Vessel Edit Modal (선박 정보 수정 — admin 전용)
// ═══════════════════════════════════════════════════════════
const VEDIT = {
  id: null,
  selectedSupIds: new Set(),
  context: null,   // 'admin' | 'myves' — 어느 리스트를 갱신할지
};

async function openVesselEdit(vid, context) {
  VEDIT.id = vid;
  VEDIT.context = context || 'admin';

  // 현재 선박 정보 조회
  const all = await api('/api/vessels/all');
  const v = all.find(x => x.id === vid);
  if (!v) { alert('선박 정보를 찾을 수 없습니다.'); return; }

  $('#vedit-name').value  = v.name || '';
  $('#vedit-short').value = v.short_name || '';
  $('#vedit-type').value  = v.vessel_type || 'VLCC';
  $('#vedit-imo').value   = v.imo || '';
  $('#vedit-class').value = v.class_society || '';

  VEDIT.selectedSupIds = new Set(v.supervisor_ids || []);
  renderVeditSups();

  // member는 담당 감독 변경 불가 — 섹션 숨김
  const supsField = $('#vedit-sups').closest('.form-field');
  if (supsField) {
    supsField.style.display = (S.user.role === 'admin') ? '' : 'none';
  }

  $('#vessel-edit-modal').hidden = false;
  document.body.style.overflow = 'hidden';
}

function renderVeditSups() {
  const box = $('#vedit-sups');
  box.innerHTML = '';
  // S.supervisors 또는 ADMIN.supervisors 사용
  const sups = (ADMIN.supervisors && ADMIN.supervisors.length) ? ADMIN.supervisors : S.supervisors;
  if (!sups || !sups.length) {
    box.append(el('span', { style: 'color:var(--text-tertiary); font-size:11.5px' },
      '감독이 없습니다.'));
    return;
  }
  for (const s of sups) {
    const chip = el('span', {
      class: 'admin-chip' + (VEDIT.selectedSupIds.has(s.id) ? ' selected' : ''),
      onclick: () => {
        if (VEDIT.selectedSupIds.has(s.id)) VEDIT.selectedSupIds.delete(s.id);
        else VEDIT.selectedSupIds.add(s.id);
        renderVeditSups();
      },
    }, s.name);
    box.append(chip);
  }
}

function closeVesselEdit() {
  $('#vessel-edit-modal').hidden = true;
  document.body.style.overflow = '';
}

async function saveVesselEdit() {
  const name = $('#vedit-name').value.trim();
  if (!name) { alert('선박명을 입력하세요.'); return; }
  const isAdmin = S.user.role === 'admin';
  if (isAdmin && !VEDIT.selectedSupIds.size) {
    if (!confirm('담당 감독이 선택되지 않았습니다. 저장하면 미할당 상태가 됩니다. 계속할까요?')) return;
  }
  try {
    const payload = {
      name,
      short_name:    $('#vedit-short').value.trim(),
      vessel_type:   $('#vedit-type').value,
      imo:           $('#vedit-imo').value.trim(),
      class_society: $('#vedit-class').value.trim(),
    };
    if (isAdmin) {
      payload.supervisor_ids = [...VEDIT.selectedSupIds];
    }
    await api(`/api/vessels/${VEDIT.id}`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    });
    closeVesselEdit();
    if (VEDIT.context === 'myves') {
      await renderMyVesList();
    } else {
      await loadAdminVessels();
    }
    await reloadAll();
  } catch (err) { alert('저장 실패: ' + err.message); }
}

// ═══════════════════════════════════════════════════════════
//  Supervisor Edit Modal (감독 정보 수정)
// ═══════════════════════════════════════════════════════════
const SEDIT = { id: null, selectedColor: 'blue' };

function openSupervisorEdit(sup) {
  SEDIT.id = sup.id;
  SEDIT.selectedColor = sup.color || 'blue';
  $('#sedit-name').value  = sup.name || '';
  $('#sedit-email').value = sup.email || '';
  document.querySelectorAll('#sedit-colors .color-swatch').forEach(sw => {
    sw.classList.toggle('selected', sw.dataset.color === SEDIT.selectedColor);
  });
  $('#supervisor-edit-modal').hidden = false;
  document.body.style.overflow = 'hidden';
}
function closeSupervisorEdit() {
  $('#supervisor-edit-modal').hidden = true;
  document.body.style.overflow = '';
}
async function saveSupervisorEdit() {
  const name = $('#sedit-name').value.trim();
  if (!name) { alert('이름을 입력하세요.'); return; }
  try {
    await api(`/api/supervisors/${SEDIT.id}`, {
      method: 'PUT',
      body: JSON.stringify({
        name,
        email: $('#sedit-email').value.trim(),
        color: SEDIT.selectedColor,
      }),
    });
    closeSupervisorEdit();
    await loadAdminSupervisors();
    await reloadAll();
  } catch (err) { alert('저장 실패: ' + err.message); }
}

// ═══════════════════════════════════════════════════════════
//  User Edit Modal (사용자 정보 수정)
// ═══════════════════════════════════════════════════════════
const UEDIT = { id: null };

function openUserEdit(user) {
  UEDIT.id = user.id;
  $('#uedit-username').value = user.username || '';
  $('#uedit-display').value  = user.display_name || '';
  $('#uedit-role').value     = user.role || 'member';
  $('#uedit-active').value   = String(user.active != null ? user.active : 1);

  // 감독 셀렉트 옵션 채우기
  const sel = $('#uedit-supervisor');
  sel.innerHTML = '';
  sel.append(el('option', { value: '' }, '(연결 없음)'));
  for (const s of ADMIN.supervisors) {
    sel.append(el('option', { value: s.id }, s.name));
  }
  sel.value = user.supervisor_id != null ? String(user.supervisor_id) : '';

  $('#user-edit-modal').hidden = false;
  document.body.style.overflow = 'hidden';
}
function closeUserEdit() {
  $('#user-edit-modal').hidden = true;
  document.body.style.overflow = '';
}
async function saveUserEdit() {
  try {
    const supVal = $('#uedit-supervisor').value;
    await api(`/api/users/${UEDIT.id}`, {
      method: 'PUT',
      body: JSON.stringify({
        display_name: $('#uedit-display').value.trim(),
        role:         $('#uedit-role').value,
        active:       Number($('#uedit-active').value),
        supervisor_id: supVal ? Number(supVal) : null,
      }),
    });
    closeUserEdit();
    await loadAdminUsers();
  } catch (err) { alert('저장 실패: ' + err.message); }
}

// ───────────── reloadAll ─────────────
async function reloadAll() {
  await loadSupervisors();
  renderTabs();
  await loadVessels(S.activeTab === 'all' ? null : S.activeTab);
  renderVesselFilter();
  renderTabContext();
  await loadIssues();
  _vesselCache.clear();
  render();
}

// ───────────── Event wiring ─────────────
function wireEvents() {
  // 툴바의 "+ 신규 이슈" → 모달
  $('#btn-new-issue').addEventListener('click', openNew);

  $('#btn-today').addEventListener('click', () => {
    const t = todayISO();
    S.filters.q = t;
    $('#filter-search').value = t;
    loadIssues().then(render);
  });

  $('#btn-toggle-all').addEventListener('click', toggleAll);

  let searchTimer;
  $('#filter-search').addEventListener('input', (e) => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      S.filters.q = e.target.value.trim();
      loadIssues().then(render);
    }, 220);
  });

  $('#filter-vessel').addEventListener('change', (e) => {
    S.filters.vessel_id = e.target.value;
    loadIssues().then(render);
  });
  $('#filter-vessel-type').addEventListener('change', (e) => {
    S.filters.vessel_type = e.target.value;
    loadIssues().then(render);
  });
  $('#filter-status').addEventListener('change', (e) => {
    S.filters.status = e.target.value;
    loadIssues().then(render);
  });
  $('#filter-priority').addEventListener('change', (e) => {
    S.filters.priority = e.target.value;
    loadIssues().then(render);
  });

  // Edit Modal
  $('#issue-modal').addEventListener('click', (ev) => {
    if (ev.target.dataset.close === '1') closeModal();
  });
  $('#issue-form').addEventListener('submit', saveIssue);
  $('#btn-delete').addEventListener('click', () => {
    if (S.editingId) confirmDelete(S.editingId);
  });
  $('#f-supervisor').addEventListener('change', (e) => {
    refillVesselSelect(e.target.value);
  });
  $('#btn-add-action').addEventListener('click', addActionEntry);

  // Attach Modal
  $('#attach-modal').addEventListener('click', (ev) => {
    if (ev.target.dataset.closeAttach === '1') closeAttach();
  });
  const dz = $('#attach-dropzone');
  const fileIn = $('#attach-file-input');
  dz.addEventListener('click', () => fileIn.click());
  dz.addEventListener('dragover', (e) => { e.preventDefault(); dz.classList.add('dragover'); });
  dz.addEventListener('dragleave', () => dz.classList.remove('dragover'));
  dz.addEventListener('drop', (e) => {
    e.preventDefault();
    dz.classList.remove('dragover');
    if (e.dataTransfer.files.length) uploadAttachFiles([...e.dataTransfer.files]);
  });
  fileIn.addEventListener('change', (e) => {
    const files = [...(e.target.files || [])];
    if (files.length) uploadAttachFiles(files);
    e.target.value = '';
  });

  // 전역 ESC
  document.addEventListener('keydown', (ev) => {
    if (ev.key !== 'Escape') return;
    // 2차 모달(편집) 먼저 체크
    if ($('#vessel-edit-modal') && !$('#vessel-edit-modal').hidden) { closeVesselEdit(); return; }
    if ($('#supervisor-edit-modal') && !$('#supervisor-edit-modal').hidden) { closeSupervisorEdit(); return; }
    if ($('#user-edit-modal') && !$('#user-edit-modal').hidden) { closeUserEdit(); return; }
    // 1차 모달
    if (!$('#issue-modal').hidden) closeModal();
    else if (!$('#attach-modal').hidden) closeAttach();
    else if (!$('#myves-modal').hidden) closeMyVessels();
    else if (!$('#password-modal').hidden) closePasswordModal();
    else if ($('#admin-modal') && !$('#admin-modal').hidden) closeAdminModal();
    else if (S.inlineAdd) cancelInlineAdd();
  });

  // ───── 선박 편집 모달 (admin 전용) ─────
  const vEditModal = $('#vessel-edit-modal');
  if (vEditModal) {
    vEditModal.addEventListener('click', (ev) => {
      if (ev.target.dataset.closeVesedit === '1') closeVesselEdit();
    });
    $('#btn-vedit-save').addEventListener('click', saveVesselEdit);
  }

  // ───── 감독 편집 모달 (admin 전용) ─────
  const sEditModal = $('#supervisor-edit-modal');
  if (sEditModal) {
    sEditModal.addEventListener('click', (ev) => {
      if (ev.target.dataset.closeSupedit === '1') closeSupervisorEdit();
    });
    $('#btn-sedit-save').addEventListener('click', saveSupervisorEdit);
    $('#sedit-colors').addEventListener('click', (ev) => {
      const sw = ev.target.closest('.color-swatch');
      if (!sw) return;
      SEDIT.selectedColor = sw.dataset.color;
      document.querySelectorAll('#sedit-colors .color-swatch')
        .forEach(x => x.classList.toggle('selected', x === sw));
    });
  }

  // ───── 사용자 편집 모달 (admin 전용) ─────
  const uEditModal = $('#user-edit-modal');
  if (uEditModal) {
    uEditModal.addEventListener('click', (ev) => {
      if (ev.target.dataset.closeUseredit === '1') closeUserEdit();
    });
    $('#btn-uedit-save').addEventListener('click', saveUserEdit);
  }

  // ───── 담당 선박 모달 ─────
  $('#myves-modal').addEventListener('click', (ev) => {
    if (ev.target.dataset.closeMyves === '1') closeMyVessels();
  });
  $('#btn-myves-add')?.addEventListener('click', addVesselFromMyVes);

  // ───── User Menu (네비 우측 드롭다운) ─────
  const umTrig = $('#user-menu-trigger');
  const umDrop = $('#user-dropdown');
  if (umTrig) {
    umTrig.addEventListener('click', (e) => {
      e.stopPropagation();
      toggleUserMenu();
    });
    document.addEventListener('click', (e) => {
      if (!umDrop.hidden && !e.target.closest('#user-dropdown') && !e.target.closest('#user-menu-trigger')) {
        toggleUserMenu(false);
      }
    });
  }

  // ───── Password Change ─────
  $('#btn-change-password')?.addEventListener('click', () => {
    toggleUserMenu(false);
    openPasswordModal();
  });
  $('#password-modal').addEventListener('click', (ev) => {
    if (ev.target.dataset.closePw === '1') closePasswordModal();
  });
  $('#password-form').addEventListener('submit', submitPasswordChange);

  // ───── Admin Modal ─────
  const adminBtn = $('#btn-open-admin');
  if (adminBtn) {
    adminBtn.addEventListener('click', openAdminModal);
    $('#admin-modal').addEventListener('click', (ev) => {
      if (ev.target.dataset.closeAdmin === '1') closeAdminModal();
    });
    document.querySelectorAll('.admin-tab').forEach(t => {
      t.addEventListener('click', () => switchAdminTab(t.dataset.adminTab));
    });
    // 감독 추가
    $('#btn-sup-add').addEventListener('click', addSupervisor);
    $('#sup-add-colors').addEventListener('click', (ev) => {
      const sw = ev.target.closest('.color-swatch');
      if (!sw) return;
      ADMIN.selectedColor = sw.dataset.color;
      document.querySelectorAll('#sup-add-colors .color-swatch')
        .forEach(x => x.classList.toggle('selected', x === sw));
    });
    // 선박 추가
    $('#btn-ves-add').addEventListener('click', addVessel);
    // 사용자 추가
    $('#btn-user-add').addEventListener('click', addUser);
  }
}

// ───────────── Init ─────────────
(async function init() {
  try {
    await loadSupervisors();
    S.activeTab = S.user.supervisor_id
      ? S.user.supervisor_id
      : (S.supervisors[0] ? S.supervisors[0].id : 'all');
    await loadVessels(S.activeTab === 'all' ? null : S.activeTab);
    renderTabs();
    renderVesselFilter();
    renderTabContext();
    await loadIssues();
    fillFormSelects();
    render();
    wireEvents();
  } catch (err) {
    console.error(err);
    alert('초기 로드 실패: ' + err.message);
  }
})();
