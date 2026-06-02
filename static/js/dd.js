// ════════════════════════════════════════════════════════════════
//  Dry Dock Report — Step 1: 보고서 목록 + 메타 CRUD
//  · 본문 편집 / 추출 기능은 Step 2~3에서 추가
// ════════════════════════════════════════════════════════════════
const DD = {
  reports: [],
  vessels: [],
  supervisors: [],
  filters: { q: '', vessel_id: '', status: '' },
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

function escHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  }[c]));
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

// ─────────────────────────────────────────────────────────────
//  Load & Render
// ─────────────────────────────────────────────────────────────
async function init() {
  try {
    [DD.vessels, DD.supervisors] = await Promise.all([
      api('/api/vessels/all').catch(() => api('/api/vessels')),
      api('/api/supervisors').catch(() => []),
    ]);
  } catch (e) {
    console.error('초기 데이터 로드 실패', e);
  }
  renderVesselFilter();
  bindEvents();
  await reload();
}

function renderVesselFilter() {
  const sel = $('#dd-filter-vessel');
  const cur = sel.value;
  sel.innerHTML = '';
  sel.append(el('option', { value: '' }, 'All 선박'));
  for (const v of DD.vessels) {
    sel.append(el('option', { value: v.id }, v.name));
  }
  sel.value = DD.vessels.find(v => v.id == cur) ? cur : '';
}

async function reload() {
  const p = new URLSearchParams();
  if (DD.filters.q)         p.set('q', DD.filters.q);
  if (DD.filters.vessel_id) p.set('vessel_id', DD.filters.vessel_id);
  if (DD.filters.status)    p.set('status', DD.filters.status);

  try {
    DD.reports = await api('/api/dock-reports?' + p);
  } catch (e) {
    alert('보고서 목록 로드 실패: ' + e.message);
    DD.reports = [];
  }
  render();
}

function render() {
  const grid = $('#dd-grid');
  // 기존 카드만 제거 (empty-state는 유지)
  $$('.dd-card', grid).forEach(n => n.remove());

  const empty = $('#dd-empty');
  if (DD.reports.length === 0) {
    empty.hidden = false;
    $('#dd-count-label').textContent = '';
    return;
  }
  empty.hidden = true;
  $('#dd-count-label').textContent = `${DD.reports.length}건`;

  for (const r of DD.reports) {
    grid.append(renderCard(r));
  }
}

function fmtDate(s) {
  if (!s) return '';
  // YYYY-MM-DD → YYYY.MM.DD
  return s.replace(/-/g, '.');
}

function fmtPeriod(r) {
  if (!r.period_start && !r.period_end) return '';
  const s = fmtDate(r.period_start);
  const e = fmtDate(r.period_end);
  if (s && e) {
    // 일수 계산
    const days = Math.round(
      (new Date(r.period_end) - new Date(r.period_start)) / 86400000
    ) + 1;
    return `${s} ~ ${e}  (${days}일)`;
  }
  return s || e;
}

function renderCard(r) {
  const canEdit = !!r.can_edit;
  const card = el('div', {
    class: 'dd-card' + (canEdit ? '' : ' dd-card-readonly'),
    'data-id': r.id,
    onclick: () => { window.location = `/dry-dock/${r.id}/edit`; },
  });

  const statusBadge = el('span', {
    class: `dd-badge ${r.status === 'done' ? 'dd-badge-done' : 'dd-badge-draft'}`
  }, r.status === 'done' ? '완료' : '진행 중');
  if (canEdit) {
    statusBadge.classList.add('dd-badge-clickable');
    statusBadge.title = '클릭하여 진행 중 / 완료 전환';
    statusBadge.addEventListener('click', (ev) => {
      ev.stopPropagation();
      toggleStatus(r, statusBadge);
    });
  }

  // 메타 편집 버튼 (편집 권한 있을 때만)
  const headRight = el('div', { class: 'dd-card-head-right' }, statusBadge);
  if (canEdit) {
    const metaBtn = el('button', {
      class: 'dd-card-edit',
      type: 'button',
      title: '보고서 정보 편집',
      onclick: (ev) => { ev.stopPropagation(); openEdit(r.id); },
    }, '⋮');
    headRight.append(metaBtn);
  }

  card.append(
    el('div', { class: 'dd-card-head' },
      el('h3', { class: 'dd-card-title' }, r.title),
      headRight,
    )
  );

  // 선박명 (강조)
  card.append(
    el('div', { class: 'dd-card-vessel' }, r.vessel_name || '—')
  );

  // 메타 정보 (조선소 / 기간 / 회차)
  const meta = el('div', { class: 'dd-card-meta' });
  if (r.dock_no)   meta.append(el('span', { class: 'dd-meta-chip' }, r.dock_no));
  if (r.shipyard)  meta.append(el('span', { class: 'dd-meta-text' }, '🏭 ' + r.shipyard));
  const period = fmtPeriod(r);
  if (period)      meta.append(el('span', { class: 'dd-meta-text' }, '📅 ' + period));
  card.append(meta);

  // 푸터 — 작성자, 업데이트 시각
  card.append(
    el('div', { class: 'dd-card-foot' },
      el('span', {}, r.supervisor_name ? `담당: ${r.supervisor_name}` : ''),
      el('span', {}, r.updated_at ? `최근 수정: ${fmtDate(r.updated_at.slice(0,10))}` : ''),
    )
  );

  return card;
}

// 카드 badge 클릭 → 진행 중 ↔ 완료 즉시 전환
async function toggleStatus(r, badgeEl) {
  const next = r.status === 'done' ? 'draft' : 'done';
  badgeEl.style.opacity = '0.5';
  try {
    await api(`/api/dock-reports/${r.id}`, {
      method: 'PUT', body: JSON.stringify({ status: next }),
    });
    r.status = next;
    if (DD.filters.status) {
      reload();   // 상태 필터 적용 중이면 목록을 다시 불러와 갱신
      return;
    }
    badgeEl.textContent = next === 'done' ? '완료' : '진행 중';
    badgeEl.classList.toggle('dd-badge-done', next === 'done');
    badgeEl.classList.toggle('dd-badge-draft', next !== 'done');
  } catch (e) {
    alert('상태 변경 실패: ' + e.message);
  } finally {
    badgeEl.style.opacity = '';
  }
}
function fillVesselSupervisorSelects() {
  const vSel = $('#dd-vessel');
  const sSel = $('#dd-supervisor');
  vSel.innerHTML = '<option value="">선박 선택...</option>';
  for (const v of DD.vessels) vSel.append(el('option', { value: v.id }, v.name));
  sSel.innerHTML = '<option value="">미지정</option>';
  for (const s of DD.supervisors) sSel.append(el('option', { value: s.id }, s.name));
}

function openNew() {
  DD.editingId = null;
  $('#dd-modal-title').textContent = '신규 보고서';
  $('#dd-btn-delete').hidden = true;
  $('#dd-btn-save-edit').hidden = false;
  fillVesselSupervisorSelects();
  // 폼 초기화
  $('#dd-form').reset();
  $('#dd-status').value = 'draft';
  $('#dd-template-name-row').hidden = true;
  openModal();
}

async function openEdit(id) {
  try {
    const r = await api(`/api/dock-reports/${id}`);
    DD.editingId = id;
    $('#dd-modal-title').textContent = '보고서 정보 편집';
    $('#dd-btn-delete').hidden = false;
    $('#dd-btn-save-edit').hidden = false;
    fillVesselSupervisorSelects();

    $('#dd-title').value         = r.title || '';
    $('#dd-vessel').value        = r.vessel_id || '';
    $('#dd-supervisor').value    = r.supervisor_id || '';
    $('#dd-status').value        = r.status || 'draft';
    $('#dd-dock-no').value       = r.dock_no || '';
    $('#dd-shipyard').value      = r.shipyard || '';
    $('#dd-period-start').value  = r.period_start || '';
    $('#dd-period-end').value    = r.period_end || '';
    $('#dd-imo').value           = r.imo_no || '';
    $('#dd-gt').value            = r.gross_tonnage || '';
    $('#dd-dwt').value           = r.dead_weight || '';
    $('#dd-app-drafter').value   = r.approval_drafter || '';
    $('#dd-app-team').value      = r.approval_team_lead || '';
    $('#dd-app-director').value  = r.approval_director || '';
    $('#dd-app-ceo').value       = r.approval_ceo || '';
    $('#dd-is-template').checked = !!r.is_template;
    $('#dd-template-name').value = r.template_name || '';
    $('#dd-template-name-row').hidden = !r.is_template;

    openModal();
  } catch (e) {
    alert('보고서 로드 실패: ' + e.message);
  }
}

function openModal() {
  $('#dd-modal').hidden = false;
  document.body.classList.add('modal-open');
}
function closeModal() {
  $('#dd-modal').hidden = true;
  document.body.classList.remove('modal-open');
}

function collectForm() {
  const isT = $('#dd-is-template').checked;
  return {
    title:               $('#dd-title').value.trim(),
    vessel_id:           $('#dd-vessel').value || null,
    supervisor_id:       $('#dd-supervisor').value || null,
    status:              $('#dd-status').value || 'draft',
    dock_no:             $('#dd-dock-no').value.trim(),
    shipyard:            $('#dd-shipyard').value.trim(),
    period_start:        $('#dd-period-start').value || null,
    period_end:          $('#dd-period-end').value || null,
    imo_no:              $('#dd-imo').value.trim(),
    gross_tonnage:       $('#dd-gt').value.trim(),
    dead_weight:         $('#dd-dwt').value.trim(),
    approval_drafter:    $('#dd-app-drafter').value.trim(),
    approval_team_lead:  $('#dd-app-team').value.trim(),
    approval_director:   $('#dd-app-director').value.trim(),
    approval_ceo:        $('#dd-app-ceo').value.trim(),
    is_template:         isT,
    template_name:       isT ? $('#dd-template-name').value.trim() : null,
  };
}

async function saveReport(thenEdit = false) {
  const data = collectForm();
  if (!data.title) { alert('제목을 입력하세요.'); return; }
  if (!data.vessel_id) { alert('선박을 선택하세요.'); return; }

  try {
    if (DD.editingId) {
      await api(`/api/dock-reports/${DD.editingId}`, {
        method: 'PUT',
        body: JSON.stringify(data),
      });
    } else {
      const res = await api('/api/dock-reports', {
        method: 'POST',
        body: JSON.stringify(data),
      });
      DD.editingId = res.id;
    }
    closeModal();
    await reload();
    if (thenEdit) {
      // 본문 편집 페이지로 이동
      window.location = `/dry-dock/${DD.editingId}/edit`;
    }
  } catch (e) {
    alert('저장 실패: ' + e.message);
  }
}

async function deleteReport() {
  if (!DD.editingId) return;
  if (!confirm('이 보고서를 삭제하시겠습니까?\n섹션·블록 데이터도 모두 함께 삭제됩니다.')) return;
  try {
    await api(`/api/dock-reports/${DD.editingId}`, { method: 'DELETE' });
    closeModal();
    await reload();
  } catch (e) {
    alert('삭제 실패: ' + e.message);
  }
}

// ─────────────────────────────────────────────────────────────
//  Events
// ─────────────────────────────────────────────────────────────
function bindEvents() {
  $('#btn-new-dock').addEventListener('click', openNew);

  $('#dd-modal').addEventListener('click', (ev) => {
    if (ev.target.dataset.close === '1') closeModal();
  });
  $('#dd-form').addEventListener('submit', (e) => {
    e.preventDefault();
    saveReport(false);
  });
  $('#dd-btn-save').addEventListener('click', () => saveReport(false));
  $('#dd-btn-save-edit').addEventListener('click', () => saveReport(true));
  $('#dd-btn-delete').addEventListener('click', deleteReport);

  $('#dd-is-template').addEventListener('change', (e) => {
    $('#dd-template-name-row').hidden = !e.target.checked;
  });

  let searchTimer;
  $('#dd-search').addEventListener('input', (e) => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      DD.filters.q = e.target.value.trim();
      reload();
    }, 220);
  });
  $('#dd-filter-vessel').addEventListener('change', (e) => {
    DD.filters.vessel_id = e.target.value;
    reload();
  });
  $('#dd-filter-status').addEventListener('change', (e) => {
    DD.filters.status = e.target.value;
    reload();
  });

  // ESC로 모달 닫기
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !$('#dd-modal').hidden) closeModal();
  });
}

init();
