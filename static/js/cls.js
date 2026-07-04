/* ═══════════════════════════════════════════════════════════════
   TRMT3 — Class Status
   선급 Class Status Report 업로드 → 선명 자동매칭 → Open COC·기국 추출
   ═══════════════════════════════════════════════════════════════ */
'use strict';

const $ = (sel) => document.querySelector(sel);

const HIDDEN_SUP = ['FLEET AGENDA'];
const isHiddenSup = (s) => HIDDEN_SUP.includes((s.name || '').toUpperCase());
// Class Status 탭은 손유석 담당선박만 운영 — 타 감독(김흥민·이창주) 탭은 숨김
const ONLY_SUP = '손유석';

function loadExpanded() {
  // 펼친 cs_id 집합을 저장(기본=접힘). 사용자가 펼친 카드만 기록 → 저장/액션 후에도 유지.
  try {
    const raw = localStorage.getItem('trmt_cls_expanded');
    return raw ? new Set(JSON.parse(raw)) : new Set();
  } catch (_) { return new Set(); }
}
function saveExpanded() {
  try { localStorage.setItem('trmt_cls_expanded', JSON.stringify([...S.expanded])); } catch (_) {}
}

const S = {
  user:        window.TRMT?.user || {},
  supervisors: [],
  data:        { vessels: [], unmatched: [] },
  vesselsAll:  [],
  activeTab:   'all',
  search:      '',
  expanded:    loadExpanded(),   // 펼친 cs_id 집합 (기본 접힘)
};

// ───────────── Helpers ─────────────
function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === 'class') e.className = v;
    else if (k === 'html') e.innerHTML = v;
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
    try { const j = await r.json(); msg = j.message || j.error || msg; } catch (_) {}
    throw new Error(msg);
  }
  return r.json();
}

const esc = (s) => String(s == null ? '' : s);

// ───────────── Tabs ─────────────
function renderTabs() {
  const bar = $('#cls-tab-bar');
  bar.innerHTML = '';
  // Class Status 탭은 손유석 담당선박만 — '전체' 탭 및 타 감독 탭 제거
  for (const s of S.supervisors) {
    if (isHiddenSup(s)) continue;
    if ((s.name || '').trim() !== ONLY_SUP) continue;
    bar.append(tabEl(s.id, s.name, s.color, String(S.activeTab) === String(s.id)));
  }
}
function tabEl(id, name, color, active) {
  const t = el('div', { class: 'tab' + (active ? ' active' : ''), 'data-id': id },
    el('span', { class: `tab-dot dot-${color || 'gray'}` }), name);
  t.addEventListener('click', () => switchTab(id));
  return t;
}
async function switchTab(id) {
  S.activeTab = id;
  renderTabs();
  await loadData();
}

// ───────────── Data ─────────────
async function loadData() {
  const qs = S.activeTab === 'all' ? '' : `?supervisor_id=${S.activeTab}`;
  try {
    S.data = await api('/api/class-status' + qs);
  } catch (e) {
    $('#cls-list').innerHTML = '';
    $('#cls-list').append(el('div', { class: 'cs-empty' }, '불러오기 실패: ' + e.message));
    return;
  }
  render();
}

// ───────────── Render ─────────────
function matchSearch(snap, vesselName, q) {
  if (!q) return true;
  if ((vesselName || '').toLowerCase().includes(q)) return true;
  const all = [...(snap.coc || []), ...(snap.statutory || [])];
  return all.some(it =>
    (it.description || '').toLowerCase().includes(q) ||
    (it.remark || '').toLowerCase().includes(q));
}

function render() {
  const list = $('#cls-list');
  const unm = $('#cls-unmatched');
  list.innerHTML = '';
  unm.innerHTML = '';
  const q = (S.search || '').trim().toLowerCase();

  const vessels = (S.data.vessels || []).filter(
    g => matchSearch(g.snapshot, g.vessel.name, q));
  // 지적(COC/기국) 있는 선박을 위로 — 건수 많은 순, 그다음 선명순
  const _cnt = g => (g.snapshot.coc || []).length + (g.snapshot.statutory || []).length;
  vessels.sort((a, b) => (_cnt(b) - _cnt(a)) || (a.vessel.name || '').localeCompare(b.vessel.name || ''));

  let totCoc = 0, totStat = 0;
  vessels.forEach(g => { totCoc += (g.snapshot.coc || []).length; totStat += (g.snapshot.statutory || []).length; });
  $('#cls-context').textContent =
    `선박 ${vessels.length}척 · Open 선급지적 ${totCoc} · 기국 ${totStat}`;

  if (!vessels.length) {
    list.append(el('div', { class: 'cs-empty' },
      q ? '검색 결과가 없습니다.'
        : 'Class Status가 없습니다. 우측 상단 "API에서 Pushing"으로 선급 API에서 가져오세요.'));
  } else {
    vessels.forEach(g => list.append(vesselCard(g)));
  }

  // 미매칭 (전체 탭에서만)
  const um = (S.data.unmatched || []).filter(s => matchSearch(s, s.vessel_name_raw, q));
  if (um.length) {
    unm.append(el('h2', { class: 'cls-unm-title' },
      `미매칭 — 선명 자동매칭 실패 (${um.length}건)`));
    um.forEach(s => unm.append(unmatchedCard(s)));
  }
}

function badge(text, cls) {
  return text ? el('span', { class: 'cls-badge ' + (cls || '') }, text) : '';
}

function vesselCard(g) {
  const v = g.vessel, snap = g.snapshot;
  const collapsed = !S.expanded.has(snap.id);   // 기본 접힘 — 펼친 카드만 expanded 집합에
  const coc = snap.coc || [], stat = snap.statutory || [];
  const hasFindings = (coc.length + stat.length) > 0;

  const head = el('div', { class: 'cls-card-head' },
    el('button', {
      class: 'cls-toggle' + (collapsed ? '' : ' open'),
      onclick: () => { collapsed ? S.expanded.add(snap.id) : S.expanded.delete(snap.id); saveExpanded(); render(); },
    }, collapsed ? '▸' : '▾'),
    el('div', { class: 'cls-vessel' },
      el('span', { class: 'cls-vessel-name' }, v.name),
      badge(snap.class_society, 'society'),
      v.vessel_type ? el('span', { class: 'cls-vessel-type' }, v.vessel_type) : ''),
    el('div', { class: 'cls-meta' },
      el('span', {}, '발행 ' + (snap.report_date || '-')),
      snap.has_file
        ? el('a', { class: 'cls-src cls-src-dl', href: `/api/class-status/${snap.id}/file`,
                    target: '_blank', rel: 'noopener',
                    title: '원본 미리보기: ' + (snap.source_filename || '') }, '👁 ' + (snap.source_filename || '원본 보기'))
        : (snap.source_filename ? el('span', { class: 'cls-src', title: snap.source_filename }, snap.source_filename) : ''),
      el('span', { class: 'cls-counts' }, `선급지적 ${coc.length} · 기국 ${stat.length}`)),
    el('div', { class: 'cls-actions' },
      el('button', { class: 'btn btn-outline btn-sm', onclick: () => location.href = `/api/class-status/${snap.id}/export` }, '엑셀'),
      el('button', { class: 'btn btn-outline btn-sm cls-del', onclick: () => deleteSnap(snap.id, v.name) }, '삭제')));

  const body = el('div', { class: 'cls-card-body', hidden: collapsed });
  body.append(clsDigestPanel(snap));
  if (!coc.length && !stat.length) {
    body.append(el('div', { class: 'cls-noitems' }, 'Open 선급지적 / 기국 사항 없음'));
  } else {
    body.append(catSection('선급지적 (Condition of Class)', 'coc', coc));
    body.append(catSection('기국 (Statutory)', 'stat', stat));
  }
  return el('div', { class: 'cls-card' + (hasFindings ? ' has-findings' : '') }, head, body);
}

// ───────────── 선박 단위 CLASS 요약 (자동 집계, 읽기전용) ─────────────
function clsDigest(snap) {
  const coc = snap.coc || [], stat = snap.statutory || [];
  const total = coc.length + stat.length;
  const norm = s => (s || '').trim().toLowerCase().replace(/\s+/g, ' ');
  const text = it => (it.remark || it.description || '').trim();
  const fmt = (it, dup) => {
    let s = text(it);
    if (dup) s += ' (선급지적 / 기국사항 중복)';
    const due = (it.due_date || '').trim();
    if (due) s += ' // DUE DATE : ' + due;
    const act = (it.action_taken || '').trim();
    if (act) s += '\n조치사항 : ' + act;
    return s;
  };

  const statMatched = new Set();
  const lines = [];
  // 선급지적 먼저: 동일한 기국 항목(원문 기준)이 있으면 한 번만 + 중복 표기
  coc.forEach(c => {
    const key = norm(c.description);
    const mi = key ? stat.findIndex((s, i) => !statMatched.has(i) && norm(s.description) === key) : -1;
    if (mi >= 0) {
      statMatched.add(mi);
      lines.push(fmt(c, true));
    } else {
      lines.push(fmt(c, false));
    }
  });
  // 중복되지 않은 기국 항목
  stat.forEach((s, i) => { if (!statMatched.has(i)) lines.push(fmt(s, false)); });

  const detail = lines.filter(l => l).map((l, i) => `${i + 1}. ${l}`).join('\n');
  return { society: snap.class_society || '-', total, detail };
}

function clsDigestPanel(snap) {
  const d = clsDigest(snap);
  const detailCell = el('td', { class: 'cls-dg-detail' });
  if (d.detail) {
    d.detail.split('\n').forEach(line =>
      detailCell.append(el('div', { class: 'cls-dg-line' }, line.trim() ? line : '\u00A0')));
  } else {
    detailCell.append(el('span', { class: 'placeholder' }, '–'));
  }
  const table = el('table', { class: 'cls-digest-table' });
  table.append(el('thead', {}, el('tr', {},
    el('th', {}, '선급'),
    el('th', {}, 'COC'),
    el('th', {}, 'COC / 수리 상세'))));
  table.append(el('tbody', {}, el('tr', {},
    el('td', { class: 'cls-dg-society' }, d.society),
    el('td', { class: 'cls-dg-coc' }, String(d.total)),
    detailCell)));
  return el('div', { class: 'cls-digest' }, table);
}

function catSection(title, cls, items) {
  if (!items.length) return el('div');
  const tbl = el('table', { class: 'cls-table' });
  tbl.append(el('thead', {}, el('tr', {},
    el('th', { class: 'c-no' }, 'No'),
    el('th', { class: 'c-date' }, 'Issued'),
    el('th', { class: 'c-desc' }, 'Description (원문)'),
    el('th', { class: 'c-date' }, 'Due'),
    el('th', { class: 'c-rmk' }, '한글 요약'),
    el('th', { class: 'c-act' }, '조치사항'),
    el('th', { class: 'c-imp' }, 'Urgent'))));
  const tb = el('tbody');
  items.forEach(it => tb.append(itemRow(it)));
  tbl.append(tb);
  return el('section', { class: 'cls-cat' },
    el('h4', { class: 'cls-cat-title ' + cls }, `${title} · ${items.length}`),
    el('div', { class: 'tbl-scroll' }, tbl));
}

function editCell(value, field, id, extraClass) {
  const td = el('td', {
    class: 'cls-edit ' + (extraClass || ''),
    contenteditable: 'true',
    'data-id': id, 'data-field': field, spellcheck: 'false',
  }, esc(value));
  return td;
}

function itemRow(it) {
  const chk = el('input', {
    type: 'checkbox',
    class: 'cls-urgent-chk',
    'data-id': it.id,
    title: 'Urgent 표시',
  });
  chk.checked = (it.importance === 'Urgent');
  chk.addEventListener('change', (e) => {
    const val = e.target.checked ? 'Urgent' : '';
    it.importance = val;
    e.target.closest('tr').classList.toggle('cls-urgent-row', !!val);
    saveItem(it.id, { importance: val });
  });

  return el('tr', { class: it.importance === 'Urgent' ? 'cls-urgent-row' : '' },
    el('td', { class: 'c-no' }, it.no),
    editCell(it.issued_date, 'issued_date', it.id, 'c-date'),
    editCell(it.description, 'description', it.id, 'c-desc'),
    editCell(it.due_date, 'due_date', it.id, 'c-date'),
    editCell(it.remark, 'remark', it.id, 'c-rmk'),
    editCell(it.action_taken, 'action_taken', it.id, 'c-act'),
    el('td', { class: 'c-imp cls-urgent-cell' }, chk));
}

// 인라인 편집 저장 (contenteditable blur)
document.addEventListener('blur', (e) => {
  const td = e.target.closest && e.target.closest('.cls-edit');
  if (!td) return;
  const id = td.dataset.id, field = td.dataset.field;
  const val = td.textContent.trim();
  if (td._orig === undefined) return;
  if (val === td._orig) return;
  td._orig = val;
  saveItem(id, { [field]: val });
}, true);
document.addEventListener('focus', (e) => {
  const td = e.target.closest && e.target.closest('.cls-edit');
  if (td) td._orig = td.textContent.trim();
}, true);

async function saveItem(id, patch) {
  try { await api(`/api/class-status/items/${id}`, { method: 'PUT', body: JSON.stringify(patch) }); }
  catch (e) { alert('저장 실패: ' + e.message); }
}

async function deleteSnap(csId, name) {
  if (!confirm(`${name} 의 Class Status 스냅샷을 삭제할까요?`)) return;
  try { await api(`/api/class-status/${csId}`, { method: 'DELETE' }); await loadData(); }
  catch (e) { alert('삭제 실패: ' + e.message); }
}

// ───────────── 미매칭 카드 ─────────────
function unmatchedCard(snap) {
  const coc = snap.coc || [], stat = snap.statutory || [];
  const sel = el('select', { class: 'cls-assign-sel' },
    el('option', { value: '' }, '— 선박 선택 —'));
  S.vesselsAll.forEach(v => sel.append(el('option', { value: v.id }, v.name)));

  const head = el('div', { class: 'cls-card-head cls-unm-head' },
    el('div', { class: 'cls-vessel' },
      el('span', { class: 'cls-vessel-name' }, snap.vessel_name_raw || '(선명 없음)'),
      badge(snap.class_society, 'society'),
      badge('미매칭', 'warn')),
    el('div', { class: 'cls-meta' },
      el('span', {}, '발행 ' + (snap.report_date || '-')),
      el('span', { class: 'cls-counts' }, `선급지적 ${coc.length} · 기국 ${stat.length}`)),
    el('div', { class: 'cls-actions' },
      sel,
      el('button', { class: 'btn btn-primary btn-sm', onclick: () => assignSnap(snap.id, sel.value) }, '배정'),
      el('button', { class: 'btn btn-outline btn-sm cls-del', onclick: () => deleteSnap(snap.id, snap.vessel_name_raw || '미매칭') }, '삭제')));

  const body = el('div', { class: 'cls-card-body' });
  if (!coc.length && !stat.length) body.append(el('div', { class: 'cls-noitems' }, 'Open 항목 없음'));
  else { body.append(catSection('선급지적 (Condition of Class)', 'coc', coc)); body.append(catSection('기국 (Statutory)', 'stat', stat)); }
  return el('div', { class: 'cls-card cls-card-unm' }, head, body);
}

async function assignSnap(csId, vesselId) {
  if (!vesselId) { alert('배정할 선박을 선택하세요.'); return; }
  try { await api(`/api/class-status/${csId}/assign`, { method: 'POST', body: JSON.stringify({ vessel_id: Number(vesselId) }) }); await loadData(); }
  catch (e) { alert('배정 실패: ' + e.message); }
}

// ───────────── 전체 접기/펼치기 ─────────────
function collapseAll() {
  S.expanded.clear();
  saveExpanded(); render();
}
function expandAll() {
  (S.data.vessels || []).forEach(g => S.expanded.add(g.snapshot.id));
  saveExpanded(); render();
}

// (제거됨) 사용되지 않는 업로드 모달 코드 — openUpload/uploadFiles 등은 어디서도 호출되지
// 않았고 참조하던 #cls-upload-* 요소와 /api/class-status/upload 엔드포인트도 존재하지 않음.

// ───────────── Init ─────────────
function wireUpload() {
  // API Pushing(온디맨드) — 선급검사 API에서 손유석 담당선박 Class Status를 받아 업데이트.
  // 기존 push 엔드포인트(플래그)를 맥 러너가 폴링 → 선급 API pull 실행.
  const pushBtn = $('#cls-push-btn');
  if (pushBtn) pushBtn.addEventListener('click', async () => {
    if (!confirm('선급검사 API에서 담당선박 Class Status를 받아 업데이트합니다.\n(맥 러너가 1~2분 내 처리, 텔레그램으로 결과 보고)\n진행할까요?')) return;
    pushBtn.disabled = true;
    try {
      const r = await fetch('/api/class-status/push', { method: 'POST' });
      if (r.ok) alert('요청됨 📥 — 선급 API에서 가져오는 중입니다. 1~2분 후 목록을 새로고침하세요. (결과는 텔레그램 보고)');
      else alert('요청 실패 (' + r.status + ')');
    } catch (e) { alert('요청 실패: ' + e.message); }
    finally { pushBtn.disabled = false; }
  });
  const mgrBtn = $('#cls-export-mgr-btn');
  if (mgrBtn) mgrBtn.addEventListener('click', openMgrExport);
}

// 관리사별 추출: 버튼 → 관리사 목록 → 선택 → 엑셀 다운로드
async function openMgrExport() {
  // 담당감독 탭이면 그 감독 선박만(supervisor_id), '전체'면 전부
  const supQs = (S.activeTab && S.activeTab !== 'all') ? `?supervisor_id=${S.activeTab}` : '';
  const supName = (S.activeTab !== 'all'
    ? (S.supervisors.find(s => String(s.id) === String(S.activeTab)) || {}).name : '') || '';
  let managers;
  try {
    const r = await fetch('/api/class-status/managers' + supQs);
    managers = (await r.json()).managers || [];
  } catch (e) { alert('관리사 목록 로드 실패: ' + e.message); return; }
  if (!managers.length) { alert((supName ? `${supName} 담당 선박 중 ` : '') + '지적이 있는 선박의 관리사 정보가 없습니다.\n선박 설정에서 관리사를 먼저 지정하세요.'); return; }

  // 복붙용 메일 드래프트(영문, firm-but-professional). 관리사명 넣어 생성.
  // 관리사별 담당자 인사말(있으면 'Dear 이름,', 없으면 'Dear [관리사] Team,')
  const GREET = { 'CSM CY (CNTR)': 'Alex', 'CSM GR': 'Evangelos' };
  const mailDraft = (m) => {
    const co = (m && m !== '(Unassigned)') ? m : 'Management Company';
    const greeting = GREET[m] ? `Dear ${GREET[m]},` : `Dear ${co} Team,`;
    return `Subject: Class Status – Open COC & Statutory Items: Action Plan & Progress Required\n\n`
      + `${greeting}\n\n`
      + `Please find attached the current Class Status overview for the vessels under your management, listing the Open Condition of Class and Statutory items.\n\n`
      + `For each item listed, we kindly request that you complete the "Management Action Plan & Progress" column with the following:\n`
      + `1) The corrective action to be taken;\n`
      + `2) The target date for completion (by when); and\n`
      + `3) The current status / progress to date.\n\n`
      + `Kindly return the completed file to us at your earliest convenience. Should any clarification or supporting documents be required, please do not hesitate to contact us.\n\n`
      + `Best regards,`;
  };

  const ov = document.createElement('div');
  ov.style.cssText = 'position:fixed;inset:0;z-index:3000;background:rgba(0,0,0,.4);display:flex;align-items:center;justify-content:center';
  const box = document.createElement('div');
  box.style.cssText = 'background:#fff;border-radius:12px;padding:20px;width:560px;max-width:94%;max-height:90vh;overflow:auto;box-shadow:0 10px 40px rgba(0,0,0,.25)';
  box.innerHTML = '<div style="font-weight:700;font-size:15px;margin-bottom:4px">📑 관리사별 Class Status 추출 + 메일 드래프트'
    + (supName ? ` <span style="font-weight:500;font-size:12px;color:#1d4ed8">· ${supName} 담당</span>` : '') + '</div>'
    + '<div style="font-size:12px;color:#888;margin-bottom:12px">관리사 선택 → 엑셀(영문) 다운로드 + 오른쪽 메일 드래프트 복사해서 발송. 지적 없는 선박 자동 제외.</div>';
  const sel = document.createElement('select');
  sel.style.cssText = 'width:100%;height:38px;padding:0 10px;border:1px solid #d3d1c7;border-radius:8px;font-size:14px;margin-bottom:12px';
  for (const m of managers) {
    const o = document.createElement('option');
    o.value = m.manager; o.textContent = `${m.manager}  (${m.vessels}척)`;
    sel.appendChild(o);
  }
  box.appendChild(sel);

  // 메일 드래프트 영역
  const lbl = document.createElement('div');
  lbl.style.cssText = 'font-size:12px;font-weight:600;color:#555;margin-bottom:4px;display:flex;justify-content:space-between;align-items:center';
  const copyBtn = document.createElement('button');
  copyBtn.className = 'btn btn-outline btn-sm'; copyBtn.textContent = '📋 메일 복사';
  lbl.innerHTML = '<span>✉ 메일 드래프트 (복붙용 · 영문)</span>';
  lbl.appendChild(copyBtn);
  box.appendChild(lbl);
  const ta = document.createElement('textarea');
  ta.style.cssText = 'width:100%;height:230px;padding:10px;border:1px solid #d3d1c7;border-radius:8px;font-size:12.5px;line-height:1.5;font-family:inherit;resize:vertical;margin-bottom:14px';
  ta.value = mailDraft(sel.value);
  box.appendChild(ta);
  sel.addEventListener('change', () => { ta.value = mailDraft(sel.value); });
  copyBtn.onclick = async () => {
    try { await navigator.clipboard.writeText(ta.value); copyBtn.textContent = '✓ 복사됨'; setTimeout(() => copyBtn.textContent = '📋 메일 복사', 1500); }
    catch (_) { ta.select(); document.execCommand('copy'); copyBtn.textContent = '✓ 복사됨'; setTimeout(() => copyBtn.textContent = '📋 메일 복사', 1500); }
  };

  const row = document.createElement('div');
  row.style.cssText = 'display:flex;gap:8px;justify-content:flex-end';
  const cancel = document.createElement('button');
  cancel.className = 'btn btn-outline btn-sm'; cancel.textContent = '닫기';
  cancel.onclick = () => ov.remove();
  const dl = document.createElement('button');
  dl.className = 'btn btn-primary btn-sm'; dl.textContent = '⬇ 엑셀 다운로드';
  dl.onclick = () => {
    let url = '/api/class-status/export-by-manager?manager=' + encodeURIComponent(sel.value);
    if (S.activeTab && S.activeTab !== 'all') url += '&supervisor_id=' + S.activeTab;
    location.href = url;
  };
  row.appendChild(cancel); row.appendChild(dl);
  box.appendChild(row);
  ov.appendChild(box);
  ov.addEventListener('click', (e) => { if (e.target === ov) ov.remove(); });
  document.body.appendChild(ov);
}

function wireSearch() {
  const inp = $('#cls-search'), clr = $('#cls-search-clear');
  let t;
  inp.addEventListener('input', () => {
    clr.hidden = !inp.value;
    clearTimeout(t);
    t = setTimeout(() => { S.search = inp.value; render(); }, 200);
  });
  clr.addEventListener('click', () => { inp.value = ''; clr.hidden = true; S.search = ''; render(); inp.focus(); });
}

async function init() {
  wireUpload();
  wireSearch();
  $('#cls-collapse-all').addEventListener('click', collapseAll);
  $('#cls-expand-all').addEventListener('click', expandAll);
  try {
    S.supervisors = await api('/api/supervisors');
  } catch (_) { S.supervisors = []; }
  // Class Status 탭은 손유석 담당선박만 운영 → 항상 손유석 탭으로 고정
  const onlySup = S.supervisors.find(s => (s.name || '').trim() === ONLY_SUP);
  if (onlySup) S.activeTab = onlySup.id;
  try {
    S.vesselsAll = await api('/api/vessels');
  } catch (_) { S.vesselsAll = []; }
  renderTabs();
  await loadData();
}

document.addEventListener('DOMContentLoaded', init);
