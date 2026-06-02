// ════════════════════════════════════════════════════════════════
//  Boarding Report — Step 1: 보고서 목록 + 메타 CRUD
// ════════════════════════════════════════════════════════════════
const B = {
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
async function init() {
  try {
    [B.vessels, B.supervisors] = await Promise.all([
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
  const sel = $('#brep-filter-vessel');
  const cur = sel.value;
  sel.innerHTML = '';
  sel.append(el('option', { value: '' }, 'All 선박'));
  for (const v of B.vessels) {
    sel.append(el('option', { value: v.id }, v.name));
  }
  sel.value = B.vessels.find(v => v.id == cur) ? cur : '';
}

async function reload() {
  const p = new URLSearchParams();
  if (B.filters.q)         p.set('q', B.filters.q);
  if (B.filters.vessel_id) p.set('vessel_id', B.filters.vessel_id);
  if (B.filters.status)    p.set('status', B.filters.status);

  try {
    B.reports = await api('/api/boarding-reports?' + p);
  } catch (e) {
    alert('보고서 목록 로드 실패: ' + e.message);
    B.reports = [];
  }
  render();
}

function render() {
  const grid = $('#brep-grid');
  $$('.dd-card', grid).forEach(n => n.remove());

  const empty = $('#brep-empty');
  if (B.reports.length === 0) {
    empty.hidden = false;
    $('#brep-count-label').textContent = '';
    return;
  }
  empty.hidden = true;
  $('#brep-count-label').textContent = `${B.reports.length}건`;

  for (const r of B.reports) {
    grid.append(renderCard(r));
  }
}

function fmtDate(s) {
  if (!s) return '';
  return s.replace(/-/g, '.');
}

function fmtPeriod(r) {
  if (!r.boarding_start && !r.boarding_end) return '';
  const s = fmtDate(r.boarding_start);
  const e = fmtDate(r.boarding_end);
  if (s && e) {
    const days = Math.round(
      (new Date(r.boarding_end) - new Date(r.boarding_start)) / 86400000
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
    onclick: () => { window.location = `/boarding/${r.id}/edit`; },
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

  const headRight = el('div', { class: 'dd-card-head-right' }, statusBadge);
  if (canEdit) {
    headRight.append(el('button', {
      class: 'dd-card-edit', type: 'button', title: '보고서 정보 편집',
      onclick: (ev) => { ev.stopPropagation(); openEdit(r.id); },
    }, '⋮'));
  }

  card.append(
    el('div', { class: 'dd-card-head' },
      el('h3', { class: 'dd-card-title' }, r.title),
      headRight,
    ),
    el('div', { class: 'dd-card-vessel' }, r.vessel_name || '—'),
  );

  const meta = el('div', { class: 'dd-card-meta' });
  if (r.port)     meta.append(el('span', { class: 'dd-meta-text' }, '🏭 ' + r.port));
  const period = fmtPeriod(r);
  if (period)     meta.append(el('span', { class: 'dd-meta-text' }, '📅 ' + period));
  card.append(meta);

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
    await api(`/api/boarding-reports/${r.id}`, {
      method: 'PUT', body: JSON.stringify({ status: next }),
    });
    r.status = next;
    if (B.filters.status) {
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

// ─── Modal ───────────────────────────────────────────────────
function fillSelects() {
  const vSel = $('#brep-vessel');
  const sSel = $('#brep-supervisor');
  vSel.innerHTML = '<option value="">선박 선택...</option>';
  for (const v of B.vessels) vSel.append(el('option', { value: v.id }, v.name));
  sSel.innerHTML = '<option value="">미지정</option>';
  for (const s of B.supervisors) sSel.append(el('option', { value: s.id }, s.name));
}

function openNew() {
  B.editingId = null;
  $('#brep-modal-title').textContent = '신규 보고서';
  $('#brep-btn-delete').hidden = true;
  $('#brep-btn-save-edit').hidden = false;
  fillSelects();
  $('#brep-form').reset();
  $('#brep-status').value = 'draft';
  $('#brep-template-name-row').hidden = true;
  openModal();
}

async function openEdit(id) {
  try {
    const r = await api(`/api/boarding-reports/${id}`);
    B.editingId = id;
    $('#brep-modal-title').textContent = '보고서 정보 편집';
    $('#brep-btn-delete').hidden = false;
    $('#brep-btn-save-edit').hidden = false;
    fillSelects();

    $('#brep-title').value         = r.title || '';
    $('#brep-vessel').value        = r.vessel_id || '';
    $('#brep-supervisor').value    = r.supervisor_id || '';
    $('#brep-status').value        = r.status || 'draft';
    $('#brep-port').value          = r.port || '';
    $('#brep-period-start').value  = r.boarding_start || '';
    $('#brep-period-end').value    = r.boarding_end || '';
    $('#brep-master-name').value   = r.master_name || '';
    $('#brep-master-date').value   = r.master_board_date || '';
    $('#brep-ce-name').value       = r.chief_eng_name || '';
    $('#brep-ce-date').value       = r.chief_eng_board_date || '';
    $('#brep-checklist-score').value = r.sv_checklist_score || '';
    $('#brep-app-drafter').value   = r.approval_drafter || '';
    $('#brep-app-team').value      = r.approval_team_lead || '';
    $('#brep-app-director').value  = r.approval_director || '';
    $('#brep-app-ceo').value       = r.approval_ceo || '';
    $('#brep-is-template').checked = !!r.is_template;
    $('#brep-template-name').value = r.template_name || '';
    $('#brep-template-name-row').hidden = !r.is_template;

    openModal();
  } catch (e) {
    alert('보고서 로드 실패: ' + e.message);
  }
}

function openModal() {
  $('#brep-modal').hidden = false;
  document.body.classList.add('modal-open');
}
function closeModal() {
  $('#brep-modal').hidden = true;
  document.body.classList.remove('modal-open');
}

function collectForm() {
  const isT = $('#brep-is-template').checked;
  return {
    title:               $('#brep-title').value.trim(),
    vessel_id:           $('#brep-vessel').value || null,
    supervisor_id:       $('#brep-supervisor').value || null,
    status:              $('#brep-status').value || 'draft',
    port:                $('#brep-port').value.trim(),
    boarding_start:      $('#brep-period-start').value || null,
    boarding_end:        $('#brep-period-end').value || null,
    master_name:         $('#brep-master-name').value.trim(),
    master_board_date:   $('#brep-master-date').value || null,
    chief_eng_name:      $('#brep-ce-name').value.trim(),
    chief_eng_board_date:$('#brep-ce-date').value || null,
    sv_checklist_score:  $('#brep-checklist-score').value.trim(),
    approval_drafter:    $('#brep-app-drafter').value.trim(),
    approval_team_lead:  $('#brep-app-team').value.trim(),
    approval_director:   $('#brep-app-director').value.trim(),
    approval_ceo:        $('#brep-app-ceo').value.trim(),
    is_template:         isT,
    template_name:       isT ? $('#brep-template-name').value.trim() : null,
  };
}

async function saveReport(thenEdit = false) {
  const data = collectForm();
  if (!data.title) { alert('제목을 입력하세요.'); return; }
  if (!data.vessel_id) { alert('선박을 선택하세요.'); return; }

  try {
    if (B.editingId) {
      await api(`/api/boarding-reports/${B.editingId}`, {
        method: 'PUT', body: JSON.stringify(data),
      });
    } else {
      const res = await api('/api/boarding-reports', {
        method: 'POST', body: JSON.stringify(data),
      });
      B.editingId = res.id;
    }
    closeModal();
    await reload();
    if (thenEdit) {
      window.location = `/boarding/${B.editingId}/edit`;
    }
  } catch (e) {
    alert('저장 실패: ' + e.message);
  }
}

async function deleteReport() {
  if (!B.editingId) return;
  if (!confirm('이 보고서를 삭제하시겠습니까?\n섹션·블록 데이터도 모두 함께 삭제됩니다.')) return;
  try {
    await api(`/api/boarding-reports/${B.editingId}`, { method: 'DELETE' });
    closeModal();
    await reload();
  } catch (e) {
    alert('삭제 실패: ' + e.message);
  }
}

// ─── Events ──────────────────────────────────────────────────
function bindEvents() {
  $('#brep-btn-new').addEventListener('click', openNew);

  $('#brep-modal').addEventListener('click', (ev) => {
    if (ev.target.dataset.close === '1') closeModal();
  });
  $('#brep-form').addEventListener('submit', (e) => {
    e.preventDefault();
    saveReport(false);
  });
  $('#brep-btn-save').addEventListener('click', () => saveReport(false));
  $('#brep-btn-save-edit').addEventListener('click', () => saveReport(true));
  $('#brep-btn-delete').addEventListener('click', deleteReport);

  $('#brep-is-template').addEventListener('change', (e) => {
    $('#brep-template-name-row').hidden = !e.target.checked;
  });

  let searchTimer;
  $('#brep-search').addEventListener('input', (e) => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      B.filters.q = e.target.value.trim();
      reload();
    }, 220);
  });
  $('#brep-filter-vessel').addEventListener('change', (e) => {
    B.filters.vessel_id = e.target.value;
    reload();
  });
  $('#brep-filter-status').addEventListener('change', (e) => {
    B.filters.status = e.target.value;
    reload();
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !$('#brep-modal').hidden) closeModal();
  });
}

init();
