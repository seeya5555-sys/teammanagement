// ════════════════════════════════════════════════════════════════
//  출장 경비 — 목록 + 출장 카드 CRUD
// ════════════════════════════════════════════════════════════════
const EXP = {
  trips: [],
  filters: { q: '', status: '' },
  editingId: null,
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
  const r = await fetch(url, {
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
    ...opts,
  });
  if (!r.ok) {
    let msg = `${r.status} ${r.statusText}`;
    try { const j = await r.json(); if (j.error) msg = j.error; } catch {}
    throw new Error(msg);
  }
  return r.status === 204 ? null : r.json();
}

function fmtDate(s) { return s ? s.replace(/-/g, '.') : ''; }

function fmtMoney(cur, amt) {
  const n = Number(amt || 0);
  return `${cur} ${n.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
}

function fmtTotals(totals) {
  const keys = Object.keys(totals || {});
  if (!keys.length) return '합계 —';
  return keys.map(k => fmtMoney(k, totals[k])).join('  ·  ');
}

// ─── Load & Render ───────────────────────────────────────────
async function init() {
  bindEvents();
  await reload();
}

async function reload() {
  const p = new URLSearchParams();
  if (EXP.filters.q)      p.set('q', EXP.filters.q);
  if (EXP.filters.status) p.set('status', EXP.filters.status);
  try {
    EXP.trips = await api('/api/biz-trips?' + p);
  } catch (e) {
    alert('출장 목록 로드 실패: ' + e.message);
    EXP.trips = [];
  }
  render();
}

function render() {
  const grid = $('#exp-grid');
  $$('.dd-card', grid).forEach(n => n.remove());
  const empty = $('#exp-empty');
  if (EXP.trips.length === 0) {
    empty.hidden = false;
    $('#exp-count-label').textContent = '';
    return;
  }
  empty.hidden = true;
  $('#exp-count-label').textContent = `${EXP.trips.length}건`;
  for (const t of EXP.trips) grid.append(renderCard(t));
}

function renderCard(t) {
  const canEdit = !!t.can_edit;
  const card = el('div', {
    class: 'dd-card' + (canEdit ? '' : ' dd-card-readonly'),
    'data-id': t.id,
    onclick: () => { window.location = `/expenses/${t.id}`; },
  });

  const done = t.status === 'settled';
  const statusBadge = el('span', {
    class: `dd-badge ${done ? 'dd-badge-done' : 'dd-badge-draft'}`
  }, done ? '정산완료' : '진행 중');
  if (canEdit) {
    statusBadge.classList.add('dd-badge-clickable');
    statusBadge.title = '클릭하여 진행 중 / 정산완료 전환';
    statusBadge.addEventListener('click', (ev) => { ev.stopPropagation(); toggleStatus(t, statusBadge); });
  }

  const headRight = el('div', { class: 'dd-card-head-right' }, statusBadge);
  if (canEdit) {
    headRight.append(el('button', {
      class: 'dd-card-edit', type: 'button', title: '출장 정보 편집',
      onclick: (ev) => { ev.stopPropagation(); openEdit(t.id); },
    }, '⋮'));
  }

  card.append(
    el('div', { class: 'dd-card-head' },
      el('h3', { class: 'dd-card-title' }, t.title),
      headRight,
    )
  );

  const meta = el('div', { class: 'dd-card-meta' });
  const period = (t.trip_start || t.trip_end)
    ? `${fmtDate(t.trip_start)} ~ ${fmtDate(t.trip_end)}` : '';
  if (period) meta.append(el('span', { class: 'dd-meta-text' }, '📅 ' + period));
  meta.append(el('span', { class: 'dd-meta-chip' }, `🧾 영수증 ${t.receipt_count || 0}건`));
  if (t.corp_cards && t.corp_cards.length)
    meta.append(el('span', { class: 'dd-meta-text' }, '💳 ' + t.corp_cards.join(', ')));
  card.append(meta);

  card.append(
    el('div', { class: 'dd-card-foot' },
      el('span', {}, t.supervisor_name ? `담당: ${t.supervisor_name}` : ''),
      el('span', { class: 'expd-total-chip' }, fmtTotals(t.totals)),
    )
  );
  return card;
}

async function toggleStatus(t, badgeEl) {
  const next = t.status === 'settled' ? 'open' : 'settled';
  badgeEl.style.opacity = '0.5';
  try {
    await api(`/api/biz-trips/${t.id}`, { method: 'PUT', body: JSON.stringify({ status: next }) });
    t.status = next;
    if (EXP.filters.status) { reload(); return; }
    badgeEl.textContent = next === 'settled' ? '정산완료' : '진행 중';
    badgeEl.classList.toggle('dd-badge-done', next === 'settled');
    badgeEl.classList.toggle('dd-badge-draft', next !== 'settled');
  } catch (e) {
    alert('상태 변경 실패: ' + e.message);
  } finally {
    badgeEl.style.opacity = '';
  }
}

// ─── Modal ───────────────────────────────────────────────────
function openNew() {
  EXP.editingId = null;
  $('#exp-modal-title').textContent = '신규 출장';
  $('#exp-btn-delete').hidden = true;
  $('#exp-btn-save-open').hidden = false;
  $('#exp-form').reset();
  $('#exp-status').value = 'open';
  openModal();
}

async function openEdit(id) {
  try {
    const t = await api(`/api/biz-trips/${id}`);
    EXP.editingId = id;
    $('#exp-modal-title').textContent = '출장 정보 편집';
    $('#exp-btn-delete').hidden = false;
    $('#exp-btn-save-open').hidden = false;
    $('#exp-title').value  = t.title || '';
    $('#exp-start').value  = t.trip_start || '';
    $('#exp-end').value    = t.trip_end || '';
    $('#exp-status').value = t.status || 'open';
    $('#exp-cards').value  = (t.corp_cards || []).join(', ');
    openModal();
  } catch (e) {
    alert('출장 로드 실패: ' + e.message);
  }
}

function openModal() { $('#exp-modal').hidden = false; document.body.classList.add('modal-open'); }
function closeModal() { $('#exp-modal').hidden = true; document.body.classList.remove('modal-open'); }

function collectForm() {
  return {
    title:       $('#exp-title').value.trim(),
    trip_start:  $('#exp-start').value || null,
    trip_end:    $('#exp-end').value || null,
    status:      $('#exp-status').value || 'open',
    corp_cards:  $('#exp-cards').value,
  };
}

async function saveTrip(thenOpen = false) {
  const data = collectForm();
  if (!data.title) { alert('출장명을 입력하세요.'); return; }
  try {
    let id = EXP.editingId;
    if (id) {
      await api(`/api/biz-trips/${id}`, { method: 'PUT', body: JSON.stringify(data) });
    } else {
      const res = await api('/api/biz-trips', { method: 'POST', body: JSON.stringify(data) });
      id = res.id;
    }
    closeModal();
    if (thenOpen && id) { window.location = `/expenses/${id}`; return; }
    await reload();
  } catch (e) {
    alert('저장 실패: ' + e.message);
  }
}

async function deleteTrip() {
  if (!EXP.editingId) return;
  if (!confirm('이 출장과 모든 영수증을 삭제합니다. 계속할까요?')) return;
  try {
    await api(`/api/biz-trips/${EXP.editingId}`, { method: 'DELETE' });
    closeModal();
    await reload();
  } catch (e) {
    alert('삭제 실패: ' + e.message);
  }
}

// ─── Events ──────────────────────────────────────────────────
function bindEvents() {
  $('#btn-new-trip').addEventListener('click', openNew);
  $('#exp-btn-save').addEventListener('click', () => saveTrip(false));
  $('#exp-btn-save-open').addEventListener('click', () => saveTrip(true));
  $('#exp-btn-delete').addEventListener('click', deleteTrip);

  $('#exp-modal').addEventListener('click', (ev) => {
    if (ev.target.dataset.close === '1') closeModal();
  });

  let searchTimer;
  $('#exp-search').addEventListener('input', (e) => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { EXP.filters.q = e.target.value.trim(); reload(); }, 250);
  });
  $('#exp-filter-status').addEventListener('change', (e) => {
    EXP.filters.status = e.target.value;
    reload();
  });
}

document.addEventListener('DOMContentLoaded', init);
