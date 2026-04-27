/* ═══════════════════════════════════════════════════════════════
   TRMT3 — 일정 (Calendar)
   Phase A: 월 뷰 + 사이드 패널 + 직접 입력 + 감독 탭
   ═══════════════════════════════════════════════════════════════ */
'use strict';

const $  = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

// ───────────── State ─────────────
const HIDDEN_SUPERVISOR_NAMES_CAL = ['FLEET AGENDA'];
function isHiddenSupervisor(sup) {
  return HIDDEN_SUPERVISOR_NAMES_CAL.includes((sup.name || '').toUpperCase());
}

const today0 = new Date();
const S = {
  user:        window.TRMT?.user || {},
  supervisors: [],
  vessels:     [],
  events:      [],          // 현재 표시 중인 월의 이벤트
  activeTab:   'all',       // 'all' or supervisor.id
  // 현재 표시 중인 달
  cursor:      new Date(today0.getFullYear(), today0.getMonth(), 1),
  selectedDate: null,       // 'YYYY-MM-DD'
};

// ───────────── Helpers ─────────────
function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === 'class') e.className = v;
    else if (k.startsWith('on') && typeof v === 'function') e.addEventListener(k.slice(2), v);
    else if (k === 'hidden' && v === true) e.hidden = true;
    else e.setAttribute(k, v);
  }
  for (const c of children) {
    if (c == null || c === false) continue;
    e.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
  return e;
}

async function api(url, options = {}) {
  const opts = { ...options };
  if (opts.body && typeof opts.body === 'string') {
    opts.headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
  }
  const r = await fetch(url, opts);
  if (!r.ok) {
    let msg = `HTTP ${r.status}`;
    try { const j = await r.json(); msg = j.error || msg; } catch (_) {}
    throw new Error(msg);
  }
  return r.json();
}

function pad2(n) { return String(n).padStart(2, '0'); }
function ymd(d)  { return `${d.getFullYear()}-${pad2(d.getMonth()+1)}-${pad2(d.getDate())}`; }
function isSameDay(d, ymdStr) { return ymd(d) === ymdStr; }

// 현재 월의 캘린더 그리드용 6주(42칸) 날짜 배열 생성
function buildMonthMatrix(cursor) {
  const first = new Date(cursor.getFullYear(), cursor.getMonth(), 1);
  // 일요일 시작 (0=일, 6=토)
  const startDay = first.getDay();
  const start = new Date(first);
  start.setDate(first.getDate() - startDay);
  const days = [];
  for (let i = 0; i < 42; i++) {
    const d = new Date(start);
    d.setDate(start.getDate() + i);
    days.push(d);
  }
  return days;
}

// 해당 날짜 ymd가 이벤트의 표시 기간 안에 들어가는지
function eventCoversDate(ev, ymdStr) {
  const s = ev.start_date;
  const e = ev.end_date || ev.start_date;
  return s <= ymdStr && ymdStr <= e;
}

// ───────────── Tabs ─────────────
function renderTabs() {
  const bar = $('#cal-tab-bar');
  bar.innerHTML = '';
  bar.append(tabEl('all', '전체', 'gray', S.activeTab === 'all'));
  for (const s of S.supervisors) {
    if (isHiddenSupervisor(s)) continue;
    bar.append(tabEl(s.id, s.name, s.color, S.activeTab == s.id));
  }
}

function tabEl(id, name, color, active) {
  const t = el('div', { class: 'tab' + (active ? ' active' : ''), 'data-id': id },
    el('span', { class: `tab-dot dot-${color || 'gray'}` }),
    name);
  t.addEventListener('click', () => switchTab(id));
  return t;
}

async function switchTab(id) {
  S.activeTab = id;
  renderTabs();
  await reloadEvents();
}

// ───────────── Render ─────────────
function render() {
  // 라벨
  const c = S.cursor;
  $('#cal-month-label').textContent = `${c.getFullYear()}.${pad2(c.getMonth()+1)}`;

  // 컨텍스트
  const ctx = $('#cal-context');
  const tabName = S.activeTab === 'all'
    ? '전체'
    : (S.supervisors.find(x => x.id == S.activeTab)?.name + ' 담당' || '');
  ctx.textContent = `${tabName} · 일정 ${S.events.length}건`;

  renderGrid();
  renderSideList();
}

function renderGrid() {
  const grid = $('#cal-grid');
  grid.innerHTML = '';
  const days = buildMonthMatrix(S.cursor);
  const curMonth = S.cursor.getMonth();
  const todayYmd = ymd(today0);

  for (const d of days) {
    const ymdStr = ymd(d);
    const isOther = d.getMonth() !== curMonth;
    const isToday = ymdStr === todayYmd;
    const isSelected = ymdStr === S.selectedDate;
    const dow = d.getDay();

    const cell = el('div', {
      class: 'cal-cell'
        + (isOther ? ' other-month' : '')
        + (isToday ? ' is-today' : '')
        + (isSelected ? ' is-selected' : '')
        + (dow === 0 ? ' is-sun' : '')
        + (dow === 6 ? ' is-sat' : ''),
      'data-date': ymdStr,
      onclick: () => selectDate(ymdStr),
    });

    // 날짜 숫자
    cell.append(el('div', { class: 'cal-cell-day' }, String(d.getDate())));

    // 해당 날 이벤트 (최대 3개 뱃지 + N more)
    const evs = S.events.filter(ev => eventCoversDate(ev, ymdStr));
    // 시간 있는 것 우선 정렬
    evs.sort((a, b) => {
      const ta = a.start_time || '99:99';
      const tb = b.start_time || '99:99';
      return ta.localeCompare(tb);
    });
    const maxShow = 3;
    const evList = el('div', { class: 'cal-cell-events' });
    for (const ev of evs.slice(0, maxShow)) {
      evList.append(el('div', {
        class: `cal-evt dot-${ev.color || 'blue'}`,
        title: evTooltip(ev),
        onclick: (e) => { e.stopPropagation(); openEditModal(ev); },
      }, evLabel(ev)));
    }
    if (evs.length > maxShow) {
      evList.append(el('div', { class: 'cal-evt more' }, `+${evs.length - maxShow} more`));
    }
    cell.append(evList);

    grid.append(cell);
  }
}

function evLabel(ev) {
  if (ev.all_day) return ev.title;
  const t = ev.start_time ? ev.start_time + ' ' : '';
  return t + ev.title;
}

function evTooltip(ev) {
  const lines = [ev.title];
  if (ev.start_date && ev.end_date && ev.end_date !== ev.start_date) {
    lines.push(`${ev.start_date} ~ ${ev.end_date}`);
  } else if (ev.start_date) {
    lines.push(ev.start_date);
  }
  if (!ev.all_day && ev.start_time) {
    lines.push(`${ev.start_time}${ev.end_time ? ' - ' + ev.end_time : ''}`);
  }
  if (ev.location) lines.push(`📍 ${ev.location}`);
  if (ev.category) lines.push(`[${ev.category}]`);
  return lines.join('\n');
}

function renderSideList() {
  const list = $('#cal-side-list');
  const head = $('#cal-side-date');
  const addBtn = $('#cal-side-add');

  list.innerHTML = '';
  if (!S.selectedDate) {
    head.textContent = '날짜를 클릭하세요';
    addBtn.hidden = true;
    list.append(el('div', { class: 'cal-empty' },
      '왼쪽 캘린더에서 날짜를 클릭하면 그날의 일정이 보입니다.'));
    return;
  }
  // 헤더 라벨
  const d = new Date(S.selectedDate + 'T00:00:00');
  const DOW = ['일','월','화','수','목','금','토'];
  head.textContent = `${S.selectedDate} (${DOW[d.getDay()]})`;
  addBtn.hidden = false;

  const evs = S.events
    .filter(ev => eventCoversDate(ev, S.selectedDate))
    .sort((a, b) => {
      const ta = a.start_time || '99:99';
      const tb = b.start_time || '99:99';
      return ta.localeCompare(tb);
    });

  if (!evs.length) {
    list.append(el('div', { class: 'cal-empty' },
      '이 날의 일정이 없습니다. 위 [+ 추가] 버튼으로 등록하세요.'));
    return;
  }

  for (const ev of evs) {
    const item = el('div', {
      class: 'cal-side-item',
      onclick: () => openEditModal(ev),
    });
    item.append(el('span', { class: `cal-side-color dot-${ev.color || 'blue'}` }));
    const body = el('div', { class: 'cal-side-body' });
    const titleRow = el('div', { class: 'cal-side-title-row' });
    titleRow.append(el('div', { class: 'cal-side-title' }, ev.title));
    // 출처 뱃지 (다른 모듈에서 등록된 일정)
    if (ev.source_type && ev.source_type !== 'manual') {
      const srcLabel = { issue: '업무관리', cs: 'Condition Survey', vetting: 'Vetting' }[ev.source_type] || ev.source_type;
      titleRow.append(el('span', {
        class: `cal-side-source src-${ev.source_type}`,
        title: '다른 탭에서 등록된 일정',
      }, '↳ ' + srcLabel));
    }
    body.append(titleRow);
    const meta = el('div', { class: 'cal-side-meta' });
    if (ev.all_day) {
      meta.append(el('span', { class: 'cal-side-time' }, '종일'));
    } else if (ev.start_time) {
      const tlbl = ev.end_time ? `${ev.start_time} - ${ev.end_time}` : ev.start_time;
      meta.append(el('span', { class: 'cal-side-time' }, tlbl));
    }
    if (ev.category) meta.append(el('span', { class: 'cal-side-cat' }, ev.category));
    if (ev.location) meta.append(el('span', { class: 'cal-side-loc' }, '📍 ' + ev.location));

    // 선박명 (있으면)
    if (ev.vessel_id) {
      const v = S.vessels.find(x => x.id === ev.vessel_id);
      if (v) meta.append(el('span', { class: 'cal-side-vessel' }, '🚢 ' + v.name));
    }
    body.append(meta);
    if (ev.notes) body.append(el('div', { class: 'cal-side-notes' }, ev.notes));
    item.append(body);
    list.append(item);
  }
}

function selectDate(ymdStr) {
  S.selectedDate = (S.selectedDate === ymdStr) ? null : ymdStr;
  render();
}

// ───────────── Data Reload ─────────────
async function reloadEvents() {
  // 표시 중인 달 +- 1주 정도 여유 (멀티데이 이벤트가 걸치는 경우)
  const days = buildMonthMatrix(S.cursor);
  const start = ymd(days[0]);
  const end   = ymd(days[days.length - 1]);
  const supParam = S.activeTab === 'all' ? '' : `&supervisor_id=${S.activeTab}`;
  S.events = await api(`/api/cal/events?start=${start}&end=${end}${supParam}`);
  render();
}

async function loadSupervisors() {
  S.supervisors = await api('/api/supervisors');
}
async function loadVessels() {
  S.vessels = await api('/api/vessels');
}

// ───────────── Modal ─────────────
let _editingId = null;

function openCreateModal(presetDate) {
  _editingId = null;
  $('#cal-modal-title').textContent = '새 일정';
  $('#cal-f-delete').hidden = true;

  $('#cal-f-title').value = '';
  $('#cal-f-start').value = presetDate || S.selectedDate || ymd(today0);
  $('#cal-f-end').value = '';
  $('#cal-f-allday').checked = true;
  $('#cal-f-time-row').hidden = true;
  $('#cal-f-stime').value = '';
  $('#cal-f-etime').value = '';
  $('#cal-f-supervisor').value = (S.activeTab !== 'all') ? S.activeTab : (S.user.supervisor_id || '');
  $('#cal-f-vessel').value = '';
  $('#cal-f-category').value = '';
  setColor('blue');
  $('#cal-f-location').value = '';
  $('#cal-f-notes').value = '';

  showModal();
  setTimeout(() => $('#cal-f-title').focus(), 50);
}

function openEditModal(ev) {
  _editingId = ev.id;
  $('#cal-modal-title').textContent = '일정 편집';
  $('#cal-f-delete').hidden = false;

  $('#cal-f-title').value = ev.title || '';
  $('#cal-f-start').value = ev.start_date || '';
  $('#cal-f-end').value = ev.end_date || '';
  const allday = ev.all_day !== 0;
  $('#cal-f-allday').checked = allday;
  $('#cal-f-time-row').hidden = allday;
  $('#cal-f-stime').value = ev.start_time || '';
  $('#cal-f-etime').value = ev.end_time || '';
  $('#cal-f-supervisor').value = ev.supervisor_id || '';
  $('#cal-f-vessel').value = ev.vessel_id || '';
  $('#cal-f-category').value = ev.category || '';
  setColor(ev.color || 'blue');
  $('#cal-f-location').value = ev.location || '';
  $('#cal-f-notes').value = ev.notes || '';

  showModal();
  setTimeout(() => $('#cal-f-title').focus(), 50);
}

function showModal() {
  $('#cal-modal').hidden = false;
  document.body.style.overflow = 'hidden';
}
function closeModal() {
  $('#cal-modal').hidden = true;
  document.body.style.overflow = '';
  _editingId = null;
}

function setColor(color) {
  for (const btn of $$('#cal-f-colors .color-dot')) {
    btn.classList.toggle('active', btn.dataset.color === color);
  }
}
function getColor() {
  const active = $('#cal-f-colors .color-dot.active');
  return active ? active.dataset.color : 'blue';
}

function fillSupervisorVesselSelects() {
  const supSel = $('#cal-f-supervisor');
  supSel.innerHTML = '<option value="">(공용 / 전체)</option>';
  for (const s of S.supervisors) {
    if (isHiddenSupervisor(s)) continue;
    const o = document.createElement('option');
    o.value = s.id; o.textContent = s.name;
    supSel.append(o);
  }
  const vSel = $('#cal-f-vessel');
  vSel.innerHTML = '<option value="">(선택 안 함)</option>';
  for (const v of S.vessels) {
    if (!v.active) continue;
    const o = document.createElement('option');
    o.value = v.id; o.textContent = v.name;
    vSel.append(o);
  }
}

async function saveModal() {
  const title = $('#cal-f-title').value.trim();
  const start = $('#cal-f-start').value;
  if (!title)  { alert('제목을 입력하세요.'); $('#cal-f-title').focus(); return; }
  if (!start)  { alert('시작일을 선택하세요.'); $('#cal-f-start').focus(); return; }
  const allday = $('#cal-f-allday').checked;
  const end = $('#cal-f-end').value || null;
  // 종료일이 시작일보다 빠르면 자동 보정
  if (end && end < start) { alert('종료일이 시작일보다 빠를 수 없습니다.'); return; }

  const body = {
    title,
    start_date: start,
    end_date:   end,
    all_day:    allday,
    start_time: !allday ? ($('#cal-f-stime').value || null) : null,
    end_time:   !allday ? ($('#cal-f-etime').value || null) : null,
    supervisor_id: $('#cal-f-supervisor').value ? parseInt($('#cal-f-supervisor').value) : null,
    vessel_id:     $('#cal-f-vessel').value     ? parseInt($('#cal-f-vessel').value)     : null,
    category:   $('#cal-f-category').value || '',
    color:      getColor(),
    location:   $('#cal-f-location').value.trim(),
    notes:      $('#cal-f-notes').value.trim(),
  };

  try {
    if (_editingId) {
      await api(`/api/cal/events/${_editingId}`, {
        method: 'PUT', body: JSON.stringify(body),
      });
    } else {
      await api('/api/cal/events', {
        method: 'POST', body: JSON.stringify(body),
      });
    }
    closeModal();
    // 일정 추가/편집한 날짜로 자동 점프 + 선택
    if (start) {
      const d = new Date(start + 'T00:00:00');
      // 다른 달이면 cursor 이동
      if (d.getFullYear() !== S.cursor.getFullYear() || d.getMonth() !== S.cursor.getMonth()) {
        S.cursor = new Date(d.getFullYear(), d.getMonth(), 1);
      }
      S.selectedDate = start;
    }
    await reloadEvents();
  } catch (err) {
    alert('저장 실패: ' + err.message);
  }
}

async function deleteCurrent() {
  if (!_editingId) return;
  if (!confirm('이 일정을 삭제하시겠습니까?')) return;
  try {
    await api(`/api/cal/events/${_editingId}`, { method: 'DELETE' });
    closeModal();
    await reloadEvents();
  } catch (err) { alert('삭제 실패: ' + err.message); }
}

// ───────────── Init ─────────────
async function init() {
  try {
    await Promise.all([loadSupervisors(), loadVessels()]);
    fillSupervisorVesselSelects();

    // 본인 감독 탭 자동 선택 (단, 숨김 감독이면 전체로)
    if (S.user.supervisor_id) {
      const sup = S.supervisors.find(s => s.id === S.user.supervisor_id);
      if (sup && !isHiddenSupervisor(sup)) S.activeTab = S.user.supervisor_id;
    }
    renderTabs();

    // 오늘이 표시 월에 있으면 자동 선택
    if (S.cursor.getFullYear() === today0.getFullYear() &&
        S.cursor.getMonth() === today0.getMonth()) {
      S.selectedDate = ymd(today0);
    }

    await reloadEvents();

    // 월 이동
    $('#cal-prev').addEventListener('click', async () => {
      S.cursor = new Date(S.cursor.getFullYear(), S.cursor.getMonth() - 1, 1);
      S.selectedDate = null;
      await reloadEvents();
    });
    $('#cal-next').addEventListener('click', async () => {
      S.cursor = new Date(S.cursor.getFullYear(), S.cursor.getMonth() + 1, 1);
      S.selectedDate = null;
      await reloadEvents();
    });
    $('#cal-today').addEventListener('click', async () => {
      S.cursor = new Date(today0.getFullYear(), today0.getMonth(), 1);
      S.selectedDate = ymd(today0);
      await reloadEvents();
    });

    // 추가 버튼들
    $('#cal-add').addEventListener('click', () => openCreateModal());
    $('#cal-side-add').addEventListener('click', () => openCreateModal(S.selectedDate));

    // 모달 close 이벤트
    $('#cal-modal').addEventListener('click', (ev) => {
      if (ev.target.dataset.closeCal === '1') closeModal();
    });
    $('#cal-f-save').addEventListener('click', saveModal);
    $('#cal-f-delete').addEventListener('click', deleteCurrent);

    // 종일 토글
    $('#cal-f-allday').addEventListener('change', (e) => {
      $('#cal-f-time-row').hidden = e.target.checked;
    });

    // 색상 선택
    $('#cal-f-colors').addEventListener('click', (ev) => {
      const btn = ev.target.closest('.color-dot');
      if (!btn) return;
      setColor(btn.dataset.color);
    });

    // ESC 닫기
    document.addEventListener('keydown', (ev) => {
      if (ev.key === 'Escape' && !$('#cal-modal').hidden) closeModal();
    });

  } catch (err) {
    alert('초기 로드 실패: ' + err.message);
  }
}

document.addEventListener('DOMContentLoaded', init);
