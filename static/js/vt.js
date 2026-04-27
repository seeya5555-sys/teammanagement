/* ═══════════════════════════════════════════════════════════════
   TRMT3 — Vetting Status
   적용 선박: VLCC, AFRAMAX, LR, MR (CNTR 제외)
   비정기 검사 — 선박당 0~N건
   ═══════════════════════════════════════════════════════════════ */
'use strict';

const $ = (sel) => document.querySelector(sel);

// ───────────── State ─────────────
const HIDDEN_SUPERVISOR_NAMES_VT = ['FLEET AGENDA'];
function isHiddenSupervisor(sup) {
  return HIDDEN_SUPERVISOR_NAMES_VT.includes((sup.name || '').toUpperCase());
}

function loadVtExpanded() {
  try {
    const raw = localStorage.getItem('trmt_vt_expanded');
    return raw ? new Set(JSON.parse(raw)) : new Set();
  } catch (_) { return new Set(); }
}
function saveVtExpanded() {
  try {
    localStorage.setItem('trmt_vt_expanded', JSON.stringify([...S.expandedVettings]));
  } catch (_) {}
}

const S = {
  user:        window.TRMT?.user || {},
  supervisors: [],
  data:        [],
  activeTab:   'all',
  year:        new Date().getFullYear(),
  search:      '',
  expandedVessels:  new Set(),
  expandedVettings: loadVtExpanded(),
  hiddenVesselIds:  new Set(),
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

function formatFileSize(b) {
  if (!b) return '';
  if (b < 1024) return b + ' B';
  if (b < 1048576) return (b / 1024).toFixed(1) + ' KB';
  return (b / 1048576).toFixed(1) + ' MB';
}

function parseTSV(text) {
  const rows = [];
  let row = [], cell = '', inQuotes = false, cellStart = true;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (inQuotes) {
      if (ch === '"') {
        if (text[i + 1] === '"') { cell += '"'; i++; }
        else inQuotes = false;
      } else cell += ch;
      continue;
    }
    if (ch === '"' && cellStart) { inQuotes = true; cellStart = false; continue; }
    if (ch === '\t') { row.push(cell); cell = ''; cellStart = true; continue; }
    if (ch === '\n' || ch === '\r') {
      if (ch === '\r' && text[i + 1] === '\n') i++;
      row.push(cell); rows.push(row); row = []; cell = ''; cellStart = true;
      continue;
    }
    cell += ch; cellStart = false;
  }
  if (cell !== '' || row.length) { row.push(cell); rows.push(row); }
  return rows.filter(r => r.some(c => c !== ''));
}

// ───────────── Tabs ─────────────
function renderTabs() {
  const bar = $('#vt-tab-bar');
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
  S.expandedVessels.clear();
  renderTabs();
  await reloadData();
}

// ───────────── Render ─────────────
function render() {
  const list = $('#vt-vessel-list');
  list.innerHTML = '';

  if (!S.data.length) {
    list.append(el('div', { class: 'cs-empty' },
      'Vetting 대상 선박이 없습니다. (VLCC / AFRAMAX / LR / MR 만)'));
    return;
  }

  const q = (S.search || '').trim().toLowerCase();
  const filtered = q ? S.data.filter(item => {
    const v = item.vessel;
    if (v.name && v.name.toLowerCase().includes(q)) return true;
    if (v.short_name && v.short_name.toLowerCase().includes(q)) return true;
    return (item.vettings || []).some(vt =>
      (vt.report_number && vt.report_number.toLowerCase().includes(q)) ||
      (vt.inspection_company && vt.inspection_company.toLowerCase().includes(q))
    );
  }) : S.data;

  if (q && !filtered.length) {
    list.append(el('div', { class: 'cs-empty' },
      `"${S.search}" 와(과) 일치하는 결과가 없습니다.`));
    return;
  }

  const TYPE_ORDER = ['VLCC', 'AFRAMAX', 'LR', 'MR'];
  const byType = {};
  for (const item of filtered) {
    const t = item.vessel.vessel_type || '기타';
    if (!byType[t]) byType[t] = [];
    byType[t].push(item);
  }
  const types = Object.keys(byType).sort(
    (a, b) => (TYPE_ORDER.indexOf(a) < 0 ? 99 : TYPE_ORDER.indexOf(a))
            - (TYPE_ORDER.indexOf(b) < 0 ? 99 : TYPE_ORDER.indexOf(b))
  );

  for (const t of types) {
    const grp = byType[t];
    const block = el('div', { class: 'cs-type-group' });
    block.append(el('div', { class: `cs-type-header cs-type-${t.toLowerCase()}` },
      el('span', { class: 'cs-type-badge' }, t),
      el('span', { class: 'cs-type-count' }, `${grp.length}척`),
    ));
    for (const item of grp) block.append(vesselBlock(item));
    list.append(block);
  }
}

function renderContext() {
  const c = $('#vt-context');
  const q = (S.search || '').trim().toLowerCase();
  const totalCount = S.data.length;
  let filteredCount = totalCount;
  if (q) {
    filteredCount = S.data.filter(item => {
      const v = item.vessel;
      if (v.name && v.name.toLowerCase().includes(q)) return true;
      if (v.short_name && v.short_name.toLowerCase().includes(q)) return true;
      return (item.vettings || []).some(vt =>
        (vt.report_number && vt.report_number.toLowerCase().includes(q)) ||
        (vt.inspection_company && vt.inspection_company.toLowerCase().includes(q))
      );
    }).length;
  }
  const tabName = S.activeTab === 'all'
    ? '전체'
    : (S.supervisors.find(x => x.id == S.activeTab)?.name + ' 담당' || '');
  if (q) {
    c.textContent = `${S.year}년 · ${tabName} ${filteredCount}/${totalCount}척  (검색: "${S.search}")`;
  } else {
    c.textContent = `${S.year}년 · ${tabName} ${totalCount}척`;
  }
}

// ───────────── Vessel Block ─────────────
function vesselBlock(item) {
  const v = item.vessel;
  const block = el('div', { class: 'cs-vessel-block' });
  const isExpanded = S.expandedVessels.has(v.id);

  const head = el('div', {
    class: 'cs-vessel-head' + (isExpanded ? ' expanded' : ''),
    onclick: () => toggleVessel(v.id),
    title: '클릭으로 Vetting 표 펼치기/접기',
  },
    el('span', { class: 'cs-vessel-caret' }, isExpanded ? '▼' : '▶'),
    el('span', { class: 'cs-vessel-icon' }, '🚢'),
    el('strong', { class: 'cs-vessel-name' }, v.name),
    el('span', { class: 'cs-vessel-type' }, v.vessel_type || ''),
    lastUpdateLabelVT(item.last_updated),
    vesselSummary(item),
  );
  block.append(head);

  if (!isExpanded) return block;

  const table = el('table', { class: 'vt-vetting-table' });
  table.append(el('thead', {},
    el('tr', {},
      el('th', { class: 'vt-th-caret' }, ''),
      el('th', { class: 'vt-th-rep'  }, 'Report #'),
      el('th', { class: 'vt-th-date' }, '검사일'),
      el('th', { class: 'vt-th-comp' }, 'Company'),
      el('th', { class: 'vt-th-insp' }, 'Inspector'),
      el('th', { class: 'vt-th-port' }, 'Port'),
      el('th', { class: 'vt-th-op'   }, 'Operation'),
      el('th', { class: 'vt-th-cnt'  }, 'Obs'),
      el('th', { class: 'vt-th-cnt'  }, 'Open'),
      el('th', { class: 'vt-th-cnt'  }, 'Close'),
      el('th', { class: 'vt-th-actions' }, ''),
    ),
  ));

  const tbody = el('tbody');
  if (!item.vettings.length) {
    tbody.append(el('tr', {},
      el('td', { colspan: 11, class: 'vt-empty-row' },
        '아직 Vetting 기록이 없습니다.')
    ));
  } else {
    for (const vt of item.vettings) {
      tbody.append(vettingRow(item, vt));
      if (S.expandedVettings.has(vt.id)) {
        tbody.append(detailRow(vt));
      }
    }
  }
  table.append(tbody);
  block.append(table);

  const addBar = el('div', { class: 'vt-add-bar' });
  addBar.append(el('button', {
    class: 'btn btn-outline btn-sm',
    onclick: () => createVetting(v.id),
  }, '+ 새 Vetting 추가'));
  block.append(addBar);

  return block;
}

function lastUpdateLabelVT(updatedAt) {
  if (!updatedAt) return el('span', { class: 'cs-last-update empty' }, '');
  const dateOnly = (updatedAt || '').slice(0, 10);
  return el('span', {
    class: 'cs-last-update',
    title: `Status 마지막 변경: ${updatedAt}`,
  }, '↻ ' + dateOnly);
}

function vesselSummary(item) {
  const wrap = el('span', { class: 'cs-vessel-summary' });
  const vts = item.vettings || [];
  if (!vts.length) {
    wrap.append(el('span', { class: 'cs-vessel-summary-empty' }, '검사 없음'));
    return wrap;
  }
  const latest = vts[0];
  const dateLabel = latest.inspection_date || '날짜 미정';
  const compLabel = latest.inspection_company || '미정';
  wrap.append(el('span', { class: 'vt-summary-last' },
    `Last: ${dateLabel} · ${compLabel}`));
  const totalOpen = vts.reduce((sum, v) => sum + (v.open_count || 0), 0);
  if (totalOpen > 0) {
    wrap.append(el('span', { class: 'vt-summary-open' }, `Open ${totalOpen}건`));
  } else {
    wrap.append(el('span', { class: 'vt-summary-allclosed' }, '모두 완료 ✓'));
  }
  return wrap;
}

function toggleVessel(vid) {
  if (S.expandedVessels.has(vid)) S.expandedVessels.delete(vid);
  else S.expandedVessels.add(vid);
  render();
}

function toggleVetting(vtid) {
  if (S.expandedVettings.has(vtid)) S.expandedVettings.delete(vtid);
  else S.expandedVettings.add(vtid);
  saveVtExpanded();
  render();
}

// ───────────── Vetting Row ─────────────
function vettingRow(item, vt) {
  const tr = el('tr', { class: 'vt-vetting-row' });

  // 1) caret 셀 — 클릭 시 펼침 토글 (Report# 편집과 분리)
  const caretTd = el('td', {
    class: 'vt-caret-cell',
    onclick: (e) => { e.stopPropagation(); toggleVetting(vt.id); },
    title: S.expandedVettings.has(vt.id) ? '접기' : '펼치기',
  });
  caretTd.append(el('span', { class: 'vt-caret-icon' },
    S.expandedVettings.has(vt.id) ? '▼' : '▶'));
  tr.append(caretTd);

  // 2) Report# 셀 — 클릭 시 편집 모드만
  const rep = el('td', { class: 'vt-edit-cell' });
  const repText = el('span', { class: 'vt-cell-display' },
    vt.report_number || '–');
  if (!vt.report_number) repText.classList.add('placeholder');
  rep.append(repText);
  attachInlineEdit(rep, vt, 'report_number', repText);
  tr.append(rep);

  tr.append(vtEditCell(vt, 'inspection_date', 'date'));
  tr.append(vtEditCell(vt, 'inspection_company'));
  tr.append(vtEditCell(vt, 'inspector'));
  tr.append(vtEditCell(vt, 'port'));
  tr.append(vtEditCellSelect(vt, 'operation', ['', 'Loading', 'Discharging', 'Idle']));

  tr.append(countCell(vt, 'manual_observation_count', vt.observation_count, vt.observation_manual));
  tr.append(countCell(vt, 'manual_open_count',        vt.open_count,        vt.open_manual,  'cs-cnt-open'));
  tr.append(countCell(vt, 'manual_close_count',       vt.close_count,       vt.close_manual, 'cs-cnt-close'));

  const actions = el('td', { class: 'cs-actions' });

  const attBtn = el('button', {
    class: 'icon-btn',
    title: `첨부파일${vt.attach_count ? ` (${vt.attach_count})` : ''}`,
    onclick: (e) => { e.stopPropagation(); openVtAttachModal(vt); },
  });
  attBtn.innerHTML = `<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2">
    <path d="M21.44 11.05l-9.19 9.19a6 6 0 01-8.49-8.49l9.19-9.19a4 4 0 015.66 5.66l-9.2 9.19a2 2 0 01-2.83-2.83l8.49-8.48"/></svg>`;
  if (vt.attach_count > 0) {
    attBtn.classList.add('has-attach');
    attBtn.append(el('span', { class: 'attach-badge' }, String(vt.attach_count)));
  }
  actions.append(attBtn);

  // 📅 캘린더 등록
  const calBtn = el('button', {
    class: 'icon-btn',
    title: '일정에 등록',
    onclick: (e) => { e.stopPropagation(); addVtToCalendar(item, vt); },
  });
  calBtn.innerHTML = `<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2">
    <rect x="3" y="4" width="18" height="18" rx="2" ry="2"/>
    <line x1="16" y1="2" x2="16" y2="6"/>
    <line x1="8" y1="2" x2="8" y2="6"/>
    <line x1="3" y1="10" x2="21" y2="10"/></svg>`;
  actions.append(calBtn);

  const rm = el('button', {
    class: 'icon-btn danger',
    title: '이 Vetting 삭제',
    onclick: (e) => { e.stopPropagation(); deleteVetting(vt); },
  });
  rm.innerHTML = `<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2">
    <path d="M3 6h18"/><path d="M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2"/>
    <path d="M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6"/></svg>`;
  actions.append(rm);

  tr.append(actions);
  return tr;
}

function vtEditCell(vt, field, kind = 'text') {
  const td = el('td', { class: 'vt-edit-cell' });
  const display = el('span', { class: 'vt-cell-display' }, vt[field] || '–');
  if (!vt[field]) display.classList.add('placeholder');
  td.append(display);
  attachInlineEdit(td, vt, field, display, kind);
  return td;
}

function vtEditCellSelect(vt, field, options) {
  const td = el('td', { class: 'vt-edit-cell' });
  const display = el('span', { class: 'vt-cell-display' }, vt[field] || '–');
  if (!vt[field]) display.classList.add('placeholder');
  td.append(display);

  td.addEventListener('click', () => {
    if (td._editing) return;
    td._editing = true;
    td.innerHTML = '';
    const sel = document.createElement('select');
    sel.className = 'cs-inline-input';
    for (const opt of options) {
      const o = document.createElement('option');
      o.value = opt; o.textContent = opt || '(미정)';
      if (opt === (vt[field] || '')) o.selected = true;
      sel.append(o);
    }
    td.append(sel); sel.focus();

    let done = false;
    const save = async () => {
      if (done) return; done = true;
      td._editing = false;
      const newVal = sel.value;
      if (newVal === (vt[field] || '')) {
        td.innerHTML = ''; td.append(display);
        return;
      }
      try {
        await api(`/api/vettings/${vt.id}`, {
          method: 'PUT',
          body: JSON.stringify({ [field]: newVal }),
        });
        await reloadData();
      } catch (err) {
        alert('저장 실패: ' + err.message);
        td.innerHTML = ''; td.append(display);
      }
    };
    sel.addEventListener('blur', save);
    sel.addEventListener('change', save);
    sel.addEventListener('keydown', (ev) => {
      if (ev.key === 'Escape') {
        done = true; td._editing = false;
        td.innerHTML = ''; td.append(display);
      }
    });
  });
  return td;
}

function attachInlineEdit(td, vt, field, displayEl, kind = 'text') {
  td.addEventListener('click', (e) => {
    if (td._editing) return;
    td._editing = true;
    const input = document.createElement('input');
    input.type = (kind === 'date') ? 'date' : 'text';
    input.value = vt[field] || '';
    input.className = 'cs-inline-input';
    displayEl.replaceWith(input);
    input.focus();
    if (input.select) input.select();

    let done = false;
    const save = async () => {
      if (done) return; done = true;
      td._editing = false;
      const newVal = input.value;
      if (newVal === (vt[field] || '')) {
        input.replaceWith(displayEl);
        return;
      }
      try {
        await api(`/api/vettings/${vt.id}`, {
          method: 'PUT',
          body: JSON.stringify({ [field]: newVal }),
        });
        await reloadData();
      } catch (err) {
        alert('저장 실패: ' + err.message);
        input.replaceWith(displayEl);
      }
    };
    input.addEventListener('blur', save);
    input.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter') { ev.preventDefault(); save(); }
      if (ev.key === 'Escape') {
        done = true; td._editing = false;
        input.replaceWith(displayEl);
      }
    });
  });
}

function countCell(vt, field, value, isManual, extraClass = '') {
  const td = el('td', {
    class: 'cs-cnt cs-edit-cell ' + extraClass + (isManual ? ' is-manual' : ''),
    style: 'text-align:center',
    title: isManual ? '수동 입력값 (클릭으로 수정, 빈칸 저장 시 자동 복귀)' : '자동 카운트 (클릭으로 직접 입력)',
  });
  const display = el('div', { class: 'cs-cell-display' }, String(value));
  td.append(display);

  td.addEventListener('click', () => {
    if (td._editing) return;
    td._editing = true;
    td.innerHTML = '';
    const input = document.createElement('input');
    input.type = 'number';
    input.min = '0';
    input.value = isManual ? String(value) : '';
    input.placeholder = String(value);
    input.className = 'cs-inline-input';
    input.style.textAlign = 'center';
    td.append(input); input.focus(); input.select();

    let done = false;
    const save = async () => {
      if (done) return; done = true;
      td._editing = false;
      const raw = input.value.trim();
      const newVal = raw === '' ? null : Number(raw);
      if (newVal !== null && (isNaN(newVal) || newVal < 0)) {
        td.innerHTML = ''; td.append(display); return;
      }
      try {
        await api(`/api/vettings/${vt.id}`, {
          method: 'PUT',
          body: JSON.stringify({ [field]: newVal === null ? '' : newVal }),
        });
        await reloadData();
      } catch (err) {
        alert('저장 실패: ' + err.message);
        td.innerHTML = ''; td.append(display);
      }
    };
    input.addEventListener('blur', save);
    input.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter') save();
      if (ev.key === 'Escape') {
        done = true; td._editing = false;
        td.innerHTML = ''; td.append(display);
      }
    });
  });
  return td;
}

// ───────────── Detail Row ─────────────
function detailRow(vt) {
  const tr = el('tr', { class: 'cs-detail-row' });
  const td = el('td', { colspan: 11, class: 'cs-detail-cell' });

  // Overall Remark
  const remarkSec = el('div', { class: 'cs-finding-section' });
  remarkSec.append(el('div', { class: 'cs-finding-header cs-cat-overall' },
    el('span', { class: 'cs-cat-dot' }),
    el('strong', {}, 'Overall Remark'),
    el('button', {
      class: 'btn btn-outline btn-sm',
      style: 'margin-left:auto',
      onclick: () => editOverallRemark(vt),
    }, '✏ 편집'),
  ));
  remarkSec.append(el('div', { class: 'cs-overall-body' },
    vt.overall_remark || el('span', { class: 'placeholder' }, '(작성된 메모 없음)')));
  td.append(remarkSec);

  const observations = vt.findings || [];
  td.append(findingsSection(vt, observations));

  tr.append(td);
  return tr;
}

function findingsSection(vt, findings) {
  const sec = el('div', { class: 'cs-finding-section' });
  sec.append(el('div', { class: 'cs-finding-header cs-cat-observation' },
    el('span', { class: 'cs-cat-dot' }),
    el('strong', {}, 'Observations'),
    el('span', { class: 'cs-finding-count' }, `(${findings.length}건)`),
  ));

  const table = el('table', { class: 'cs-findings-table' });
  table.append(el('thead', {}, el('tr', {},
    el('th', { style: 'width:50px' }, 'No'),
    el('th', { class: 'cs-th-item' }, 'Item'),
    el('th', { class: 'cs-th-desc' }, 'Description'),
    el('th', { class: 'cs-th-remark' }, 'Remark'),
    el('th', { style: 'width:90px; text-align:center' }, 'Status'),
    el('th', { style: 'width:80px' }, ''),
  )));

  const tbody = el('tbody');
  if (!findings.length && !(vt._inlineAdd)) {
    tbody.append(el('tr', {},
      el('td', { colspan: 6, class: 'vt-empty-row' },
        '아직 Observation이 없습니다. 아래 + 버튼으로 추가하세요.')
    ));
  } else {
    for (const f of findings) tbody.append(findingRow(vt, f));
  }
  if (vt._inlineAdd) {
    vt._inlineAdd.rows.forEach((r, idx) => {
      tbody.append(inlineAddRow(vt, r, idx, findings.length));
    });
  }
  table.append(tbody);
  sec.append(table);

  const btnRow = el('div', { class: 'cs-add-btn-row' });
  btnRow.append(el('button', {
    class: 'btn btn-outline btn-sm',
    onclick: () => addBlankRow(vt),
  }, vt._inlineAdd
      ? '+ 빈 줄 추가  (엑셀 표 붙여넣기 가능)'
      : '+ Observation 추가  (엑셀 표 붙여넣기 가능)'));
  if (vt._inlineAdd) {
    btnRow.append(el('button', {
      class: 'btn btn-primary btn-sm',
      onclick: () => saveAllInlineRows(vt),
    }, '💾 전체 저장'));
    btnRow.append(el('button', {
      class: 'btn btn-outline btn-sm',
      onclick: () => { vt._inlineAdd = null; render(); },
    }, '취소'));
  }
  sec.append(btnRow);
  return sec;
}

function findingRow(vt, f) {
  const tr = el('tr');
  tr.append(el('td', { class: 'cs-no' }, String(f.no)));
  tr.append(findingEditableCell(f, 'item', vt));
  tr.append(findingEditableCell(f, 'description', vt));
  tr.append(findingEditableCell(f, 'remark', vt));

  const stTd = el('td', { class: 'cs-status', style: 'text-align:center' });
  const badge = el('span', {
    class: 'bd ' + (f.status === 'Closed' ? 'status-done' : 'status-open'),
    onclick: () => toggleFindingStatus(f),
    style: 'cursor:pointer',
    title: '클릭으로 Open/Closed 토글',
  }, f.status);
  stTd.append(badge);
  tr.append(stTd);

  const acts = el('td', { class: 'cs-actions' });
  if (f.status === 'Open') {
    const issueBtn = el('button', {
      class: 'icon-btn',
      title: 'Daily 업무관리 이슈로 등록',
      onclick: () => createIssueFromFinding(vt, f),
    });
    issueBtn.innerHTML = `<svg viewBox="0 0 24 24" width="11" height="11" fill="none" stroke="currentColor" stroke-width="2">
      <path d="M12 2v20M5 12h14"/></svg>`;
    acts.append(issueBtn);
  }
  const rm = el('button', {
    class: 'icon-btn danger',
    title: '삭제',
    onclick: () => deleteFinding(f),
  });
  rm.innerHTML = `<svg viewBox="0 0 24 24" width="11" height="11" fill="none" stroke="currentColor" stroke-width="2">
    <path d="M3 6h18"/><path d="M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2"/>
    <path d="M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6"/></svg>`;
  acts.append(rm);
  tr.append(acts);

  return tr;
}

function findingEditableCell(f, field, vt) {
  const td = el('td', { class: 'cs-edit-cell' });
  const display = el('div', { class: 'cs-cell-display' }, f[field] || '–');
  if (!f[field]) display.classList.add('placeholder');
  td.append(display);

  td.addEventListener('click', () => {
    if (td._editing) return;
    td._editing = true;
    td.innerHTML = '';
    const input = document.createElement('input');
    input.type = 'text';
    input.value = f[field] || '';
    input.className = 'cs-inline-input';
    td.append(input); input.focus(); input.select();

    let done = false;
    const save = async () => {
      if (done) return; done = true;
      td._editing = false;
      const newVal = input.value;
      if (newVal === (f[field] || '')) {
        td.innerHTML = ''; td.append(display); return;
      }
      try {
        await api(`/api/vt-findings/${f.id}`, {
          method: 'PUT',
          body: JSON.stringify({ [field]: newVal }),
        });
        f[field] = newVal;
        await reloadData();
      } catch (err) {
        alert('저장 실패: ' + err.message);
        td.innerHTML = ''; td.append(display);
      }
    };
    input.addEventListener('blur', save);
    input.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter' && !ev.shiftKey) { ev.preventDefault(); save(); }
      if (ev.key === 'Escape') {
        done = true; td._editing = false;
        td.innerHTML = ''; td.append(display);
      }
    });

    input.addEventListener('paste', async (ev) => {
      if (!vt) return;
      const text = (ev.clipboardData || window.clipboardData).getData('text');
      if (!text) return;
      const isTabular = text.includes('\t') || /\r?\n/.test(text.trim());
      if (!isTabular) return;

      ev.preventDefault();
      const rows = parseTSV(text);
      if (rows.length <= 1) {
        input.value = rows.length ? rows[0][0] : text;
        return;
      }
      done = true; td._editing = false;
      const values = rows.map(r => r[0] !== undefined ? r[0].trim() : '');
      td.innerHTML = ''; td.append(display);
      await bulkUpdateFindings(vt, f, field, values);
    });
  });
  return td;
}

async function bulkUpdateFindings(vt, startF, field, values) {
  const list = (vt.findings || []).slice().sort((a, b) => a.no - b.no);
  const startIdx = list.findIndex(x => x.id === startF.id);
  if (startIdx < 0) { alert('대상 항목을 찾지 못했습니다.'); return; }
  const remaining = list.length - startIdx;
  const willUpdate = Math.min(values.length, remaining);
  const skipped = values.length - willUpdate;

  const fieldLabel = { item: 'Item', description: 'Description', remark: 'Remark' }[field] || field;
  const startNo = startF.no;
  const endNo = list[startIdx + willUpdate - 1].no;
  let msg = `Observation ${startNo}번 ~ ${endNo}번 항목의 ${fieldLabel}을(를) 일괄 수정합니다.\n총 ${willUpdate}개 항목.`;
  if (skipped > 0) msg += `\n\n주의: ${skipped}개 행은 대상 항목 부족으로 무시됩니다.`;
  msg += '\n\n진행하시겠습니까?';
  if (!confirm(msg)) return;

  try {
    const tasks = [];
    for (let i = 0; i < willUpdate; i++) {
      const target = list[startIdx + i];
      tasks.push(api(`/api/vt-findings/${target.id}`, {
        method: 'PUT',
        body: JSON.stringify({ [field]: values[i] }),
      }));
    }
    await Promise.all(tasks);
    await reloadData();
  } catch (err) {
    alert('일괄 수정 중 오류: ' + err.message);
    await reloadData();
  }
}

async function toggleFindingStatus(f) {
  const newSt = f.status === 'Closed' ? 'Open' : 'Closed';
  try {
    await api(`/api/vt-findings/${f.id}`, {
      method: 'PUT',
      body: JSON.stringify({ status: newSt }),
    });
    await reloadData();
  } catch (err) { alert('상태 변경 실패: ' + err.message); }
}

async function deleteFinding(f) {
  if (!confirm(`Observation #${f.no} 을(를) 삭제하시겠습니까?`)) return;
  try {
    await api(`/api/vt-findings/${f.id}`, { method: 'DELETE' });
    await reloadData();
  } catch (err) { alert('삭제 실패: ' + err.message); }
}

// ───────────── 인라인 추가 ─────────────
function addBlankRow(vt) {
  for (const item of S.data) {
    for (const o of (item.vettings || [])) {
      if (o !== vt && o._inlineAdd) o._inlineAdd = null;
    }
  }
  if (!vt._inlineAdd) vt._inlineAdd = { rows: [] };
  vt._inlineAdd.rows.push({ item: '', description: '', remark: '', status: 'Open' });
  render();
  setTimeout(() => {
    const inputs = document.querySelectorAll('.cs-inline-add-row .cs-inline-input');
    const targetIdx = (vt._inlineAdd.rows.length - 1) * 4;
    if (inputs[targetIdx]) inputs[targetIdx].focus();
  }, 50);
}

function inlineAddRow(vt, row, idx, baseNo) {
  const tr = el('tr', { class: 'cs-inline-add-row' });
  tr.append(el('td', { class: 'cs-no' }, String(baseNo + idx + 1)));

  const itemInput = el('input', {
    type: 'text', class: 'cs-inline-input',
    placeholder: 'Item', value: row.item,
  });
  const descInput = el('input', {
    type: 'text', class: 'cs-inline-input',
    placeholder: idx === 0 ? 'Description (엑셀 표 복사 후 Ctrl+V)' : 'Description',
    value: row.description,
  });
  const remarkInput = el('input', {
    type: 'text', class: 'cs-inline-input', placeholder: 'Remark', value: row.remark,
  });
  const statusSel = document.createElement('select');
  statusSel.className = 'cs-inline-input';
  for (const v of ['Open', 'Closed']) {
    const o = document.createElement('option'); o.value = v; o.textContent = v;
    if (v === row.status) o.selected = true;
    statusSel.append(o);
  }

  itemInput  .addEventListener('input',  () => { row.item        = itemInput.value; });
  descInput  .addEventListener('input',  () => { row.description = descInput.value; });
  remarkInput.addEventListener('input',  () => { row.remark      = remarkInput.value; });
  statusSel  .addEventListener('change', () => { row.status      = statusSel.value; });

  const onPaste = (ev) => {
    const text = (ev.clipboardData || window.clipboardData).getData('text');
    if (!text) return;
    const isTabular = text.includes('\t') || /\r?\n/.test(text.trim());
    if (!isTabular) return;
    ev.preventDefault();
    const rows = parseTSV(text);
    rows.forEach((cols, k) => {
      const targetIdx = idx + k;
      while (vt._inlineAdd.rows.length <= targetIdx) {
        vt._inlineAdd.rows.push({ item: '', description: '', remark: '', status: 'Open' });
      }
      const target = vt._inlineAdd.rows[targetIdx];
      if (cols[0] !== undefined && cols[0] !== '') target.item        = cols[0].trim();
      if (cols[1] !== undefined && cols[1] !== '') target.description = cols[1].trim();
      if (cols[2] !== undefined && cols[2] !== '') target.remark      = cols[2].trim();
      if (cols[3] !== undefined && cols[3] !== '') {
        const st = cols[3].trim();
        target.status = (st === 'Closed' || st.toLowerCase() === 'closed') ? 'Closed' : 'Open';
      }
    });
    render();
  };
  itemInput.addEventListener('paste', onPaste);
  descInput.addEventListener('paste', onPaste);
  remarkInput.addEventListener('paste', onPaste);

  for (const inp of [itemInput, descInput, remarkInput]) {
    inp.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter' && !ev.shiftKey) {
        ev.preventDefault();
        if (idx === vt._inlineAdd.rows.length - 1) addBlankRow(vt);
        else {
          const inputs = document.querySelectorAll('.cs-inline-add-row .cs-inline-input');
          const nextIdx = (idx + 1) * 4;
          if (inputs[nextIdx]) inputs[nextIdx].focus();
        }
      }
      if (ev.key === 'Escape') { vt._inlineAdd = null; render(); }
    });
  }

  const td0 = el('td'); td0.append(itemInput);
  const td1 = el('td'); td1.append(descInput);
  const td2 = el('td'); td2.append(remarkInput);
  const td3 = el('td', { style: 'text-align:center' }); td3.append(statusSel);
  tr.append(td0, td1, td2, td3);

  const acts = el('td', { class: 'cs-actions' });
  const rm = el('button', {
    class: 'icon-btn danger', title: '이 빈 줄 삭제',
    onclick: () => {
      vt._inlineAdd.rows.splice(idx, 1);
      if (!vt._inlineAdd.rows.length) vt._inlineAdd = null;
      render();
    },
  });
  rm.innerHTML = `<svg viewBox="0 0 24 24" width="11" height="11" fill="none" stroke="currentColor" stroke-width="2">
    <path d="M3 6h18"/><path d="M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2"/>
    <path d="M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6"/></svg>`;
  acts.append(rm);
  tr.append(acts);
  return tr;
}

async function saveAllInlineRows(vt) {
  if (!vt._inlineAdd) return;
  const valid = vt._inlineAdd.rows
    .filter(r => (r.item || '').trim() !== '' || (r.description || '').trim() !== '')
    .map(r => ({
      item:        (r.item || '').trim(),
      description: (r.description || '').trim(),
      remark:      (r.remark || '').trim(),
      status:      r.status || 'Open',
    }));
  if (!valid.length) {
    alert('저장할 항목이 없습니다. Item 또는 Description을 입력하세요.');
    return;
  }
  try {
    await api(`/api/vettings/${vt.id}/findings`, {
      method: 'POST',
      body: JSON.stringify({ items: valid }),
    });
    vt._inlineAdd = null;
    await reloadData();
  } catch (err) { alert('저장 실패: ' + err.message); }
}

// ───────────── Vetting CRUD ─────────────
async function createVetting(vesselId) {
  try {
    const r = await api('/api/vettings', {
      method: 'POST',
      body: JSON.stringify({ vessel_id: vesselId }),
    });
    // 해당 선박 자동 펼침 (이미 펼쳐있으면 그대로) + 새 Vetting 펼침
    S.expandedVessels.add(vesselId);
    S.expandedVettings.add(r.id);
    saveVtExpanded();
    await reloadData();
    // 새로 추가된 Vetting 행으로 스크롤
    setTimeout(() => {
      const rows = document.querySelectorAll('.vt-vetting-row');
      const last = rows[rows.length - 1];
      if (last) {
        last.scrollIntoView({ behavior: 'smooth', block: 'center' });
        last.classList.add('vt-row-flash');
        setTimeout(() => last.classList.remove('vt-row-flash'), 1500);
      }
    }, 100);
  } catch (err) { alert('생성 실패: ' + err.message); }
}

async function deleteVetting(vt) {
  const label = vt.report_number || vt.inspection_date || `(ID ${vt.id})`;
  if (!confirm(`Vetting [${label}]을(를) 삭제하시겠습니까?\n관련 Observations 및 첨부파일도 함께 삭제됩니다.`)) return;
  try {
    await api(`/api/vettings/${vt.id}`, { method: 'DELETE' });
    S.expandedVettings.delete(vt.id);
    saveVtExpanded();
    await reloadData();
  } catch (err) { alert('삭제 실패: ' + err.message); }
}

// ───────────── 캘린더(일정)에 등록 ─────────────
async function addVtToCalendar(item, vt) {
  if (!vt.inspection_date) {
    alert('Inspection Date가 설정되지 않았습니다.\n검사일을 먼저 입력해주세요.');
    return;
  }

  // 중복 체크
  let existing = null;
  try {
    existing = await api(`/api/cal/events/find?source_type=vetting&source_id=${vt.id}`);
  } catch (_) {}

  if (existing) {
    if (confirm(
        `이 Vetting은 이미 일정에 등록되어 있습니다.\n\n` +
        `제목: ${existing.title}\n날짜: ${existing.start_date}\n\n` +
        `일정 페이지에서 확인/편집하시겠습니까?`
    )) {
      window.location.href = '/calendar';
    }
    return;
  }

  const v = item.vessel;
  let supId = null;
  if (S.activeTab !== 'all') supId = parseInt(S.activeTab);
  if (!supId && S.user.supervisor_id) supId = S.user.supervisor_id;
  if (!supId && (v.supervisor_ids || []).length) supId = v.supervisor_ids[0];

  const company = vt.inspection_company || '검사기관 미정';
  const repPart = vt.report_number ? ` (${vt.report_number})` : '';
  const title = `[${v.name}] Vetting · ${company}${repPart}`;

  const summary =
    `다음 정보로 일정에 등록합니다:\n\n` +
    `  제목: ${title}\n` +
    `  날짜: ${vt.inspection_date}\n` +
    `  선박: ${v.name}\n` +
    `  Operation: ${vt.operation || '-'}\n` +
    `  카테고리: 검사  (색상: 호박)\n\n` +
    `진행하시겠습니까? (저장 후 일정 페이지에서 추가 편집 가능)`;
  if (!confirm(summary)) return;

  const notes =
    `Inspector: ${vt.inspector || '-'}\n` +
    `Port: ${vt.port || '-'}\n` +
    `Operation: ${vt.operation || '-'}\n` +
    (vt.overall_remark ? `\n${vt.overall_remark}` : '');

  try {
    await api('/api/cal/events', {
      method: 'POST',
      body: JSON.stringify({
        title,
        start_date: vt.inspection_date,
        all_day:    true,
        supervisor_id: supId,
        vessel_id:     v.id,
        category:   '검사',
        color:      'amber',
        location:   vt.port || '',
        notes,
        source_type: 'vetting',
        source_id:   vt.id,
      }),
    });
    if (confirm('일정에 등록되었습니다. 일정 페이지로 이동하시겠습니까?')) {
      window.location.href = '/calendar';
    }
  } catch (err) {
    alert('일정 등록 실패: ' + err.message);
  }
}

// ───────────── Overall Remark 모달 ─────────────
let _vtRemarkVetting = null;

function openRemarkModal(vt) {
  _vtRemarkVetting = vt;
  const subtitle = `· ${vt.inspection_date || '날짜미정'} ${vt.inspection_company || ''} ${vt.report_number || ''}`.trim();
  $('#vt-remark-subtitle').textContent = subtitle;
  const ta = $('#vt-remark-textarea');
  ta.value = vt.overall_remark || '';
  $('#vt-remark-modal').hidden = false;
  document.body.style.overflow = 'hidden';
  // 즉시 textarea에 포커스 + 끝으로 커서 이동
  setTimeout(() => {
    ta.focus();
    ta.setSelectionRange(ta.value.length, ta.value.length);
    autoGrowRemark(ta);
  }, 50);
}

function closeRemarkModal() {
  $('#vt-remark-modal').hidden = true;
  document.body.style.overflow = '';
  _vtRemarkVetting = null;
}

// textarea 자동 확장 (입력에 따라 높이 늘어남)
function autoGrowRemark(ta) {
  ta.style.height = 'auto';
  const min = 130;          // 최소 높이 (px)
  const max = window.innerHeight * 0.6;  // 화면의 60%까지
  const target = Math.min(Math.max(ta.scrollHeight, min), max);
  ta.style.height = target + 'px';
}

async function saveRemarkModal() {
  if (!_vtRemarkVetting) return;
  const newVal = $('#vt-remark-textarea').value;
  const cur = _vtRemarkVetting.overall_remark || '';
  if (newVal === cur) {
    closeRemarkModal();
    return;
  }
  try {
    await api(`/api/vettings/${_vtRemarkVetting.id}`, {
      method: 'PUT',
      body: JSON.stringify({ overall_remark: newVal }),
    });
    closeRemarkModal();
    await reloadData();
  } catch (err) {
    alert('저장 실패: ' + err.message);
  }
}

// 기존 editOverallRemark 자리 — 모달 호출로 변경
async function editOverallRemark(vt) {
  openRemarkModal(vt);
}

// ───────────── Daily 이슈 등록 ─────────────
async function createIssueFromFinding(vt, f) {
  const item = S.data.find(x => x.vettings.some(v => v.id === vt.id));
  if (!item) return;
  const v = item.vessel;

  // supervisor_id 결정: 활성 탭 / 본인 / 첫 담당
  let supId = null;
  if (S.activeTab !== 'all') supId = parseInt(S.activeTab);
  if (!supId && S.user.supervisor_id) supId = S.user.supervisor_id;
  if (!supId && (v.supervisor_ids || []).length) supId = v.supervisor_ids[0];
  if (!supId) {
    alert('이슈를 등록할 담당 감독을 결정할 수 없습니다.');
    return;
  }

  const topic = f.item || (f.description ? f.description.slice(0, 60) : 'Vetting Observation');
  const repNum = vt.report_number ? ` (${vt.report_number})` : '';
  const desc =
    `[Vetting Observation #${f.no}]${repNum}\n` +
    `검사: ${vt.inspection_date || '-'} · ${vt.inspection_company || '-'} · ${vt.port || '-'}\n\n` +
    `${f.description || ''}` +
    (f.remark ? `\n\n비고: ${f.remark}` : '');

  // 오늘 날짜 (issue_date 필수)
  const today = new Date().toISOString().slice(0, 10);

  if (!confirm(
      `다음 내용으로 Daily 업무관리 이슈를 생성합니다.\n\n` +
      `선박: ${v.name}\n` +
      `Topic: ${topic}\n` +
      `Issue Date: ${today}\n` +
      `Priority: Normal\n\n진행하시겠습니까?`
  )) return;

  try {
    await api('/api/issues', {
      method: 'POST',
      body: JSON.stringify({
        supervisor_id: supId,
        vessel_id:     v.id,
        issue_date:    today,
        item_topic:    topic,
        description:   desc,
        priority:      'Normal',
        status:        'Open',
      }),
    });
    alert('Daily 업무관리에 이슈가 등록되었습니다.');
  } catch (err) { alert('등록 실패: ' + err.message); }
}

// ───────────── Data Reload ─────────────
async function reloadData() {
  const url = `/api/vettings?year=${S.year}` +
              (S.activeTab !== 'all' ? `&supervisor_id=${S.activeTab}` : '');
  let data = await api(url);
  if (S.activeTab === 'all' && S.hiddenVesselIds.size) {
    data = data.filter(item => !S.hiddenVesselIds.has(item.vessel.id));
  }
  S.data = data;
  renderContext();
  render();
}

async function loadSupervisors() {
  S.supervisors = await api('/api/supervisors');
  S.hiddenVesselIds = new Set();
  const hiddenSups = S.supervisors.filter(isHiddenSupervisor);
  for (const sup of hiddenSups) {
    try {
      const vessels = await api(`/api/vessels?supervisor_id=${sup.id}`);
      for (const v of vessels) S.hiddenVesselIds.add(v.id);
    } catch (_) {}
  }
}

// ───────────── 첨부 모달 ─────────────
let _vtAttachVetting = null;

async function openVtAttachModal(vt) {
  _vtAttachVetting = vt;
  const subtitle = `· ${vt.inspection_date || '날짜미정'} ${vt.inspection_company || ''} ${vt.report_number || ''}`.trim();
  $('#vt-attach-subtitle').textContent = subtitle;
  await renderVtAttachGrid();
  $('#vt-attach-modal').hidden = false;
  document.body.style.overflow = 'hidden';
}

async function closeVtAttachModal() {
  $('#vt-attach-modal').hidden = true;
  document.body.style.overflow = '';
  _vtAttachVetting = null;
  await reloadData();
}

async function renderVtAttachGrid() {
  const grid = $('#vt-attach-grid');
  grid.innerHTML = '';
  if (!_vtAttachVetting) return;
  let items = [];
  try {
    items = await api(`/api/vettings/${_vtAttachVetting.id}/attachments`);
  } catch (_) {}
  if (!items.length) {
    grid.append(el('div', { class: 'attach-empty' },
      '첨부 파일이 없습니다. 위 영역으로 파일을 드래그하거나 클릭해 업로드하세요.'));
    return;
  }
  for (const a of items) grid.append(vtAttachItemEl(a));
}

function vtAttachItemEl(a) {
  const item = el('div', { class: 'attach-item' });

  // 썸네일
  const thumb = el('div', { class: 'attach-thumb' });
  const isImg = /\.(jpe?g|png|gif|webp|bmp|heic|heif)$/i.test(a.filename);
  const isPdf = /\.pdf$/i.test(a.filename);
  if (isImg) {
    thumb.append(el('img', {
      src: `/api/vt-attachments/${a.id}?inline=1`,
      alt: a.filename, loading: 'lazy',
    }));
  } else {
    thumb.append(el('div', { class: 'attach-file-icon' },
      isPdf ? 'PDF' : (a.filename.split('.').pop() || 'FILE').toUpperCase().slice(0, 4)));
  }
  item.append(thumb);

  // 메타 (파일명 + 크기) — flex:1로 가운데 늘어나서 삭제 버튼 오른쪽 끝으로
  const meta = el('div', { class: 'attach-meta' },
    el('a', {
      href: `/api/vt-attachments/${a.id}` + (isImg || isPdf ? '?inline=1' : ''),
      target: (isImg || isPdf) ? '_blank' : '_self',
      class: 'attach-name',
    }, a.filename),
    el('span', { class: 'attach-size' }, formatFileSize(a.file_size)),
  );
  item.append(meta);

  // 삭제 버튼 — 오른쪽 끝
  const rm = el('button', {
    class: 'icon-btn danger attach-rm-right',
    title: '삭제',
    onclick: () => deleteVtAttach(a.id),
  });
  rm.innerHTML = `<svg viewBox="0 0 24 24" width="11" height="11" fill="none" stroke="currentColor" stroke-width="2">
    <path d="M3 6h18"/><path d="M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2"/>
    <path d="M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6"/></svg>`;
  item.append(rm);

  return item;
}

async function uploadVtFiles(files) {
  if (!_vtAttachVetting || !files || !files.length) return;
  const vid = _vtAttachVetting.id;
  for (const f of files) {
    if (f.size > 20 * 1024 * 1024) {
      alert(`"${f.name}" 은 20MB를 초과합니다.`);
      continue;
    }
    const fd = new FormData();
    fd.append('file', f);
    try {
      await api(`/api/vettings/${vid}/attachments`, { method: 'POST', body: fd });
    } catch (err) {
      alert(`"${f.name}" 업로드 실패: ${err.message}`);
    }
  }
  await renderVtAttachGrid();
}

async function deleteVtAttach(aid) {
  if (!confirm('이 첨부파일을 삭제하시겠습니까?')) return;
  try {
    await api(`/api/vt-attachments/${aid}`, { method: 'DELETE' });
    await renderVtAttachGrid();
  } catch (err) { alert('삭제 실패: ' + err.message); }
}

// ───────────── Init ─────────────
async function init() {
  try {
    await loadSupervisors();
    if (S.user.supervisor_id) {
      const sup = S.supervisors.find(s => s.id === S.user.supervisor_id);
      if (sup && !isHiddenSupervisor(sup)) S.activeTab = S.user.supervisor_id;
    }
    const activeSup = S.supervisors.find(s => s.id == S.activeTab);
    if (activeSup && isHiddenSupervisor(activeSup)) S.activeTab = 'all';

    renderTabs();
    $('#vt-year-label').textContent = S.year;
    await reloadData();

    $('#vt-year-prev').addEventListener('click', async () => {
      S.year--; $('#vt-year-label').textContent = S.year;
      await reloadData();
    });
    $('#vt-year-next').addEventListener('click', async () => {
      S.year++; $('#vt-year-label').textContent = S.year;
      await reloadData();
    });

    const searchInput = $('#vt-search');
    const clearBtn = $('#vt-search-clear');
    searchInput.addEventListener('input', (e) => {
      S.search = e.target.value;
      clearBtn.hidden = !S.search;
      renderContext(); render();
    });
    searchInput.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && S.search) {
        S.search = ''; searchInput.value = ''; clearBtn.hidden = true;
        renderContext(); render();
      }
    });
    clearBtn.addEventListener('click', () => {
      S.search = ''; searchInput.value = ''; clearBtn.hidden = true;
      searchInput.focus(); renderContext(); render();
    });

    $('#vt-attach-modal').addEventListener('click', (ev) => {
      if (ev.target.dataset.closeVta === '1') closeVtAttachModal();
    });
    const dz = $('#vt-attach-dropzone');
    const fi = $('#vt-attach-file-input');
    dz.addEventListener('click', () => fi.click());
    fi.addEventListener('change', () => { uploadVtFiles(fi.files); fi.value = ''; });
    dz.addEventListener('dragover',  (e) => { e.preventDefault(); dz.classList.add('dragover'); });
    dz.addEventListener('dragleave', () => dz.classList.remove('dragover'));
    dz.addEventListener('drop', (e) => {
      e.preventDefault(); dz.classList.remove('dragover');
      uploadVtFiles(e.dataTransfer.files);
    });

    // Overall Remark 모달
    $('#vt-remark-modal').addEventListener('click', (ev) => {
      if (ev.target.dataset.closeVtr === '1') closeRemarkModal();
    });
    $('#vt-remark-save').addEventListener('click', saveRemarkModal);
    const remarkTa = $('#vt-remark-textarea');
    remarkTa.addEventListener('input', () => autoGrowRemark(remarkTa));
    remarkTa.addEventListener('keydown', (ev) => {
      // Ctrl+Enter 또는 Cmd+Enter 로 저장
      if (ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)) {
        ev.preventDefault();
        saveRemarkModal();
      }
      // ESC 로 닫기 (textarea 안에서도 작동)
      if (ev.key === 'Escape') {
        ev.preventDefault();
        closeRemarkModal();
      }
    });

    document.addEventListener('keydown', (ev) => {
      if (ev.key === 'Escape') {
        if (!$('#vt-remark-modal').hidden) closeRemarkModal();
        else if (!$('#vt-attach-modal').hidden) closeVtAttachModal();
      }
    });

  } catch (err) {
    alert('초기 로드 실패: ' + err.message);
  }
}

document.addEventListener('DOMContentLoaded', init);
