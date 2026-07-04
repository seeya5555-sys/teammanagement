'use strict';
// 전역 누출 방지 IIFE — 다른 탭 JS(cal/cs/vt/cls/exp 등)와 const $/S/api 충돌 방지.
(function () {

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
  activeSubTab: localStorage.getItem('trmt_subtab') || 'open',  // 'open' = Open+진행중 / 'closed' = Closed / 'all' / 'summary'
  issues:       [],
  summary:      { rows: [], generated_at: null },
  summaryCounts: {},   // { scopeKey: n }
  filters: { q:'', vessel_id:'', vessel_type:'', status:'', priority:'', item_topic:'' },
  linkFilter: null,                                        // 요약→이슈 링크 필터 활성 라벨(해제 칩용)

  editingId:      null,
  editingActions: [],

  collapsedMonths: new Set(),
  collapsedDates:  new Set(),
  expandedActions: new Set(),

  // ── 선박별 보기 (rev.4) ──
  selectedVessel: null,                                  // 선택 선박 id (null=자동선택 전)
  mainSort:  localStorage.getItem('trmt_main_sort')  || 'old',   // 'old'=오래된→최근 / 'new'=최근·우선순위
  quickFilter: 'all',                                    // 'all'|'recent'|'stale'|'risk'
  issueSearch: '',                                       // 선택 선박 내 키워드 검색(제목/상세/조치)
  expandedRows: new Set(),                               // 인라인 펼친(상세+진행사항) 이슈 id

  // 사용자가 직접 클릭해서 펼치거나 접은 날짜 — 자동 접기에서 제외
  userToggledDates: new Set(),

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
  'COC & Flag': { cls: 'pri-cocflag', label: 'COC & Flag' },
  Urgent:       { cls: 'pri-urgent',  label: 'Urgent'     },
  'Next DD':    { cls: 'pri-nextdd',  label: 'Next DD'    },
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
// 손유석 단독 운영 — '전체' 탭/타 감독 탭 제거, 항상 손유석 탭 (cls.js ONLY_SUP 패턴과 동일)
const ONLY_SUP_NAME = '손유석';
function onlySupId() {
  const s = S.supervisors.find(x => (x.name || '').trim() === ONLY_SUP_NAME);
  return s ? s.id : 'all';  // 손유석 미존재(이론상無) 시 타 감독 scope로 새지 않게 'all' 유지 (cls.js 패턴)
}
async function loadVessels(supId) {
  const url = supId && supId !== 'all' ? `/api/vessels?supervisor_id=${supId}` : '/api/vessels';
  S.vessels = await api(url);
}
// Daily 사이드바 커스텀 선박순서(유저별, 서버저장). [] = 기본정렬(디펙트순).
async function loadVesselOrder() {
  try { const r = await api('/api/vessel-order'); S.vesselOrder = (r && r.order) || []; }
  catch (_) { S.vesselOrder = []; }
}
async function saveVesselOrder(order) {
  S.vesselOrder = order;
  try { await api('/api/vessel-order', { method: 'POST', body: JSON.stringify({ order }) }); }
  catch (e) { console.warn('vessel-order save fail', e); }
}
async function loadIssues() {
  const p = new URLSearchParams();
  if (S.activeTab !== 'all') p.set('supervisor_id', S.activeTab);

  // 요약 서브탭: 저장된 요약만 불러오고 일반 이슈 로딩은 건너뜀
  if (S.activeSubTab === 'summary') {
    const sp = new URLSearchParams();
    if (S.activeTab !== 'all') sp.set('supervisor_id', S.activeTab);
    try {
      S.summary = await api('/api/issues/summary?' + sp.toString());
    } catch (_) {
      S.summary = { rows: [], generated_at: null };
    }
    const scopeKey = (S.activeTab === 'all') ? 'all' : String(S.activeTab);
    S.summaryCounts[scopeKey] = (S.summary.rows || []).length;
    S.issues = [];
    return;
  }

  if (S.filters.q)           p.set('q', S.filters.q);
  if (S.filters.item_topic)  p.set('item_topic', S.filters.item_topic);
  if (S.filters.vessel_id)   p.set('vessel_id', S.filters.vessel_id);
  if (S.filters.vessel_type) p.set('vessel_type', S.filters.vessel_type);
  if (S.filters.priority)    p.set('priority', S.filters.priority);

  // status: 사용자가 명시적으로 status 필터를 골랐으면 그게 우선,
  //         아니면 서브 탭 기준으로 자동 적용
  if (S.filters.status) {
    p.set('status', S.filters.status);
  } else if (S.activeSubTab === 'closed') {
    p.set('status', 'Closed');
  } else if (S.activeSubTab === 'all') {
    // '전체' 서브 탭 = 진행중 + 완료 (status 미지정 → 전부)
  } else {
    // 'open' 서브 탭 = Open + InProgress
    const [openIssues, progIssues] = await Promise.all([
      (() => { const q = new URLSearchParams(p); q.set('status', 'Open'); return api('/api/issues?' + q); })(),
      (() => { const q = new URLSearchParams(p); q.set('status', 'InProgress'); return api('/api/issues?' + q); })(),
    ]);
    S.issues = [...openIssues, ...progIssues].sort((a, b) => {
      if (a.issue_date !== b.issue_date) return a.issue_date < b.issue_date ? -1 : 1;
      return a.id - b.id;
    });
    autoCollapseNewDates();
    return;
  }

  S.issues = await api('/api/issues?' + p);
  autoCollapseNewDates();
}

// 새로 로드된 이슈 중 사용자가 손대지 않은 날짜는 자동으로 접기
function autoCollapseNewDates() {
  for (const i of S.issues) {
    if (i.issue_date && !S.userToggledDates.has(i.issue_date)) {
      S.collapsedDates.add(i.issue_date);
    }
  }
}

// ───────────── Tabs / Filter / Summary ─────────────
function renderTabs() {
  const bar = $('#tab-bar');
  bar.innerHTML = '';
  // 손유석 단독 운영 — '전체' 탭 및 타 감독 탭 제거 (탭 카운트는 진행중만)
  for (const s of S.supervisors) {
    if ((s.name || '').trim() !== ONLY_SUP_NAME) continue;
    const active = (s.open_count || 0) + (s.progress_count || 0);
    bar.append(tabEl(s.id, s.name, s.color, active, S.activeTab == s.id));
  }
  renderSubTabs();
}

// 서브 탭 (진행중 = Open+InProgress / 완료 = Closed)
function renderSubTabs() {
  const bar = $('#subtab-bar');
  bar.innerHTML = '';

  // 카운트 계산 (현재 활성 메인 탭 기준)
  let openCnt = 0, doneCnt = 0;
  if (S.activeTab === 'all') {
    for (const s of S.supervisors) {
      openCnt += (s.open_count || 0) + (s.progress_count || 0);
      doneCnt += (s.closed_count || 0);
    }
  } else {
    const s = S.supervisors.find(x => x.id == S.activeTab);
    if (s) {
      openCnt = (s.open_count || 0) + (s.progress_count || 0);
      doneCnt = (s.closed_count || 0);
    }
  }

  // 손유석 단독 운영 — '전체'·'요약' 서브탭을 손유석 탭에 그대로 노출(옛 '전체' 부모탭에서 이동).
  const showAllSummary = true;
  if (showAllSummary) {
    bar.append(subtabEl('all',  '전체',   openCnt + doneCnt, S.activeSubTab === 'all'));
  }
  bar.append(subtabEl('open',   '진행중', openCnt, S.activeSubTab === 'open'));
  bar.append(subtabEl('closed', '완료',   doneCnt, S.activeSubTab === 'closed'));
  if (showAllSummary) {
    const sumCnt = S.summaryCounts[String(S.activeTab)];  // 손유석 scope 카운트만(없으면 배지 없음)
    bar.append(subtabEl('summary', '요약', (sumCnt === undefined ? null : sumCnt), S.activeSubTab === 'summary'));
  }
}

function subtabEl(id, label, count, active) {
  const t = el('div', {
    class: 'subtab' + (active ? ' active' : ''),
    'data-sub': id,
    onclick: () => switchSubTab(id),
  },
    el('span', { class: 'subtab-dot' }),
    label,
  );
  if (count !== null && count !== undefined) {
    t.append(el('span', { class: 'subtab-count' }, String(count)));
  }
  return t;
}

async function switchSubTab(id) {
  if (S.activeSubTab === id) return;
  S.activeSubTab = id;
  try { localStorage.setItem('trmt_subtab', id); } catch (_) {}
  S.inlineAdd = null;
  S.quickFilter = 'all';                 // 서브탭 전환 시 빠른필터 초기화(빈 화면 혼선 방지)
  renderSubTabs();
  renderTabContext();
  await loadIssues();
  render();
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

  // 비동기로 활성 이슈 카운트 받아서 옵션 라벨에 추가
  refreshVesselFilterCounts();
}

async function refreshVesselFilterCounts() {
  const sel = $('#filter-vessel');
  if (!sel || !S.vessels.length) return;

  const p = new URLSearchParams();
  if (S.activeTab !== 'all')   p.set('supervisor_id', S.activeTab);
  if (S.filters.q)             p.set('q', S.filters.q);
  if (S.filters.vessel_type)   p.set('vessel_type', S.filters.vessel_type);
  if (S.filters.priority)      p.set('priority', S.filters.priority);
  // vessel_id는 의도적으로 제외 (드롭다운 라벨용)

  let counts = {};
  try {
    counts = await api('/api/vessels/active-counts?' + p);
  } catch (e) {
    return;   // 실패해도 조용히 — 기본 라벨 유지
  }

  // 옵션 라벨 갱신 ("All 선박"은 총합으로 업데이트)
  const total = Object.values(counts).reduce((a, b) => a + (b || 0), 0);
  for (const opt of sel.options) {
    if (opt.value === '') {
      opt.textContent = `All 선박 (${total})`;
    } else {
      const v = S.vessels.find(x => String(x.id) === opt.value);
      if (!v) continue;
      const c = counts[opt.value] || 0;
      opt.textContent = c > 0 ? `${v.name}  ·  ${c}건` : v.name;
    }
  }
}
function renderTabContext() {
  const c = $('#tab-context');
  c.innerHTML = '';

  const isClosedSub = S.activeSubTab === 'closed';
  const isAllSub = S.activeSubTab === 'all';

  if (S.activeTab === 'all') {
    const open = S.supervisors.reduce((a,s)=>a+s.open_count, 0);
    const prog = S.supervisors.reduce((a,s)=>a+s.progress_count, 0);
    const done = S.supervisors.reduce((a,s)=>a+s.closed_count, 0);
    const parts = [`전체 감독 · <strong>${S.supervisors.length}</strong>명`];
    if (isClosedSub) {
      parts.push(`Closed <strong>${done}</strong>`);
    } else if (isAllSub) {
      parts.push(`Open <strong>${open}</strong>`);
      parts.push(`진행중 <strong>${prog}</strong>`);
      parts.push(`Closed <strong>${done}</strong>`);
    } else {
      parts.push(`Open <strong>${open}</strong>`);
      parts.push(`진행중 <strong>${prog}</strong>`);
    }
    c.innerHTML = parts.join(' · ');
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

  const ctx = el('span', { style: 'margin-left: 10px;' });
  if (isClosedSub) {
    ctx.append('· Closed ', el('strong', {}, String(s.closed_count)));
  } else if (isAllSub) {
    ctx.append(
      '· Open ', el('strong', {}, String(s.open_count)),
      ' · 진행중 ', el('strong', {}, String(s.progress_count)),
      ' · Closed ', el('strong', {}, String(s.closed_count)),
    );
  } else {
    ctx.append(
      '· Open ', el('strong', {}, String(s.open_count)),
      ' · 진행중 ', el('strong', {}, String(s.progress_count)),
    );
  }
  c.append(ctx);
}
function renderSummary() {
  const n  = S.issues.length;
  const op = S.issues.filter(i => i.status === 'Open').length;
  const pg = S.issues.filter(i => i.status === 'InProgress').length;
  const cl = S.issues.filter(i => i.status === 'Closed').length;

  const isClosedSub = S.activeSubTab === 'closed';
  const isAllSub = S.activeSubTab === 'all';
  const parts = [`<span>총 <strong>${n}</strong>건</span>`];
  if (isClosedSub) {
    parts.push(`<span>· Closed <strong>${cl}</strong></span>`);
  } else if (isAllSub) {
    parts.push(`<span>· Open <strong>${op}</strong></span>`);
    parts.push(`<span>· 진행중 <strong>${pg}</strong></span>`);
    parts.push(`<span>· Closed <strong>${cl}</strong></span>`);
  } else {
    parts.push(`<span>· Open <strong>${op}</strong></span>`);
    parts.push(`<span>· 진행중 <strong>${pg}</strong></span>`);
  }
  $('#summary-row').innerHTML = parts.join('');
  $('#count-label').textContent = `${n} items`;
}

// ───────────── Render — main ─────────────
function render() {
  const isSummary = S.activeSubTab === 'summary';
  // 뷰 전환: 요약이면 daily-body(선박 2단) 숨기고 summary-wrap 표시
  const db = $('#daily-body'), sw = $('#summary-wrap'), sr = $('#summary-row');
  if (sw) sw.hidden = !isSummary;
  if (db) db.hidden = isSummary;
  if (isSummary) {
    if (sr) sr.innerHTML = '';
    renderSummaryView();
    return;
  }
  // 선박별 보기
  ensureSelectedVessel();
  renderVesselSidebar();
  renderVmainHead();
  const g = curVesselGroup();
  const has = !!g && g.issues.length > 0;
  $('#empty-state').hidden = has || !!S.inlineAdd;
  renderTable();
  renderCards();
  renderSummary();
  refreshVesselFilterCounts();
}

// 요약 서브탭 뷰 렌더
function renderSummaryView() {
  const tbody = $('#summary-tbody');
  const meta = $('#summary-meta');
  const empty = $('#summary-empty');
  if (!tbody) return;
  tbody.innerHTML = '';
  const data = S.summary || { rows: [], generated_at: null };
  let rows = data.rows || [];

  // 툴바 필터 적용 (저장된 요약 행을 화면에서 필터링)
  const f = S.filters;
  // 선박/선종 역조회용 맵 + 상태 라벨 맵 (옛 요약 데이터에 새 필드가 없어도 동작)
  const vById = {}, vByName = {};
  for (const v of S.vessels) { vById[v.id] = v; vByName[(v.name || '').toLowerCase()] = v; }
  const STAT_LABEL = { Open: 'Open', InProgress: '진행중', Closed: 'Closed' };

  rows = rows.filter(r => {
    const vByNameHit = vByName[(r.vessel_name || '').toLowerCase()];
    // 선박: vessel_id 우선, 없으면 선박명으로
    if (f.vessel_id) {
      const rid = r.vessel_id || (vByNameHit && vByNameHit.id);
      if (String(rid) !== String(f.vessel_id)) return false;
    }
    // 선종: row.vessel_type 우선, 없으면 선박명으로 조회
    if (f.vessel_type) {
      const vt = r.vessel_type || (vByNameHit && vByNameHit.vessel_type) || '';
      if (vt !== f.vessel_type) return false;
    }
    // 상태: status_raw 우선, 없으면 표시 라벨로 매칭
    if (f.status) {
      if (r.status_raw) {
        if (r.status_raw !== f.status) return false;
      } else if ((r.status || '') !== (STAT_LABEL[f.status] || f.status)) {
        return false;
      }
    }
    if (f.priority && r.priority !== f.priority) return false;
    if (f.q) {
      const q = f.q.toLowerCase();
      if (!((r.issue || '') + (r.vessel_name || '')).toLowerCase().includes(q)) return false;
    }
    return true;
  });

  if (meta) {
    const total = (data.rows || []).length;
    const shown = rows.length;
    meta.textContent = data.generated_at
      ? `마지막 갱신: ${data.generated_at}  ·  ${shown}/${total}건`
      : '';
  }
  if (!rows.length) {
    if (empty) { empty.hidden = false; empty.innerHTML = (data.generated_at ? '필터 조건에 맞는 항목이 없습니다.' : '아직 요약이 없습니다. 상단 <strong>"업무 요약"</strong> 버튼을 눌러 생성하세요.'); }
    return;
  }
  if (empty) empty.hidden = true;
  let n = 0;
  for (const r of rows) {
    n++;
    const linkBtn = el('button', {
      class: 'icon-btn', title: '원본 이슈로 이동',
      onclick: () => gotoIssueFromSummary(r),
    });
    linkBtn.innerHTML = `<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2">
      <path d="M10 13a5 5 0 007.54.54l3-3a5 5 0 00-7.07-7.07l-1.72 1.71"/>
      <path d="M14 11a5 5 0 00-7.54-.54l-3 3a5 5 0 007.07 7.07l1.71-1.71"/></svg>`;
    tbody.append(el('tr', {},
      el('td', { 'data-label': 'No.', style: 'text-align:center;vertical-align:top;' }, String(n)),
      el('td', { 'data-label': '선박', style: 'vertical-align:top;' }, r.vessel_name || ''),
      el('td', { 'data-label': '현안업무', class: 'sum-issue', style: 'white-space:pre-wrap;vertical-align:top;line-height:1.5;' }, r.issue || ''),
      el('td', { 'data-label': 'Priority', style: 'text-align:center;vertical-align:top;' }, r.priority || ''),
      el('td', { 'data-label': 'Status', style: 'text-align:center;vertical-align:top;' }, (r.status || '')),
      el('td', { 'data-label': '링크', style: 'text-align:center;vertical-align:top;' }, linkBtn),
    ));
  }
}

// 요약 행 → 원본 이슈로 이동 (전체 대분류/전체 소분류 + 제목 검색 필터)
// 요약 행에서 검색용 제목 추출: item 필드 우선, 없으면 현안업무 첫 줄에서 [M/D] 제거
function summaryTitle(r) {
  if (r.item && String(r.item).trim()) return String(r.item).trim();
  const first = (r.issue || '').split('\n')[0] || '';
  return first.replace(/^\s*\[[^\]]*\]\s*/, '').trim();
}

async function gotoIssueFromSummary(r) {
  const title = summaryTitle(r);
  S.activeTab = onlySupId();
  S.activeSubTab = 'all';
  try { localStorage.setItem('trmt_subtab', 'all'); } catch (_) {}
  // 제목으로 검색(q) — 직접 검색한 것과 동일하게 필터링
  S.filters.q = title;
  S.filters.item_topic = title;   // 새 서버면 정확일치까지 적용되어 딱 하나만
  S.filters.vessel_id = '';
  S.filters.vessel_type = '';
  S.filters.status = '';
  S.filters.priority = '';
  await loadVessels(null);
  renderTabs();
  renderVesselFilter();
  renderTabContext();
  const sb = $('#filter-search'); if (sb) sb.value = title;
  ['#filter-vessel', '#filter-vessel-type', '#filter-status', '#filter-priority']
    .forEach(sel => { const e = $(sel); if (e) e.value = ''; });
  await loadIssues();
  // 매칭 이슈 → 해당 선박 선택 + 펼침 + 링크필터 활성(해제 칩 표시용)
  const tlc = title.toLowerCase();
  const target = S.issues.find(i => (i.item_topic || '').trim().toLowerCase() === tlc)
              || S.issues.find(i => ((i.item_topic || '') + (i.description || '')).toLowerCase().includes(tlc));
  if (target) {
    S.selectedVessel = (target.vessel_id != null) ? target.vessel_id : '__none__';
    S.expandedRows.add(target.id); S.expandedActions.add(target.id);
  }
  S.linkFilter = title;
  // 펼친 상태로 표시
  S.collapsedMonths.clear();
  S.collapsedDates.clear();
  for (const i of S.issues) {
    if (i.issue_date) S.userToggledDates.add(i.issue_date);
  }
  render();
  if (target) flashIssue(target.id);   // 해당 항목으로 스크롤 + 하이라이트
}

// 링크 필터(요약→이슈) 해제: 검색바가 숨겨진 상태에서도 전체 보기로 복귀
async function clearLinkFilter() {
  S.linkFilter = null;
  S.filters.q = ''; S.filters.item_topic = '';
  const sb = $('#filter-search'); if (sb) sb.value = '';
  await loadIssues();
  render();
}

// 특정 이슈 카드/행으로 스크롤 + 잠깐 하이라이트
function flashIssue(id) {
  if (id == null) return;
  setTimeout(() => {
    const node = document.querySelector(
      `.issue-card[data-id="${id}"], tr.data-row[data-id="${id}"]`);
    if (!node) return;
    node.scrollIntoView({ behavior: 'smooth', block: 'center' });
    node.classList.add('flash-hl');
    setTimeout(() => node.classList.remove('flash-hl'), 2600);
  }, 140);
}

// ═══════════════ 선박별 보기 (rev.4) ═══════════════
const VTYPE_ORDER = ['VLCC', 'LR', 'AFRAMAX', 'MR', 'CNTR'];
const RISK_PRI = new Set(['COC & Flag', 'Urgent']);
const isActiveStatus = (s) => s === 'Open' || s === 'InProgress';
function vtypeRank(t) { const i = VTYPE_ORDER.indexOf((t || '').toUpperCase()); return i < 0 ? VTYPE_ORDER.length : i; }

// 선박 단위 집계. 베이스=S.vessels(담당 전 선박 → 0건 선박도 표시), 거기에 S.issues 병합.
// vessel_id null(미배정) 이슈는 별도 '(미배정)' 그룹으로(소실 방지). active=진행중+Open, risk=활성 COC&Flag/Urgent.
function vesselGroups() {
  const byId = new Map();
  for (const v of S.vessels) {
    byId.set(String(v.id), { id: v.id, name: v.name, type: v.vessel_type || '',
                             issues: [], active: 0, risk: false, latest: '' });
  }
  let unassigned = null;
  for (const i of S.issues) {
    let g;
    if (i.vessel_id == null) {
      if (!unassigned) unassigned = { id: '__none__', name: '(미배정)', type: '기타',
                                      issues: [], active: 0, risk: false, latest: '', unassigned: true };
      g = unassigned;
    } else {
      g = byId.get(String(i.vessel_id));
      if (!g) {                                 // S.vessels에 없는(비활성 등) 선박이 이슈 보유
        g = { id: i.vessel_id, name: i.vessel_name || '(선박)', type: '',
              issues: [], active: 0, risk: false, latest: '' };
        byId.set(String(i.vessel_id), g);
      }
    }
    g.issues.push(i);
    if (isActiveStatus(i.status)) { g.active++; if (RISK_PRI.has(i.priority)) g.risk = true; }
    if ((i.issue_date || '') > g.latest) g.latest = i.issue_date || '';
  }
  const arr = [...byId.values()];
  if (unassigned) arr.push(unassigned);
  return arr;
}

// 선종별 묶음 → 선종순서(VLCC→LR→AFRAMAX→MR→CNTR), 그룹 내 고위험 우선 → 활성수 → 최근발생
function sidebarGroups() {
  const byType = new Map();
  for (const g of vesselGroups()) {
    const key = (g.type || '').toUpperCase() || '기타';
    if (!byType.has(key)) byType.set(key, []);
    byType.get(key).push(g);
  }
  const types = [...byType.keys()].sort((a, b) => (vtypeRank(a) - vtypeRank(b)) || a.localeCompare(b));
  // 커스텀 순서(유저 드래그) 우선 — order에 있는 선박은 그 순서대로, 없으면 기본정렬(디펙트순) 뒤에.
  const ord = S.vesselOrder || [];
  const oidx = id => { const i = ord.indexOf(Number(id)); return i < 0 ? 1e9 : i; };
  for (const t of types) {
    byType.get(t).sort((a, b) =>
      (oidx(a.id) - oidx(b.id)) ||
      (b.risk - a.risk) || (b.active - a.active) ||
      (b.latest < a.latest ? -1 : b.latest > a.latest ? 1 : 0) || a.name.localeCompare(b.name));
  }
  return types.map(t => ({ type: t, vessels: byType.get(t) }));
}

function curVesselGroup() {
  return vesselGroups().find(x => String(x.id) === String(S.selectedVessel)) || null;
}

// 메인 표시용 이슈: 빠른필터 + 정렬
function displayIssues(all) {
  let arr = all.slice();
  const asc = (a, b) => (a.issue_date < b.issue_date ? -1 : a.issue_date > b.issue_date ? 1 : 0) || (a.id - b.id);
  const desc = (a, b) => -asc(a, b);
  const PR = { 'COC & Flag': 0, 'Urgent': 1, 'Next DD': 2, 'Normal': 3 };
  const kw = (S.issueSearch || '').trim().toLowerCase();
  if (kw) arr = arr.filter(i => issueMatchesKw(i, kw));     // 선택 선박 내 키워드 검색
  const qf = S.quickFilter;
  if (qf === 'risk') arr = arr.filter(i => RISK_PRI.has(i.priority) && isActiveStatus(i.status));
  else if (qf === 'stale') arr = arr.filter(i => isActiveStatus(i.status));
  if (qf === 'recent') arr.sort(desc);
  else if (qf === 'stale') arr.sort(asc);                 // 장기 미종결 = 오래된 활성 먼저
  else if (S.mainSort === 'new') arr.sort((a, b) => ((PR[a.priority] ?? 9) - (PR[b.priority] ?? 9)) || desc(a, b));
  else arr.sort(asc);
  return arr;
}

function ensureSelectedVessel() {
  const flat = sidebarGroups().flatMap(g => g.vessels);
  if (!flat.length) { S.selectedVessel = null; return; }
  if (S.selectedVessel == null || !flat.some(v => String(v.id) === String(S.selectedVessel)))
    S.selectedVessel = flat[0].id;
}
function selectVessel(vid) { S.selectedVessel = vid; S.inlineAdd = null; S.issueSearch = ''; render(); }

// 선박 내 키워드 매칭(제목/상세/조치 이력)
function issueMatchesKw(i, kw) {
  if (!kw) return true;
  const hay = [i.item_topic, i.description,
    ...(Array.isArray(i.actions) ? i.actions.map(a => a && a.progress) : [])]
    .filter(Boolean).join(' ').toLowerCase();
  return hay.includes(kw);
}

// 시간순 No. (1 = 가장 오래된 발생일). 표시 정렬이 바뀌어도 번호는 발생순 고정.
function chronoNoMap(issues) {
  const m = new Map();
  issues.slice()
    .sort((a, b) => (a.issue_date < b.issue_date ? -1 : a.issue_date > b.issue_date ? 1 : 0) || (a.id - b.id))
    .forEach((i, idx) => m.set(i.id, idx + 1));
  return m;
}

function renderTable() {
  const tbody = $('#issue-tbody');
  tbody.innerHTML = '';
  const g = curVesselGroup();
  if (!g) { if (S.inlineAdd) tbody.append(inlineAddRow()); return; }
  const noMap = chronoNoMap(g.issues);
  const rows = displayIssues(g.issues);
  for (const i of rows) {
    tbody.append(rowEl(i, noMap.get(i.id)));
    if (S.expandedRows.has(i.id)) tbody.append(expandedRowEl(i));   // 펼침 카드 행
  }
  if (!rows.length) {
    tbody.append(el('tr', {}, el('td', { colspan: '5', style: 'padding:22px;text-align:center;color:var(--text-tertiary)' },
      S.quickFilter === 'all' ? '이 선박의 이슈가 없습니다.' : '이 필터에 해당하는 이슈가 없습니다.')));
  }
}

// 사이드바 배지 숫자 = subtab 의미(open=활성/closed=완료/all=전체)
function vBadgeCount(v) {
  if (S.activeSubTab === 'closed') return v.issues.filter(i => i.status === 'Closed').length;
  if (S.activeSubTab === 'all') return v.issues.length;
  return v.active;                                  // 'open'(기본) = 진행중+Open
}
function subtabCountLabel() {
  return S.activeSubTab === 'closed' ? '완료' : S.activeSubTab === 'all' ? '전체' : '활성';
}
function renderVesselSidebar() {
  const sb = $('#vessel-sidebar');
  if (!sb) return;
  sb.innerHTML = '';
  const groups = sidebarGroups();
  const totV = groups.reduce((a, g) => a + g.vessels.length, 0);
  const totC = groups.reduce((a, g) => a + g.vessels.reduce((x, v) => x + vBadgeCount(v), 0), 0);
  const head = el('div', { class: 'vsb-head' },
    el('span', { class: 'vsb-t' }, '선박'),
    el('span', { class: 'vsb-n' }, `${totV}척 · ${subtabCountLabel()} ${totC}`));
  if ((S.vesselOrder || []).length) {
    const reset = el('button', { class: 'vsb-reset', title: '커스텀 순서 해제 → 기본(디펙트순)' }, '기본순');
    reset.addEventListener('click', () => { saveVesselOrder([]).then(() => fillVesselList()); });
    head.append(reset);
  }
  sb.append(head);
  // 검색 input은 한 번만 생성(재생성 금지 → 타이핑 중 포커스 유지). 입력 시 리스트만 갱신.
  const search = el('input', { class: 'vsb-search', type: 'text', placeholder: '선박 검색…' });
  search.value = S._vsbq || '';
  search.addEventListener('input', (e) => { S._vsbq = e.target.value; fillVesselList(); });
  sb.append(el('div', { class: 'vsb-search-wrap' }, search));
  sb.append(el('div', { class: 'vsb-list', id: 'vsb-list' }));
  fillVesselList();
}

// 사이드바 선박 리스트만 갱신(검색 필터 적용). input 요소는 건드리지 않음.
function fillVesselList() {
  const list = $('#vsb-list');
  if (!list) return;
  list.innerHTML = '';
  const groups = sidebarGroups();
  const q = (S._vsbq || '').trim().toLowerCase();
  let shown = 0;
  const dragOff = !!q;   // 검색 중엔 드래그 비활성(부분목록 순서 저장 방지)
  const groupBoxes = [];
  for (const grp of groups) {
    const vis = grp.vessels.filter(v => !q || v.name.toLowerCase().includes(q));
    if (!vis.length) continue;
    list.append(el('div', { class: 'vsb-group' }, el('span', {}, grp.type), el('span', { class: 'vsb-gc' }, `${vis.length}척`)));
    const box = el('div', { class: 'vsb-group-items' });
    for (const v of vis) {
      shown++;
      const cnt = vBadgeCount(v);
      const badge = el('span', { class: 'vsb-badge' + (v.risk ? ' risk' : '') + (cnt === 0 ? ' zero' : '') },
        v.risk ? el('span', { class: 'vsb-flag' }, '⚑') : null, String(cnt));
      const handle = el('span', { class: 'vsb-drag', title: '드래그로 순서 변경' }, '≡');
      const item = el('div', {
        class: 'vsb-item' + (String(S.selectedVessel) === String(v.id) ? ' active' : ''),
        'data-vid': String(v.id),
      }, handle, el('span', { class: 'vsb-nm' }, v.name), badge);
      item.addEventListener('click', (e) => { if (!e.target.closest('.vsb-drag')) selectVessel(v.id); });
      box.append(item);
    }
    list.append(box);
    if (!dragOff) groupBoxes.push(box);
  }
  if (!shown) { list.append(el('div', { class: 'vsb-empty' }, q ? '검색 결과 없음' : '표시할 선박 없음')); return; }
  // SortableJS 드래그앤드롭(그룹 내에서만). 끝나면 전체 표시순을 서버에 저장.
  if (window.Sortable && !dragOff) {
    for (const box of groupBoxes) {
      Sortable.create(box, {
        handle: '.vsb-drag', animation: 150, delay: 120, delayOnTouchOnly: true,
        onEnd: () => {
          const order = [...document.querySelectorAll('#vsb-list .vsb-item[data-vid]')]
            .map(e => Number(e.getAttribute('data-vid')));
          saveVesselOrder(order).then(() => fillVesselList());
        },
      });
    }
  }
}

function renderVmainHead() {
  const h = $('#vmain-head');
  if (!h) return;
  h.innerHTML = '';
  // 링크 필터(요약→이슈)가 걸려 있으면 해제 칩 노출 — 검색바 숨김 상태에서 푸는 유일 경로
  const lf = S.linkFilter || S.filters.q || S.filters.item_topic;
  if (lf) {
    const chip = el('div', { class: 'vmh-linkfilter' },
      el('span', { class: 'vmh-lf-lbl' }, '🔍 링크 필터'),
      el('b', { class: 'vmh-lf-q' }, lf),
      el('button', { class: 'vmh-lf-x', title: '필터 해제 — 전체 보기로' }, '✕ 해제'));
    chip.querySelector('.vmh-lf-x').addEventListener('click', clearLinkFilter);
    h.append(chip);
  }
  const groups = sidebarGroups();
  if (groups.length) {                                    // 모바일 선박 선택(사이드바 대체)
    const msel = el('select', { class: 'vmh-vsel' });
    for (const grp of groups) {
      const og = el('optgroup', { label: grp.type });
      for (const v of grp.vessels) {
        const o = el('option', { value: String(v.id) }, `${v.name}  (${v.risk ? '⚑' : ''}${vBadgeCount(v)})`);
        if (String(v.id) === String(S.selectedVessel)) o.selected = true;
        og.append(o);
      }
      msel.append(og);
    }
    msel.addEventListener('change', (e) => selectVessel(e.target.value));   // 문자열 그대로(__none__ 대응)
    h.append(msel);
  }
  const g = curVesselGroup();
  if (!g) return;
  const done = g.issues.filter(i => i.status === 'Closed').length;
  // 선박 내 검색(제목/상세/조치). 입력 시 테이블/카드만 갱신 → 포커스 유지.
  const isearch = el('input', { class: 'vmh-search', type: 'text', placeholder: '이 선박 내 검색 (제목·상세·조치)' });
  isearch.value = S.issueSearch || '';
  isearch.addEventListener('input', (e) => { S.issueSearch = e.target.value; renderTable(); renderCards(); });
  // 이 선박으로 신규 이슈 추가(openNew가 현재 선택 선박 자동 세팅) — 상단 버튼을 이 박스로 이동
  const newBtn = el('button', { class: 'btn btn-primary btn-sm vmh-add', title: '이 선박으로 신규 이슈 추가' }, '+ 신규 이슈');
  newBtn.addEventListener('click', openNew);
  h.append(el('div', { class: 'vmh-row' },
    el('span', { class: 'vmh-name' }, g.name),
    g.type ? el('span', { class: 'vmh-type' }, g.type) : null,
    isearch,
    el('span', { class: 'vmh-kpi' },
      el('b', { class: 'k-open' }, String(g.active)), ' 진행중+Open',
      el('span', { class: 'k-dim' }, ' · 완료 '), el('b', { class: 'k-dim' }, String(done)),
      el('span', { class: 'k-dim' }, ' · 전체 '), el('b', { class: 'k-dim' }, String(g.issues.length))),
    newBtn));
  const QF = [['all', '전체'], ['recent', '최근 발생'], ['stale', '장기 미종결'], ['risk', '⚑ COC·Urgent']];
  const qf = el('div', { class: 'vmh-qf' });
  for (const [k, label] of QF) {
    const c = el('span', { class: 'qchip' + (S.quickFilter === k ? ' on' : '') + (k === 'risk' ? ' risk' : '') }, label);
    c.addEventListener('click', () => { S.quickFilter = k; render(); });
    qf.append(c);
  }
  const sortBtn = el('button', { class: 'vmh-sort' }, '정렬: ', el('b', {}, S.mainSort === 'old' ? '오래된→최근' : '최근·우선순위'), ' ⇅');
  sortBtn.addEventListener('click', () => {
    S.mainSort = S.mainSort === 'old' ? 'new' : 'old';
    try { localStorage.setItem('trmt_main_sort', S.mainSort); } catch (_) {}
    render();
  });
  h.append(el('div', { class: 'vmh-tools' }, qf, sortBtn));
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
  S.userToggledDates.add(d);   // 사용자가 직접 토글했음 — 자동 접기에서 제외
  renderTable(); renderCards();
}

// ───────────── Row 렌더 (셀별 인라인 편집) ─────────────
// 컴팩트 행: No | 발생일 | 현안업무(클릭=펼침) | Priority(인라인) | Status(인라인)
function rowEl(i, no) {
  const expanded = S.expandedRows.has(i.id);
  const tr = el('tr', { class: 'data-row' + (expanded ? ' is-expanded' : ''), 'data-id': i.id });

  tr.append(el('td', { class: 'no-cell' }, String(no)));
  tr.append(el('td', { class: 'date-cell' }, i.issue_date || '-'));

  // 현안업무 — 클릭 시 행 펼치기(상세/진행사항)
  const topicTd = el('td', { class: 'topic-cell topic-expand', title: '클릭하여 상세·진행사항 펼치기' });
  const line = el('div', { class: 'topic-line' },
    el('span', { class: 'row-caret' }, expanded ? '▾' : '▸'),
    el('span', { class: 'topic-text' }, i.item_topic));
  topicTd.append(line);
  if (S.activeTab === 'all') {
    topicTd.append(el('div', { class: `sup-chip c-${i.supervisor_color}` },
      el('span', { class: `tab-dot dot-${i.supervisor_color}` }), i.supervisor_name));
  }
  if (i.att_count > 0) topicTd.append(el('span', { class: 'topic-att' }, `📎 ${i.att_count}`));
  topicTd.addEventListener('click', () => toggleRow(i.id));
  tr.append(topicTd);

  // Priority (+D-day) — 인라인 변경
  const priTd = el('td', { class: 'cell-edit', title: '클릭하여 우선순위 / 마감일 편집' });
  const priStack = el('div', { class: 'pri-stack' }, priBadge(i.priority));
  const ddBd = dDayBadge(i.due_date);
  if (ddBd) priStack.append(ddBd);
  else priStack.append(el('span', { class: 'dday dday-later', style: 'opacity:0.5; cursor:pointer', title: '마감일 설정' }, '+ 마감'));
  priTd.append(priStack);
  priTd.addEventListener('click', (ev) => {
    ev.stopPropagation();
    if (ev.target.closest('.dday')) startEditInline(priTd, i, 'due_date', 'date');
    else startEditSelect(priTd, i, 'priority', [['Normal', 'Normal'], ['Urgent', 'Urgent'], ['Next DD', 'Next DD'], ['COC & Flag', 'COC & Flag']]);
  });
  tr.append(priTd);

  // Status — 인라인 변경
  const statTd = el('td', { class: 'cell-edit', title: '클릭하여 상태 변경' }, statBadge(i.status));
  statTd.addEventListener('click', (ev) => {
    ev.stopPropagation();
    startEditSelect(statTd, i, 'status', [['Open', 'Open'], ['InProgress', '진행중'], ['Closed', 'Closed']]);
  });
  tr.append(statTd);
  return tr;
}

function toggleRow(id) {
  if (S.expandedRows.has(id)) { S.expandedRows.delete(id); S.expandedActions.delete(id); }
  else { S.expandedRows.add(id); S.expandedActions.add(id); }   // 펼치면 조치 이력 전체 표시
  renderTable(); renderCards();
}

// 펼침 행: 상세 내용 + 진행사항(조치 이력) + 액션 버튼
function expandedRowEl(i) {
  const tr = el('tr', { class: 'exp-row', 'data-exp-id': i.id });
  const box = el('div', { class: 'exp-box' });

  // 상세 내용 (클릭 인라인 편집)
  box.append(el('div', { class: 'exp-label' }, '상세 내용'));
  const desc = el('div', { class: 'exp-desc cell-edit', title: '클릭하여 상세 편집' }, i.description || '—');
  desc.addEventListener('click', (ev) => { ev.stopPropagation(); startEditInline(desc, i, 'description', 'textarea'); });
  box.append(desc);

  // 진행사항 (조치 이력) — 기존 액션 편집 UI 재사용(CSS로 타임라인화)
  box.append(el('div', { class: 'exp-label' }, '진행사항 (조치 이력)'));
  box.append(el('div', { class: 'exp-acts-wrap' }, renderActionCell(i)));

  // 버튼
  const acts = el('div', { class: 'exp-btns' });
  acts.append(mkTextBtn('상세 / 편집 열기', 'pri', () => openEdit(i.id)));
  acts.append(mkTextBtn(i.att_count > 0 ? `첨부 (${i.att_count})` : '첨부', '', () => openAttach(i.id)));
  acts.append(mkTextBtn('일정 등록', '', () => addIssueToCalendar(i)));
  acts.append(mkTextBtn('삭제', 'danger', () => confirmDelete(i.id)));
  box.append(acts);

  tr.append(el('td', { colspan: '5' }, box));
  return tr;
}

function mkTextBtn(label, kind, onclick) {
  return el('button', {
    type: 'button',
    class: 'exp-btn' + (kind === 'pri' ? ' pri' : kind === 'danger' ? ' danger' : ''),
    onclick: (ev) => { ev.stopPropagation(); onclick(); },
  }, label);
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
    calendar: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
      <rect x="3" y="4" width="18" height="18" rx="2" ry="2"/>
      <line x1="16" y1="2" x2="16" y2="6"/>
      <line x1="8" y1="2" x2="8" y2="6"/>
      <line x1="3" y1="10" x2="21" y2="10"/></svg>`,
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
    // 액션 UI는 데스크탑=별도 펼침행(tr.exp-row[data-exp-id]), 모바일=카드(.issue-card[data-id])에 있음
    const cont = document.querySelector(
      `tr[data-exp-id="${issue.id}"] .act-cell-wrap, .issue-card[data-id="${issue.id}"] .act-cell-wrap`);
    if (!cont) return;
    const entries = cont.querySelectorAll('.act-entry');
    const last = entries[entries.length - 1];
    if (last) startEditActionEntry(last, issue, issue.actions.length - 1);
  }, 30);
}

// ───────────── Cards (모바일) ─────────────
function renderCards() {
  const list = $('#card-list');
  list.innerHTML = '';
  const g = curVesselGroup();
  if (!g) return;
  const noMap = chronoNoMap(g.issues);
  const rows = displayIssues(g.issues);
  if (!rows.length) {
    list.append(el('div', { style: 'padding:20px;text-align:center;color:var(--text-tertiary);font-size:12.5px' },
      S.quickFilter === 'all' ? '이 선박의 이슈가 없습니다.' : '이 필터에 해당하는 이슈가 없습니다.'));
    return;
  }
  for (const i of rows) list.append(cardEl(i, noMap.get(i.id)));
}

function inlineAddCardHint() {
  return el('div', {
    style: 'background:var(--blue-bg); border:1px solid var(--blue-border); padding:10px 12px; border-radius:8px; font-size:12px; color:var(--blue-text); margin-bottom:10px',
  }, '📝 데스크톱에서 상단 인라인 입력 폼을 이용해 새 이슈를 추가하세요.');
}

function cardEl(i, no) {
  const expanded = S.expandedRows.has(i.id);
  const card = el('div', { class: 'issue-card' + (expanded ? ' is-expanded' : ''), 'data-id': i.id });
  // 카드 탭 → 펼침(상세+진행사항). 내부 버튼/액션은 제외.
  card.addEventListener('click', (ev) => {
    if (ev.target.closest('.icon-btn') || ev.target.closest('.act-arrow') ||
        ev.target.closest('.exp-btn') || ev.target.closest('.act-add-inline') ||
        ev.target.closest('.act-entry')) return;
    toggleRow(i.id);
  });

  const head = el('div', { class: 'issue-card-head' });
  if (no != null) head.append(el('span', { class: 'issue-card-no' }, 'No.' + no));
  head.append(el('span', { class: 'card-caret' }, expanded ? '▾' : '▸'));
  if (i.issue_date) head.append(el('span', { class: 'card-date' }, i.issue_date));
  if (S.activeTab === 'all') {
    head.append(el('span', { class: `sup-chip c-${i.supervisor_color}` },
      el('span', { class: `tab-dot dot-${i.supervisor_color}` }), i.supervisor_name));
  }
  head.append(priBadge(i.priority));
  const dd = dDayBadge(i.due_date);
  if (dd) head.append(dd);
  head.append(statBadge(i.status));
  card.append(head);

  card.append(el('div', { class: 'issue-card-body' },
    el('div', { class: 'issue-card-title' }, i.item_topic)));

  if (expanded) {
    const det = el('div', { class: 'issue-card-det' });
    det.append(el('div', { class: 'exp-label' }, '상세 내용'));
    det.append(el('div', { class: 'exp-desc' }, i.description || '—'));
    det.append(el('div', { class: 'exp-label' }, '진행사항 (조치 이력)'));
    det.append(el('div', { class: 'exp-acts-wrap' }, renderActionCell(i)));
    const acts = el('div', { class: 'exp-btns' });
    acts.append(mkTextBtn('상세 / 편집', 'pri', () => openEdit(i.id)));
    acts.append(mkTextBtn(i.att_count > 0 ? `첨부 (${i.att_count})` : '첨부', '', () => openAttach(i.id)));
    acts.append(mkTextBtn('일정', '', () => addIssueToCalendar(i)));
    acts.append(mkTextBtn('삭제', 'danger', () => confirmDelete(i.id)));
    det.append(acts);
    card.append(det);
  }
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
  // 사용자가 직접 전체 토글했음 → 저장/액션(reloadAll) 후 autoCollapse가 다시 접지 않게 표시(펼침 유지)
  getAllDates().forEach(d => S.userToggledDates.add(d));
  renderTable(); renderCards(); updateToggleAllButton();
}

// ═══════════════════════════════════════════════════════════
//  Inline Add (새 이슈 인라인 입력 행)
// ═══════════════════════════════════════════════════════════
function openInlineAdd(date = null) {
  // 현재 선택 선박 기본 세팅(감독은 그 선박 이슈에서 역추적)
  const selV = S.selectedVessel;
  let supId = S.activeTab === 'all'
      ? (S.user.supervisor_id || (S.supervisors[0] && S.supervisors[0].id))
      : S.activeTab;
  let vesId = null;
  if (selV != null && selV !== '__none__') {
    vesId = Number(selV) || null;
    const anyIssue = S.issues.find(i => String(i.vessel_id) === String(selV));
    if (anyIssue && anyIssue.supervisor_id != null) supId = anyIssue.supervisor_id;
  }
  S.inlineAdd = {
    date: date || todayISO(),
    supervisor_id: supId,
    vessel_id: vesId,
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
  for (const [v, l] of [['Normal','Normal'], ['Urgent','Urgent'], ['Next DD','Next DD'], ['COC & Flag','COC & Flag']]) {
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

async function openNew() {
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

  // 현재 선박별 보기에서 선택된 선박 → 모달에 자동 세팅(감독은 그 선박 이슈에서 역추적).
  const selV = S.selectedVessel;
  let preVes = null, supId = null;
  if (selV != null && selV !== '__none__') {
    preVes = selV;
    const anyIssue = S.issues.find(i => String(i.vessel_id) === String(selV));
    if (anyIssue && anyIssue.supervisor_id != null) supId = anyIssue.supervisor_id;
  }
  // 감독 폴백: 현재 탭 기준 (전체 탭이면 본인 감독 or 첫 감독)
  if (supId == null) {
    supId = S.activeTab !== 'all' ? S.activeTab
          : (S.user.supervisor_id || (S.supervisors[0] && S.supervisors[0].id));
  }
  if (supId != null) {
    $('#f-supervisor').value = supId;
    await refillVesselSelect(supId);
    // 선택 선박이 이 감독의 선박목록에 있으면 자동 선택(없으면 select 첫 항목 유지)
    if (preVes != null && $('#f-vessel').querySelector(`option[value="${preVes}"]`)) {
      $('#f-vessel').value = String(preVes);
    }
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

// ───────────── 캘린더(일정)에 등록 ─────────────
async function addIssueToCalendar(i) {
  // 중복 체크
  let existing = null;
  try {
    existing = await api(`/api/cal/events/find?source_type=issue&source_id=${i.id}`);
  } catch (_) {}

  if (existing) {
    if (confirm(
        `이 이슈는 이미 일정에 등록되어 있습니다.\n\n` +
        `제목: ${existing.title}\n날짜: ${existing.start_date}\n\n` +
        `일정 페이지에서 확인/편집하시겠습니까?`
    )) {
      window.location.href = '/calendar';
    }
    return;
  }

  // priority별 색상 매핑
  const colorMap = {
    'COC & Flag': 'red',
    'Urgent':     'amber',
    'Next DD':    'blue',
    'Normal':     'gray',
  };
  const color = colorMap[i.priority] || 'blue';

  // 미리 채워진 데이터
  const vesselName = (S.vessels.find(v => v.id === i.vessel_id) || {}).name || '';
  const supName    = (S.supervisors.find(s => s.id === i.supervisor_id) || {}).name || '';
  const title = (vesselName ? `[${vesselName}] ` : '') + (i.item_topic || '(이슈)');
  const startDate = i.due_date || i.issue_date;
  const endDate   = (i.due_date && i.issue_date && i.due_date !== i.issue_date) ? i.due_date : null;

  const summary =
    `다음 정보로 일정에 등록합니다:\n\n` +
    `  제목: ${title}\n` +
    `  날짜: ${startDate}${endDate ? ' ~ ' + endDate : ''}\n` +
    `  감독: ${supName || '(미지정)'}\n` +
    `  선박: ${vesselName || '(미지정)'}\n` +
    `  우선순위: ${i.priority} → 색상: ${color}\n\n` +
    `진행하시겠습니까? (저장 후 일정 페이지에서 시간/메모 등 추가 편집 가능)`;
  if (!confirm(summary)) return;

  try {
    await api('/api/cal/events', {
      method: 'POST',
      body: JSON.stringify({
        title,
        start_date: startDate,
        end_date:   endDate,
        all_day:    true,
        supervisor_id: i.supervisor_id || null,
        vessel_id:     i.vessel_id || null,
        category:   '업무',
        color,
        notes:      i.description || '',
        source_type: 'issue',
        source_id:   i.id,
      }),
    });
    if (confirm('일정에 등록되었습니다. 일정 페이지로 이동하시겠습니까?')) {
      window.location.href = '/calendar';
    }
  } catch (err) {
    alert('일정 등록 실패: ' + err.message);
  }
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
  refreshRosterSyncStatus();
}

// ---------- 로스터 자동화 동기화 (온디맨드 버튼 + 실시간 폴링 UX) ----------
let ROSTER_SYNC_POLLING = false;   // 폴링 재진입/중복 트리거 방지

// last_result(예: "enrich=OK · fleet-map=OK" / "enrich=FAIL(rc1) · fleet-map=OK")
// 안에 OK 아닌 단계(FAIL/TIMEOUT/EXC)가 하나라도 있으면 부분실패로 판정.
function rosterResultFailed(result) {
  return /=\s*(FAIL|TIMEOUT|EXC)/i.test(result || '');
}
function setRosterButton(disabled, label) {
  const btn = $('#btn-roster-sync');
  if (!btn) return;
  btn.disabled = disabled;
  btn.innerHTML = label;
}
function setRosterBanner(kind, msg) {   // kind: 'ok' | 'warn' | 'err' | null(숨김)
  const b = $('#roster-sync-banner');
  if (!b) return;
  b.classList.remove('rs-ok', 'rs-warn', 'rs-err');
  if (!kind) { b.style.display = 'none'; b.textContent = ''; return; }
  b.classList.add('rs-' + kind);
  b.textContent = msg;
  b.style.display = 'block';
}
function renderRosterStatusLine(s) {   // 상태표시 줄 갱신(마지막 동기화 시각·단계결과)
  const box = $('#roster-sync-status');
  if (!box) return;
  const done = s.done_at ? `마지막 동기화: ${s.done_at}` : '아직 동기화 이력 없음';
  box.textContent = done + (s.last_result ? ` · ${s.last_result}` : '');
}

async function refreshRosterSyncStatus() {
  const box = $('#roster-sync-status');
  if (!box) return;
  if (ROSTER_SYNC_POLLING) return;   // 폴링 중엔 폴링 로직이 UI 소유
  try {
    const s = await api('/api/roster-sync/status');
    if (s.pending) {
      setRosterButton(true, '<span class="rs-spinner"></span>동기화 중…');
      box.textContent = '동기화 진행 중… 맥 러너가 처리하고 있습니다 (~1분).';
    } else {
      setRosterButton(false, '선박 로스터 동기화');
      renderRosterStatusLine(s);
    }
  } catch (e) {
    box.textContent = '상태 조회 실패: ' + e.message;
  }
}

async function triggerRosterSync() {
  if (ROSTER_SYNC_POLLING) return;   // 진행 중 중복 클릭 무시
  if (!confirm('TRMT 선박 로스터를 전 자동화(지도·선급 등)에 동기화합니다.\n(맥 러너가 ~1분 내 처리)\n진행할까요?')) return;

  const clickAt = Date.now();       // 완료 판정 기준(이 시각 이후로 done 갱신되면 완료)
  setRosterBanner(null);
  setRosterButton(true, '<span class="rs-spinner"></span>동기화 중… (최대 ~2분)');

  // 1) 트리거
  try {
    const r = await fetch('/api/roster-sync/trigger', {
      method: 'POST', credentials: 'same-origin',
    });
    if (!r.ok) {
      setRosterButton(false, '선박 로스터 동기화');
      setRosterBanner('err', `요청 실패 (HTTP ${r.status})`);
      return;
    }
  } catch (e) {
    setRosterButton(false, '선박 로스터 동기화');
    setRosterBanner('err', '요청 실패: ' + e.message);
    return;
  }

  // done_at("YYYY-MM-DD HH:MM:SS", localtime) → epoch ms. 파싱 실패 시 0.
  const doneEpoch = (v) => {
    if (!v) return 0;
    const t = Date.parse(v.replace(' ', 'T'));   // 로컬 타임존으로 해석
    return isNaN(t) ? 0 : t;
  };
  // 트리거 직전 기준선(관대하게 5초 뒤로): 이 값보다 큰 done 이면 이번 실행 결과로 간주.
  const baseline = clickAt - 5000;

  // 2) 폴링: ~4초 간격, 최대 ~3분
  ROSTER_SYNC_POLLING = true;
  const INTERVAL = 4000, MAX_MS = 180000;
  const started = Date.now();

  const finishOK = (s) => {
    ROSTER_SYNC_POLLING = false;
    setRosterButton(false, '선박 로스터 동기화');
    renderRosterStatusLine(s);
    if (rosterResultFailed(s.last_result)) {
      setRosterBanner('warn', '일부 실패: ' + (s.last_result || '결과 미상') + ' — 로그 확인 필요');
    } else {
      setRosterBanner('ok', '동기화 완료' + (s.done_at ? ` · ${s.done_at}` : ''));
    }
  };

  const poll = async () => {
    if (Date.now() - started > MAX_MS) {   // 3분 초과 → 지연 안내(폭주 방지)
      ROSTER_SYNC_POLLING = false;
      setRosterButton(false, '선박 로스터 동기화');
      setRosterBanner('warn', '진행 지연 — 백그라운드에서 계속됩니다. 잠시 후 새로고침으로 확인하세요.');
      return;
    }
    let s;
    try {
      s = await api('/api/roster-sync/status');
    } catch (e) {
      // 네트워크/HTTP 오류: 조용히 재시도
      setTimeout(poll, INTERVAL);
      return;
    }
    // 완료 판정: pending 해제 AND done_at 이 이번 클릭(baseline) 이후로 갱신됨.
    const doneAfter = doneEpoch(s.done_at) >= baseline;
    if (!s.pending && doneAfter) { finishOK(s); return; }
    setTimeout(poll, INTERVAL);
  };
  setTimeout(poll, INTERVAL);   // 첫 폴은 4초 뒤(러너 픽업 여유)
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
        manager:       ($('#ves-add-manager') ? $('#ves-add-manager').value.trim() : ''),
        supervisor_ids: [...ADMIN.selectedSupIds],
      }),
    });
    $('#ves-add-name').value = '';
    $('#ves-add-short').value = '';
    $('#ves-add-imo').value = '';
    $('#ves-add-class').value = '';
    if ($('#ves-add-manager')) $('#ves-add-manager').value = '';
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
      if ($('#myves-add-manager')) $('#myves-add-manager').value = '';
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
          v.manager,
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
        manager:       ($('#myves-add-manager') ? $('#myves-add-manager').value.trim() : ''),
        supervisor_ids: [S.myVesSupId],
      }),
    });
    $('#myves-add-name').value = '';
    $('#myves-add-short').value = '';
    $('#myves-add-imo').value = '';
    $('#myves-add-class').value = '';
    if ($('#myves-add-manager')) $('#myves-add-manager').value = '';
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
  if ($('#vedit-manager')) $('#vedit-manager').value = v.manager || '';

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
      manager:       ($('#vedit-manager') ? $('#vedit-manager').value.trim() : ''),
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
  if (!document.getElementById('btn-new-issue')) return;  // Daily 페이지 아니면 no-op
  await loadSupervisors();
  if (S.activeTab === 'all') S.activeTab = onlySupId();  // 손유석 단독 — 'all' 잔상 방지
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

  // 선박별 보기(rev.4)에선 날짜 그룹 접기 버튼 불필요 → 숨김
  { const bta = $('#btn-toggle-all'); if (bta) bta.style.display = 'none'; }

  // 툴바 슬림화: 엑셀추출 / 영문엑셀추출 / 업무요약 만 상단 우측(page-actions)으로 옮기고
  // 나머지(Today·검색·필터·업무요약추출·items)는 안 쓰므로 툴바째 숨김. (필터 요소는 DOM 유지 → JS 정상)
  {
    const pa = document.querySelector('.page-actions');
    ['#btn-export-xlsx', '#btn-export-xlsx-en', '#btn-summary-gen'].forEach(s => {
      const b = $(s); if (b && pa) pa.append(b);
    });
    const tb = document.querySelector('.toolbar'); if (tb) tb.style.display = 'none';
  }

  // 엑셀 추출 — 현재 필터 상태 그대로 백엔드에 넘김
  function buildExportParams() {
    const p = new URLSearchParams();
    if (S.activeTab !== 'all')   p.set('supervisor_id', S.activeTab);
    if (S.filters.q)             p.set('q', S.filters.q);
    if (S.filters.vessel_id)     p.set('vessel_id', S.filters.vessel_id);
    if (S.filters.vessel_type)   p.set('vessel_type', S.filters.vessel_type);
    if (S.filters.priority)      p.set('priority', S.filters.priority);
    // status 처리 — 화면과 동일하게:
    //  · 명시 필터 있으면 그대로  · "완료" 서브탭은 Closed  · 그 외 진행중(Open,InProgress)
    if (S.filters.status) {
      p.set('status', S.filters.status);
    } else if (S.activeSubTab === 'closed') {
      p.set('status', 'Closed');
    } else if (S.activeSubTab === 'all') {
      // 전체 서브 탭 = status 미지정 → 진행중+완료 모두
    } else {
      p.set('status_in', 'Open,InProgress');
    }
    return p;
  }

  $('#btn-export-xlsx').addEventListener('click', () => {
    window.location = '/api/issues/export?' + buildExportParams().toString();
  });

  // 선박→담당자(영문 메일 인사말). 정규화 선명으로 매칭.
  const EN_CONTACT = {
    indonesiaprosperity: 'Giorgos', southafricaprosperity: 'Giorgos',
    kuwaitprosperity: 'Sergiy', cyprusprosperity: 'Nitin',
    atlanticmerchant: 'Gerasimos', pacificmonaco: 'Gerasimos', atlanticbridge: 'Gerasimos',
    pacificbeijing: 'Methew', atlanticexpress: 'Methew', atlanticgeneva: 'Methew',
    atlanticsouth: 'Dmitry', atlanticgreen: 'Dmitry', atlanticnorth: 'Leonid',
  };
  const enNorm = (s) => (s || '').toLowerCase().replace(/[^a-z0-9]/g, '');
  const enMailDraft = (vn) => {
    const who = EN_CONTACT[enNorm(vn)] || 'Sir/Madam';
    return `Subject: [Important!] ${vn} – Open Technical Issues / Update Request\n\n`
      + `Dear ${who},\n\n`
      + `Good day.\n\n`
      + `Please find attached the list of open technical issues for M/T ${vn} that have been raised to the Owners.\n\n`
      + `Kindly review the attached file and update the current progress status and repair plan for each item in the TSI comment column, and revert to us at your earliest convenience.\n\n`
      + `Also, if any issue has been closed, please change the status to closed for our reference.\n\n`
      + `Your prompt feedback would be highly appreciated.\n\n`
      + `Thank you for your cooperation.\n\n`
      + `Best regards,`;
  };
  const enMailDraftMulti = (person, vnames) => {
    const list = vnames.map(n => `- M/T ${n}`).join('\n');
    return `Subject: [Important!] Open Technical Issues / Update Request\n\n`
      + `Dear ${person},\n\n`
      + `Good day.\n\n`
      + `Please find attached the list of open technical issues for the following vessels under your responsibility that have been raised to the Owners:\n${list}\n\n`
      + `Kindly review the attached file and update the current progress status and repair plan for each item in the TSI comment column, and revert to us at your earliest convenience.\n\n`
      + `Also, if any issue has been closed, please change the status to closed for our reference.\n\n`
      + `Your prompt feedback would be highly appreciated.\n\n`
      + `Thank you for your cooperation.\n\n`
      + `Best regards,`;
  };

  // 영문 엑셀 추출 — 2모드: ① 선택 선박만 ② 담당자별(여러 선박 묶음). + 복붙용 영문 메일 드래프트.
  $('#btn-export-xlsx-en').addEventListener('click', () => {
    // 담당자→담당선박(현재 탭 스코프=S.vessels). EN_CONTACT 매핑된 선박만.
    const personVessels = {};
    for (const v of (S.vessels || [])) {
      const who = EN_CONTACT[enNorm(v.name)];
      if (who) (personVessels[who] = personVessels[who] || []).push({ id: v.id, name: v.name });
    }
    const persons = Object.keys(personVessels).sort();
    const g = curVesselGroup();
    const hasVessel = g && g.id !== '__none__';
    if (!hasVessel && !persons.length) { alert('좌측에서 선박을 선택하거나, 담당자 매핑된 선박이 있어야 합니다.'); return; }

    const ov = document.createElement('div');
    ov.style.cssText = 'position:fixed;inset:0;z-index:3000;background:rgba(0,0,0,.4);display:flex;align-items:center;justify-content:center';
    const box = document.createElement('div');
    box.style.cssText = 'background:#fff;border-radius:12px;padding:20px;width:560px;max-width:94%;max-height:90vh;overflow:auto;box-shadow:0 10px 40px rgba(0,0,0,.25)';
    box.innerHTML = '<div style="font-weight:700;font-size:15px;margin-bottom:10px">📄 영문 엑셀 추출 + 메일 드래프트</div>';

    // 모드 토글
    let mode = hasVessel ? 'vessel' : 'person';
    const modeRow = document.createElement('div');
    modeRow.style.cssText = 'display:flex;gap:14px;margin-bottom:10px;font-size:13px';
    modeRow.innerHTML =
      `<label style="cursor:pointer"><input type="radio" name="enmode" value="vessel" ${mode === 'vessel' ? 'checked' : ''} ${hasVessel ? '' : 'disabled'}> 선택 선박${hasVessel ? ' (' + escHtml(g.name) + ')' : ''}</label>`
      + `<label style="cursor:pointer"><input type="radio" name="enmode" value="person" ${mode === 'person' ? 'checked' : ''} ${persons.length ? '' : 'disabled'}> 담당자별</label>`;
    box.appendChild(modeRow);

    // 담당자 select (person 모드)
    const psel = document.createElement('select');
    psel.style.cssText = 'width:100%;height:36px;padding:0 10px;border:1px solid #d3d1c7;border-radius:8px;font-size:14px;margin-bottom:10px';
    for (const p of persons) psel.append(new Option(`${p}  (${personVessels[p].length}척)`, p));
    box.appendChild(psel);

    const lbl = document.createElement('div');
    lbl.style.cssText = 'font-size:12px;font-weight:600;color:#555;margin-bottom:4px;display:flex;justify-content:space-between;align-items:center';
    const copyBtn = document.createElement('button');
    copyBtn.className = 'btn btn-outline btn-sm'; copyBtn.textContent = '📋 메일 복사';
    lbl.innerHTML = '<span>✉ 메일 드래프트 (복붙용 · 영문)</span>'; lbl.appendChild(copyBtn);
    box.appendChild(lbl);
    const ta = document.createElement('textarea');
    ta.style.cssText = 'width:100%;height:280px;padding:10px;border:1px solid #d3d1c7;border-radius:8px;font-size:12.5px;line-height:1.5;font-family:inherit;resize:vertical;margin-bottom:14px';
    box.appendChild(ta);

    function dlUrl() {
      const p = buildExportParams();
      p.set('lang', 'en');
      if (mode === 'vessel') { p.set('vessel_id', g.id); }
      else { p.delete('vessel_id'); p.set('vessel_ids', personVessels[psel.value].map(v => v.id).join(',')); }
      return '/api/issues/export?' + p.toString();
    }
    function refresh() {
      psel.style.display = (mode === 'person') ? '' : 'none';
      if (mode === 'vessel') ta.value = enMailDraft(g.name);
      else ta.value = enMailDraftMulti(psel.value, personVessels[psel.value].map(v => v.name));
    }
    modeRow.querySelectorAll('input[name="enmode"]').forEach(r =>
      r.addEventListener('change', (e) => { mode = e.target.value; refresh(); }));
    psel.addEventListener('change', refresh);
    refresh();

    copyBtn.onclick = async () => {
      try { await navigator.clipboard.writeText(ta.value); } catch (_) { ta.select(); document.execCommand('copy'); }
      copyBtn.textContent = '✓ 복사됨'; setTimeout(() => copyBtn.textContent = '📋 메일 복사', 1500);
    };
    const row = document.createElement('div');
    row.style.cssText = 'display:flex;gap:8px;justify-content:flex-end';
    const cancel = document.createElement('button');
    cancel.className = 'btn btn-outline btn-sm'; cancel.textContent = '닫기';
    cancel.onclick = () => ov.remove();
    const dl = document.createElement('button');
    dl.className = 'btn btn-primary btn-sm'; dl.textContent = '⬇ 영문 엑셀 다운로드';
    dl.onclick = () => { window.location = dlUrl(); };
    row.appendChild(cancel); row.appendChild(dl);
    box.appendChild(row);
    ov.appendChild(box);
    ov.addEventListener('click', (e) => { if (e.target === ov) ov.remove(); });
    document.body.appendChild(ov);
  });

  // 업무 요약 추출 — 한글 요약(Gemini) 3열 표
  $('#btn-export-summary').addEventListener('click', () => {
    const p = new URLSearchParams();
    if (S.activeTab !== 'all') p.set('supervisor_id', S.activeTab);
    downloadExport('#btn-export-summary', '/api/issues/summary-export?' + p.toString(), 'TRMT_업무요약.xlsx');
  });

  // 업무 요약 — 현재 탭(전체/감독)의 전체 이슈를 요약하여 '요약' 탭에 저장·갱신
  $('#btn-summary-gen').addEventListener('click', async () => {
    const btn = $('#btn-summary-gen');
    const label = btn.querySelector('span');
    const prev = label ? label.textContent : '';
    if (label) label.textContent = '요약 생성 중...';
    btn.disabled = true;
    try {
      // 어느 탭에서 누르든 항상 "전체" 스코프로 생성 (감독별 분리 저장도 함께 갱신됨)
      const res = await api('/api/issues/summary-generate', { method: 'POST' });
      if (res.counts) Object.assign(S.summaryCounts, res.counts);
      else S.summaryCounts['all'] = (res.rows || []).length;
      // 전체 대분류 + 요약 서브탭으로 전환해 전체 요약을 표시
      S.activeTab = 'all';
      S.activeSubTab = 'summary';
      S.summary = res;
      try { localStorage.setItem('trmt_subtab', 'summary'); } catch (_) {}
      await loadVessels(null);
      renderTabs();
      renderVesselFilter();
      renderTabContext();
      render();
    } catch (e) {
      alert('요약 생성 실패: ' + e.message);
    } finally {
      if (label) label.textContent = prev;
      btn.disabled = false;
    }
  });

  // 공통: AI 호출로 시간이 걸리는 추출을 fetch로 받아 파일 저장
  function downloadExport(btnSel, url, fallbackName) {
    const btn = $(btnSel);
    const label = btn.querySelector('span');
    const prev = label ? label.textContent : '';
    if (label) label.textContent = '생성 중...';
    btn.disabled = true;
    fetch(url)
      .then(res => { if (!res.ok) throw new Error('HTTP ' + res.status); return res.blob().then(b => ({ b, res })); })
      .then(({ b, res }) => {
        const cd = res.headers.get('content-disposition') || '';
        let name = fallbackName;
        const m = cd.match(/filename\*?=(?:UTF-8'')?["']?([^;"']+)/i);
        if (m) name = decodeURIComponent(m[1]);
        const u = URL.createObjectURL(b);
        const a = document.createElement('a');
        a.href = u; a.download = name; document.body.appendChild(a); a.click();
        a.remove(); URL.revokeObjectURL(u);
      })
      .catch(err => alert('추출 실패: ' + err.message))
      .finally(() => { if (label) label.textContent = prev; btn.disabled = false; });
  }

  let searchTimer;
  $('#filter-search').addEventListener('input', (e) => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      S.filters.item_topic = '';   // 수동 검색 시 제목 정확일치 해제
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
}

// ───────────── 공용 와이어링 (모든 페이지: 유저메뉴/비번/관리) ─────────────
function wireCommon() {
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
    // 로스터 자동화 동기화 버튼
    const rsBtn = $('#btn-roster-sync');
    if (rsBtn) rsBtn.addEventListener('click', triggerRosterSync);
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
wireCommon();  // 유저메뉴/관리/비번 — 모든 페이지 공통
if (document.getElementById('btn-new-issue')) (async function init() {
  try {
    await loadSupervisors();
    try { S.summaryCounts = await api('/api/issues/summary-counts') || {}; } catch (_) {}
    // 손유석 단독 운영 — 항상 손유석 탭으로 고정 (소분류는 저장된 값 유지)
    S.activeTab = onlySupId();
    await loadVessels(S.activeTab === 'all' ? null : S.activeTab);
    await loadVesselOrder();
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

})();  // ← 전역 누출 방지 IIFE 끝
