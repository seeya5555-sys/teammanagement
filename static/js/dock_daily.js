(() => {
  'use strict';
  const $ = (s) => document.querySelector(s);
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const state = { projects: [], project: null, reports: [], report: null, dirty: false, vessels: [] };
  async function api(url, options) {
    const r = await fetch(url, options);
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.error || `요청 실패 (${r.status})`);
    return body;
  }
  const json = (v) => ({headers:{'Content-Type':'application/json'}, body:JSON.stringify(v)});
  function err(e) { $('#dd-error').textContent = e.message || String(e); }
  function clearErr() { $('#dd-error').textContent = ''; }
  async function loadProjects() {
    state.projects = await api('/api/dock-daily/projects');
    $('#dd-project-list').innerHTML = state.projects.length ? state.projects.map(p =>
      `<button data-project="${p.id}" class="${state.project && state.project.id === p.id ? 'active' : ''}"><b>${esc(p.vessel_name)}</b><br><span class="dd-muted">${esc(p.title)} · ${p.report_count || 0}일</span></button>`).join('')
      : '<p class="dd-muted">등록된 프로젝트가 없습니다.</p>';
    document.querySelectorAll('[data-project]').forEach(b => b.onclick = () => selectProject(+b.dataset.project));
  }
  async function selectProject(id) {
    clearErr(); state.project = state.projects.find(p => p.id === id) || null;
    if (!state.project) return;
    state.reports = await api(`/api/dock-daily/projects/${id}/reports`);
    await loadProjects();
    $('#dd-project-tools').style.display = 'block';
    $('#dd-generate-date').value = new Date().toLocaleDateString('en-CA');
    const specials = (state.project.sections || []).filter(s => s.kind === 'special');
    $('#dd-special-tools').innerHTML = specials.length ? '<b>Special 항목</b>' + specials.map(s => `<label style="display:block;margin-top:7px"><input type="checkbox" class="dd-special-toggle" data-key="${esc(s.section_key)}" ${s.enabled ? 'checked' : ''}> ${esc(s.label)}</label>`).join('') : '<span class="dd-muted">Special 항목 없음</span>';
    document.querySelectorAll('.dd-special-toggle').forEach(t => t.onchange = () => toggleSpecial(t.dataset.key, t.checked));
    $('#dd-empty').style.display = 'block'; $('#dd-report').classList.remove('show');
    if (state.reports.length) await selectReport(state.reports[0].id);
  }
  async function selectReport(id) {
    clearErr(); state.report = await api(`/api/dock-daily/reports/${id}`); state.dirty = false;
    $('#dd-empty').style.display = 'none'; $('#dd-report').classList.add('show');
    $('#dd-report-title').textContent = `${state.report.vessel_name} · 입거 Daily Report`;
    $('#dd-report-meta').textContent = `${state.report.status} · revision ${state.report.revision}`;
    $('#dd-date').value = state.report.report_date;
    $('#dd-report-list').innerHTML = state.reports.map(r => `<option value="${r.id}" ${r.id === state.report.id ? 'selected' : ''}>${esc(r.report_date)} · ${esc(r.status)}</option>`).join('');
    $('#dd-report-list').onchange = () => selectReport(Number($('#dd-report-list').value)).catch(err);
    const it = [['BERTHING',state.report.berthing_date],['DRY DOCK IN',state.report.dock_in_date],['DRY DOCK OUT',state.report.dock_out_date],['DEPARTURE',state.report.departure_date]];
    $('#dd-itinerary').innerHTML = it.map(x => `<b>${x[0]}</b><span>${esc(x[1] || '-')}</span>`).join('');
    renderSections();
    const locked = state.report.status === 'final';
    ['#dd-save','#dd-final','#dd-add-block'].forEach(s => $(s).disabled = locked);
  }
  function renderSections() {
    const blocks = state.report.blocks || [];
    $('#dd-sections').innerHTML = (state.report.sections || []).filter(s => s.enabled).map(s => {
      const bs = blocks.filter(b => b.section_key === s.section_key);
      const disabled = state.report.status === 'final' ? ' disabled' : '';
      return `<div class="dd-card dd-section" data-section="${esc(s.section_key)}"><div class="dd-section-head"><h3>${esc(s.label)}</h3><button class="dd-btn alt add-section" data-section="${esc(s.section_key)}"${disabled}>＋ 추가</button></div>${bs.length ? bs.map(b => blockHtml(b)).join('') : '<p class="dd-muted">NIL</p>'}</div>`;
    }).join('');
    document.querySelectorAll('.add-section').forEach(b => b.onclick = () => addBlock(b.dataset.section));
    document.querySelectorAll('.edit-block').forEach(b => b.onclick = () => editBlock(+b.dataset.id));
    document.querySelectorAll('.delete-block').forEach(b => b.onclick = () => deleteBlock(+b.dataset.id));
  }
  function blockText(b) { const c = b.content || {}; return c.title || c.body || c.text || (b.block_type === 'table' ? JSON.stringify(c.rows || []) : ''); }
  function blockHtml(b) { const disabled = state.report.status === 'final' ? ' disabled' : ''; return `<div class="dd-block"><span class="dd-badge ${b.origin === 'dock_auto' ? 'auto' : ''}">${b.origin === 'dock_auto' ? '자동수집' : '수동'}</span> ${b.manual_override ? '<span class="dd-badge">수동 수정 보호</span>' : ''}<div>${esc(blockText(b))}</div><button class="dd-btn alt edit-block" data-id="${b.id}"${disabled}>편집</button> <button class="dd-btn warn delete-block" data-id="${b.id}"${disabled}>삭제</button></div>`; }
  function addBlock(section) {
    const text = prompt(`${section} 블록 내용`); if (!text) return;
    state.report.blocks.push({id:0, section_key:section, block_type:'paragraph', content:{body:text}, sort_order:0, origin:'manual', manual_override:1, _new:true});
    state.dirty = true; renderSections();
  }
  function editBlock(id) {
    const b = state.report.blocks.find(x => x.id === id); if (!b) return;
    const text = prompt('블록 내용', blockText(b)); if (text === null) return;
    b.content = {...(b.content || {}), body:text}; b.block_type = b.block_type || 'paragraph'; b._edit = true; state.dirty = true; renderSections();
  }
  function deleteBlock(id) {
    const b = state.report.blocks.find(x => x.id === id); if (!b || !confirm('이 블록을 삭제할까요?')) return;
    if (b._new) state.report.blocks = state.report.blocks.filter(x => x !== b); else { b._delete = true; b._deleted = true; }
    state.dirty = true; renderSections();
  }
  async function save() {
    if (!state.report) return;
    clearErr(); const operations = [];
    state.report.blocks.filter(b => b._delete).forEach(b => operations.push({op:'delete', id:b.id}));
    state.report.blocks.filter(b => !b._delete && (b._new || b._edit)).forEach(b => operations.push({op:'upsert', id:b._new ? undefined : b.id, section_key:b.section_key, block_type:b.block_type, content:b.content, sort_order:b.sort_order || 0}));
    const fresh = await api(`/api/dock-daily/reports/${state.report.id}`, {...json({revision:state.report.revision, operations}), method:'PUT'});
    state.report = fresh; state.dirty = false; renderSections(); $('#dd-report-meta').textContent = `${fresh.status} · revision ${fresh.revision}`;
  }
  async function toggleSpecial(key, enabled) {
    const updated = await api(`/api/dock-daily/projects/${state.project.id}`, {...json({sections:[{section_key:key, enabled}]}), method:'PATCH'});
    state.project = updated; state.projects = state.projects.map(p => p.id === updated.id ? updated : p);
    if (state.report) { state.report.sections = updated.sections; renderSections(); }
  }
  async function preview(kind) {
    if (state.dirty) await save();
    const v = await api(`/api/dock-daily/reports/${state.report.id}/${kind}-preview`);
    $('#dd-preview').textContent = kind === 'email' ? v.text : JSON.stringify(v, null, 2);
  }
  $('#dd-save').onclick = () => save().catch(err); $('#dd-email').onclick = () => preview('email').catch(err); $('#dd-svms').onclick = () => preview('svms').catch(err);
  $('#dd-final').onclick = async () => { if (!state.report || !confirm('이 보고서를 확정하면 더 이상 수정할 수 없습니다. 확정할까요?')) return; try { if (state.dirty) await save(); state.report = await api(`/api/dock-daily/reports/${state.report.id}`, {...json({revision:state.report.revision, status:'final', operations:[]}), method:'PUT'}); await selectReport(state.report.id); } catch(e) { err(e); } };
  $('#dd-generate').onclick = async () => { if (!state.project) return; const report_date = $('#dd-generate-date').value; if (!report_date) return err(new Error('보고서 일자를 선택하세요.')); try { const report = await api(`/api/dock-daily/projects/${state.project.id}/reports/generate`, {...json({report_date}), method:'POST'}); state.reports = await api(`/api/dock-daily/projects/${state.project.id}/reports`); await selectReport(report.id); } catch(e) { err(e); } };
  $('#dd-add-block').onclick = () => { const s = state.report?.sections?.find(x => x.enabled); if (s) addBlock(s.section_key); };
  const projectModal = $('#dd-project-modal');
  const projectForm = $('#dd-project-form');
  const projectError = $('#ddp-error');
  const autoToggle = $('#ddp-auto');
  const today = () => new Date().toLocaleDateString('en-CA');
  function setAutoFields() {
    const enabled = autoToggle.checked;
    $('#ddp-auto-fields').hidden = !enabled;
    ['#ddp-active-from','#ddp-active-to','#ddp-source-ids'].forEach(s => $(s).required = enabled);
    if (enabled && !$('#ddp-active-from').value) {
      $('#ddp-active-from').value = today();
      $('#ddp-active-to').value = today();
    }
  }
  async function openProjectModal() {
    projectForm.reset(); projectError.textContent = ''; setAutoFields();
    try {
      if (!state.vessels.length) state.vessels = await api('/api/vessels');
      $('#ddp-vessel').innerHTML = '<option value="">활성 선박을 선택하세요</option>' + state.vessels.map(v => `<option value="${v.id}">${esc(v.name)}${v.vsl_cd ? ` · ${esc(v.vsl_cd)}` : ''}</option>`).join('');
      projectModal.hidden = false; document.body.style.overflow = 'hidden'; $('#ddp-vessel').focus();
    } catch (e) { err(e); }
  }
  function closeProjectModal() { projectModal.hidden = true; document.body.style.overflow = ''; }
  autoToggle.onchange = setAutoFields;
  $('#dd-new-project').onclick = openProjectModal;
  $('#dd-project-close').onclick = closeProjectModal;
  $('#dd-project-cancel').onclick = closeProjectModal;
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && !projectModal.hidden) closeProjectModal(); });
  projectForm.onsubmit = async e => {
    e.preventDefault(); projectError.textContent = '';
    if (!projectForm.reportValidity()) return;
    const auto_generate = autoToggle.checked;
    const active_from = $('#ddp-active-from').value || null;
    const active_to = $('#ddp-active-to').value || null;
    const sourceIds = $('#ddp-source-ids').value.split(',').map(x => x.trim()).filter(Boolean);
    if (auto_generate && active_from > active_to) return projectError.textContent = '자동작성 종료일은 시작일보다 빠를 수 없습니다.';
    if (auto_generate && sourceIds.some(x => !/^v_[A-Za-z0-9][A-Za-z0-9_.:-]*$/.test(x))) return projectError.textContent = 'Dock Manager 원천 ID는 모두 v_로 시작해야 합니다.';
    const button = $('#dd-project-create'); button.disabled = true; button.textContent = '생성 중…';
    try {
      const created = await api('/api/dock-daily/projects', {...json({
        vessel_id:Number($('#ddp-vessel').value), title:$('#ddp-title').value.trim(),
        berthing_date:$('#ddp-berthing').value || null, dock_in_date:$('#ddp-dock-in').value || null,
        dock_out_date:$('#ddp-dock-out').value || null, departure_date:$('#ddp-departure').value || null,
        active_from:auto_generate ? active_from : null, active_to:auto_generate ? active_to : null,
        auto_generate, dock_manager_project_ids:auto_generate ? sourceIds : [],
        svms_dk_cd:$('#ddp-svms-dk').value.trim() || null,
        special_sections:$('#ddp-egcs').checked ? [{section_key:'egcs', label:'EGCS Retrofit', enabled:true}] : []
      }), method:'POST'});
      closeProjectModal(); await loadProjects(); await selectProject(created.id);
    } catch (error) { projectError.textContent = error.message || String(error); }
    finally { button.disabled = false; button.textContent = '프로젝트 생성'; }
  };
  loadProjects().catch(err);
})();
