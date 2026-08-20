(() => {
  'use strict';
  const $ = s => document.querySelector(s);
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const state = {projects:[], project:null, reports:[], report:null, dirty:false, vessels:[], tempId:-1, preview:null};
  async function api(url, options) {
    const r = await fetch(url, options); const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.error || `요청 실패 (${r.status})`); return body;
  }
  const json = v => ({headers:{'Content-Type':'application/json'}, body:JSON.stringify(v)});
  const err = e => { $('#dd-error').textContent = e.message || String(e); };
  const clearErr = () => { $('#dd-error').textContent = ''; };
  const today = () => new Date().toLocaleDateString('en-CA');

  async function loadProjects() {
    state.projects = await api('/api/dock-daily/projects');
    $('#dd-project-list').innerHTML = state.projects.length ? state.projects.map(p =>
      `<div class="dd-list-row"><button data-project="${p.id}" class="${state.project?.id===p.id?'active':''}"><b>${esc(p.vessel_name)}</b><br><span class="dd-muted">${esc(p.title)} · ${p.report_count||0}일</span></button><button class="dd-list-del" type="button" data-del-project="${p.id}" title="프로젝트 삭제" aria-label="${esc(p.vessel_name)} 프로젝트 삭제">삭제</button></div>`).join('') : '<p class="dd-muted">등록된 프로젝트가 없습니다.</p>';
    document.querySelectorAll('[data-project]').forEach(b => b.onclick = () => {if(canLeaveDraft())selectProject(+b.dataset.project);});
    document.querySelectorAll('[data-del-project]').forEach(b => b.onclick = () => once(b, () => deleteProject(+b.dataset.delProject)));
  }
  function renderReportDates() {
    $('#dd-report-list').innerHTML = state.reports.length ? state.reports.map(r =>
      `<div class="dd-list-row"><button data-report="${r.id}" class="${state.report?.id===r.id?'active':''}"><b>${esc(r.report_date)}</b><small>${esc(r.status)}</small></button><button class="dd-list-del" type="button" data-del-report="${r.id}" title="이 일자 삭제" aria-label="${esc(r.report_date)} 보고서 삭제">삭제</button></div>`).join('') : '<p class="dd-muted">생성된 일자가 없습니다.</p>';
    document.querySelectorAll('[data-report]').forEach(b => b.onclick = () => {if(canLeaveDraft())selectReport(+b.dataset.report).catch(err);});
    document.querySelectorAll('[data-del-report]').forEach(b => b.onclick = () => once(b, () => deleteReport(+b.dataset.delReport)));
  }
  // A second click while the first DELETE is in flight would send a duplicate
  // request, so the button is held down for the whole round trip. Always
  // released again: a cancelled confirm() does no work at all and must not
  // leave the row dead. On success the button has usually been re-rendered
  // away by then, so the release just touches a detached node.
  async function once(button, run) {
    if (button.disabled) return;
    button.disabled = true;
    try { await run(); } catch (e) { err(e); } finally { button.disabled = false; }
  }
  // Deleting cascades on the server (blocks, source links, revisions,
  // attachments), so both of these ask before firing and neither has an undo.
  async function deleteProject(id) {
    const p = state.projects.find(x => x.id === id); if (!p) return; clearErr();
    if (!confirm(`[${p.vessel_name}] ${p.title} 프로젝트를 삭제할까요?\n\n보고서 ${p.report_count||0}일치와 첨부파일이 모두 함께 지워지고 되돌릴 수 없습니다.`)) return;
    await api(`/api/dock-daily/projects/${id}`, {...json({confirm:'delete-project'}), method:'DELETE'});
    // Only the open project's editor state is dropped; deleting another one
    // leaves unsaved edits alone, so state.dirty is not touched there.
    if (state.project?.id === id) {
      state.project = null; state.report = null; state.reports = []; state.dirty = false;
      $('#dd-project-tools').style.display = 'none'; $('#dd-report').classList.remove('show'); $('#dd-empty').style.display = 'block';
    }
    await loadProjects();
  }
  async function deleteReport(id) {
    const r = state.reports.find(x => x.id === id); if (!r || !state.project) return; clearErr();
    // A finalized report is edit-locked, so the server demands a token for it
    // rather than refusing outright -- otherwise a mistaken 확정 is unfixable.
    const final = r.status === 'final';
    if (!confirm(`${r.report_date} 보고서를 삭제할까요?${final?'\n\n확정된 보고서입니다. 지우면 되돌릴 수 없습니다.':''}`)) return;
    await api(`/api/dock-daily/reports/${id}`, {...json(final?{confirm:'delete-final'}:{}), method:'DELETE'});
    const openWasDeleted = state.report?.id === id;
    if (openWasDeleted) { state.report = null; state.dirty = false; $('#dd-report').classList.remove('show'); $('#dd-empty').style.display = 'block'; }
    state.reports = await api(`/api/dock-daily/projects/${state.project.id}/reports`);
    await loadProjects();                       // the day count on the row changed
    renderReportDates();
    if (openWasDeleted && state.reports.length) await selectReport(state.reports[0].id);
  }
  async function selectProject(id) {
    clearErr(); state.project = state.projects.find(p => p.id === id) || null; if (!state.project) return;
    state.report = null; state.reports = await api(`/api/dock-daily/projects/${id}/reports`); await loadProjects();
    $('#dd-project-tools').style.display = 'block'; $('#dd-generate-date').value = today(); renderReportDates();
    const specials = (state.project.sections||[]).filter(s => s.kind === 'special');
    $('#dd-special-tools').innerHTML = specials.length ? '<b>Special 항목</b>'+specials.map(s => `<label style="display:block;margin-top:7px"><input type="checkbox" class="dd-special-toggle" data-key="${esc(s.section_key)}" ${s.enabled?'checked':''}> ${esc(s.label)}</label>`).join('') : '<span class="dd-muted">Special 항목 없음</span>';
    document.querySelectorAll('.dd-special-toggle').forEach(t => t.onchange = () => toggleSpecial(t.dataset.key, t.checked));
    $('#dd-empty').style.display='block'; $('#dd-report').classList.remove('show'); if (state.reports.length) await selectReport(state.reports[0].id);
  }
  function ensureSectionEditors() {
    for (const s of (state.report.sections||[]).filter(x => x.enabled)) {
      if (!(state.report.blocks||[]).some(b => !b._delete && b.section_key === s.section_key)) {
        state.report.blocks.push({id:0,_key:state.tempId--,section_key:s.section_key,block_type:'paragraph',content:{body:''},sort_order:0,origin:'manual',manual_override:1,_new:true});
      }
    }
  }
  async function selectReport(id) {
    clearErr(); state.report = await api(`/api/dock-daily/reports/${id}`); state.dirty=false; ensureSectionEditors();
    $('#dd-empty').style.display='none'; $('#dd-report').classList.add('show');
    $('#dd-report-title').textContent=`${state.report.vessel_name} · 입거 Daily Report`;
    $('#dd-report-meta').textContent=`${state.report.report_date} · ${state.report.status} · revision ${state.report.revision}`;
    renderReportDates(); renderItinerary(); renderSections(); renderAttachments();
    const locked = state.report.status === 'final'; ['#dd-save','#dd-final','#dd-attach'].forEach(s => $(s).disabled=locked);
  }
  function renderItinerary() {
    const values=[['berthing_date','BERTHING'],['dock_in_date','DRY DOCK IN'],['dock_out_date','DRY DOCK OUT'],['departure_date','DEPARTURE']];
    $('#dd-itinerary').innerHTML=values.map(([key,label])=>`<label>${label}<input class="dd-input dd-itinerary-date" data-key="${key}" type="date" value="${esc(state.report[key]||'')}" ${state.report.status==='final'?'disabled':''}></label>`).join('');
    document.querySelectorAll('.dd-itinerary-date').forEach(i=>i.onchange=()=>{state.dirty=true;});
  }
  function blockText(b){const c=b.content||{};return c.title||c.body||c.text||(b.block_type==='table'?JSON.stringify(c.rows||[]):'');}
  // Cards carry their own "1) " numbering so what the supervisor types is what
  // the Outlook mail shows. The renderers strip any stored number before
  // applying their own, so a renumbered card never double-numbers.
  const NUM=window.DockDailyNumbering;
  function applyNumbering(ta,result){
    if(ta.value!==result.value)ta.value=result.value;
    ta.selectionStart=ta.selectionEnd=result.caret;
  }
  function commitItems(ta){
    const b=findBlock(ta.dataset.key); if(!b||blockText(b)===ta.value)return;
    b.content={...(b.content||{}),body:ta.value}; b.block_type='paragraph'; b._edit=true; state.dirty=true;
  }
  function normalizeItems(ta,keepCaretLine){
    applyNumbering(ta,NUM.renumber(ta.value,ta.selectionStart,keepCaretLine));
    commitItems(ta);
  }
  function bindItemNumbering(ta){
    let composing=false;
    // Hangul/IME input must never be renumbered mid-composition, so the empty
    // card gets its "1) " on focus instead of on the first keystroke.
    ta.onfocus=()=>{if(!ta.value.trim()){ta.value='1) ';ta.selectionStart=ta.selectionEnd=3;}};
    ta.oncompositionstart=()=>{composing=true;};
    // A composed run can finish on a line that still has no number (legacy card
    // or a paste), so normalize once the IME hands the text back.
    ta.oncompositionend=()=>{composing=false;if(NUM.needsNumbering(ta.value))normalizeItems(ta,true);else commitItems(ta);};
    ta.onkeydown=e=>{
      if(e.key!=='Enter'||e.shiftKey||e.isComposing||composing)return;
      e.preventDefault();
      applyNumbering(ta,NUM.breakLine(ta.value,ta.selectionStart,ta.selectionEnd));
      commitItems(ta);
    };
    ta.oninput=()=>{
      // Legacy cards saved without numbers get them as soon as they are edited;
      // an already-numbered first line is left alone so mid-line typing is safe.
      if(!composing&&NUM.needsNumbering(ta.value))normalizeItems(ta,true);
      else commitItems(ta);
    };
    // Leaving the card drops any number left on an empty line.
    ta.onblur=()=>normalizeItems(ta,false);
  }
  function renderSections() {
    const locked=state.report.status==='final'; const blocks=state.report.blocks||[];
    $('#dd-sections').innerHTML=(state.report.sections||[]).filter(s=>s.enabled).map(s=>{
      const bs=blocks.filter(b=>b.section_key===s.section_key&&!b._delete);
      return `<div class="dd-card dd-section" data-section="${esc(s.section_key)}"><div class="dd-section-head"><h3>${esc(s.label)}</h3><span class="dd-muted">엔터를 누르면 1) 2) 번호가 붙습니다</span></div>${bs.map((b,i)=>{const key=b._key??b.id;
      // Provenance badges only carry meaning for auto-collected blocks; hand
      // written cards showed a permanent "수동" pair that said nothing.
      const badges=b.origin==='dock_auto'?`<span class="dd-badge auto">자동수집</span>${b.manual_override?'<span class="dd-badge">수동 수정 보호</span>':''}`:'';
      const del=bs.length>1?`<button class="dd-btn alt delete-inline" type="button" data-key="${key}" ${locked?'disabled':''}>삭제</button>`:'';
      const meta=(badges||del)?`<div class="dd-block-meta"><span>${badges}</span>${del}</div>`:'';
      return `<div class="dd-block-editor">${meta}<textarea class="dd-section-edit" data-key="${key}" placeholder="${esc(s.label)} 내용을 입력하세요" ${locked?'disabled':''}>${esc(blockText(b))}</textarea></div>`}).join('')}</div>`;
    }).join('');
    document.querySelectorAll('.dd-section-edit').forEach(bindItemNumbering);
    document.querySelectorAll('.delete-inline').forEach(btn=>btn.onclick=()=>{const b=findBlock(btn.dataset.key);if(!b)return;if(b._new)state.report.blocks=state.report.blocks.filter(x=>x!==b);else b._delete=true;state.dirty=true;ensureSectionEditors();renderSections();});
  }
  function findBlock(key){return (state.report.blocks||[]).find(b=>String(b._key??b.id)===String(key));}
  function canLeaveDraft(){return !state.dirty||confirm('저장되지 않은 수정사항이 있습니다. 저장하지 않고 이동할까요?');}
  function renderAttachments(){
    const ats=state.report.attachments||[];
    $('#dd-attachments').innerHTML=ats.length?ats.map(a=>`<button class="dd-attachment" type="button" data-attachment="${a.id}" data-name="${esc(a.original_name)}"><b>${esc(a.original_name)}</b><span>${esc(a.mime_type)} · ${(a.size/1024).toFixed(1)} KB · 미리보기</span></button>`).join(''):'<p class="dd-muted">등록된 첨부파일이 없습니다.</p>';
    document.querySelectorAll('[data-attachment]').forEach(b=>b.onclick=()=>openFilePreview(+b.dataset.attachment,b.dataset.name));
  }
  async function save() {
    if(!state.report)return; clearErr();
    const itinerary={}; document.querySelectorAll('.dd-itinerary-date').forEach(i=>itinerary[i.dataset.key]=i.value||null);
    if(Object.keys(itinerary).some(k=>(state.project[k]||null)!==itinerary[k])){
      const updated=await api(`/api/dock-daily/projects/${state.project.id}`,{...json(itinerary),method:'PATCH'});
      state.project=updated; state.projects=state.projects.map(p=>p.id===updated.id?updated:p);
    }
    const operations=[];
    state.report.blocks.filter(b=>b._delete).forEach(b=>operations.push({op:'delete',id:b.id}));
    state.report.blocks.filter(b=>!b._delete&&(b._new||b._edit)&&(!b._new||blockText(b).trim())).forEach(b=>operations.push({op:'upsert',id:b._new?undefined:b.id,section_key:b.section_key,block_type:'paragraph',content:{body:blockText(b)},sort_order:b.sort_order||0}));
    state.report=await api(`/api/dock-daily/reports/${state.report.id}`,{...json({revision:state.report.revision,operations}),method:'PUT'}); state.dirty=false; ensureSectionEditors();
    $('#dd-report-meta').textContent=`${state.report.report_date} · ${state.report.status} · revision ${state.report.revision}`; renderItinerary(); renderSections(); renderAttachments();
  }
  async function toggleSpecial(key,enabled){const updated=await api(`/api/dock-daily/projects/${state.project.id}`,{...json({sections:[{section_key:key,enabled}]}),method:'PATCH'});state.project=updated;state.projects=state.projects.map(p=>p.id===updated.id?updated:p);if(state.report){state.report.sections=updated.sections;ensureSectionEditors();renderSections();}}

  const previewModal=$('#dd-preview-modal');
  function closePreview(){previewModal.hidden=true;document.body.style.overflow='';state.preview=null;}
  async function openPreview(kind){
    if(state.dirty)await save(); const v=await api(`/api/dock-daily/reports/${state.report.id}/${kind}-preview`); state.preview={kind,data:v};
    $('#dd-preview-title').textContent=kind==='email'?'이메일 미리보기':'SVMS 미리보기'; $('#dd-preview-status').textContent='';
    $('#dd-copy-all').hidden=kind!=='email'; $('#dd-svms-push').hidden=kind!=='svms';
    if(kind==='email') $('#dd-preview-content').innerHTML=`<div class="dd-email-subject"><b>제목</b><br>${esc(v.subject)}</div><div class="dd-email-html">${v.html}</div>`;
    else {const f=v.fields||{},push=$('#dd-svms-push');push.disabled=!v.publishable;push.title=v.publishable?'미리보기 내용을 SVMS에 반영':'SVMS 저장 계약과 byte limit 검증 전에는 반영할 수 없습니다.';$('#dd-preview-status').textContent=v.publishable?'':'실제 푸싱은 SVMS 저장 계약 검증 후 활성화됩니다.';$('#dd-preview-content').innerHTML=`<p class="dd-modal-intro"><b>${v.publishable?'SVMS 반영 준비 완료':'Preview only 안전게이트'}</b><br>DK_CD와 byte limit 계약이 모두 확인되어야 실제 반영됩니다.</p><div class="dd-svms-grid"><b>DK_CD</b><pre>${esc(f.DK_CD||'')}</pre><b>DR_DT</b><pre>${esc(f.DR_DT||'')}</pre><b>Shipyard</b><pre>${esc(f.RMK_SYD||'')}</pre><b>Vendor</b><pre>${esc(f.RMK_VNDR||'')}</pre><b>Remark</b><pre>${esc(f.RMK||'')}</pre></div>`;}
    previewModal.hidden=false;document.body.style.overflow='hidden';
  }
  async function copyEmail(){
    const v=state.preview?.data;if(!v)return;const plain=`제목: ${v.subject}\n\n${v.text}`;
    try{if(window.ClipboardItem&&navigator.clipboard?.write){await navigator.clipboard.write([new ClipboardItem({'text/plain':new Blob([plain],{type:'text/plain'}),'text/html':new Blob([`<div style="font-family:Arial,Helvetica,sans-serif;font-size:11pt;line-height:1.5;color:#222"><p style="margin:0"><span style="font-family:Arial,Helvetica,sans-serif;font-size:11pt"><b>제목: ${esc(v.subject)}</b></span></p><p style="margin:0;line-height:1.5"><span style="font-family:Arial,Helvetica,sans-serif;font-size:11pt">&nbsp;</span></p></div>${v.html}`],{type:'text/html'})})]);}else await navigator.clipboard.writeText(plain);$('#dd-preview-status').textContent='전체 내용이 복사되었습니다.';}catch(e){$('#dd-preview-status').textContent='복사 실패: '+e.message;}
  }
  async function pushSvms(){
    if(!confirm('현재 미리보기 내용으로 SVMS 입거 Daily Report에 반영할까요?'))return;
    const btn=$('#dd-svms-push');btn.disabled=true;$('#dd-preview-status').textContent='SVMS 반영 요청 중…';
    try{const v=await api(`/api/dock-daily/reports/${state.report.id}/svms-publish`,{...json({confirmation:'user_preview_approved'}),method:'POST'});$('#dd-preview-status').textContent=v.message||'SVMS 반영 완료';}
    catch(e){$('#dd-preview-status').textContent=e.message;}finally{btn.disabled=false;}
  }
  function openFilePreview(id,name){$('#dd-file-title').textContent=name;$('#dd-file-frame').src=`/api/dock-daily/attachments/${id}/preview`;$('#dd-file-modal').hidden=false;document.body.style.overflow='hidden';}
  function closeFilePreview(){$('#dd-file-modal').hidden=true;$('#dd-file-frame').src='about:blank';document.body.style.overflow='';}
  async function uploadOne(rid,file){const fd=new FormData();fd.append('file',file);const r=await fetch(`/api/dock-daily/reports/${rid}/attachments`,{method:'POST',body:fd});const body=await r.json().catch(()=>({}));if(!r.ok)throw new Error(body.error||`업로드 실패 (${r.status})`);return body;}
  let uploading=false;
  // Files go up one at a time against a report id pinned before the first
  // post. Re-reading state.report per file would scatter a batch across two
  // reports if the user switched in the middle, and parallel posts would race
  // the reload and drop a row from the list that the server actually kept.
  async function uploadFiles(files){
    const list=[...(files||[])].filter(Boolean); if(!list.length||!state.report)return;
    if(uploading){$('#dd-upload-status').textContent='앞의 업로드가 끝난 뒤 다시 놓아주세요.';return;}
    const rid=state.report.id, ul=$('#dd-upload-list'); let ok=0; uploading=true;
    try{
      $('#dd-upload-status').textContent=`0/${list.length} 업로드 중…`;
      for(const file of list){
        const li=document.createElement('li');
        li.innerHTML=`<b>${esc(file.name)}</b><span>업로드 중…</span>`; ul.appendChild(li);
        const status=li.querySelector('span');
        try{await uploadOne(rid,file); status.textContent='완료'; li.className='ok'; ok++;}
        catch(e){status.textContent=e.message||String(e); li.className='fail';}
        $('#dd-upload-status').textContent=`${ok}/${list.length} 등록됨`;
      }
      const tail=ok===list.length?`${ok}건 모두 등록됨`:`${list.length}건 중 ${ok}건 등록됨`;
      // Reload only when the same report is still open with nothing unsaved.
      // selectReport() replaces state.report wholesale, so refreshing over
      // edits typed during the upload would discard them silently.
      if(state.report?.id===rid&&!state.dirty){await selectReport(rid);$('#dd-upload-status').textContent=tail;}
      else $('#dd-upload-status').textContent=`${tail} · 목록은 저장 후 새로 열면 반영됩니다.`;
    } finally { uploading=false; }
  }
  const uploadModal=$('#dd-upload-modal'), dropzone=$('#dd-dropzone');
  async function openUploadModal(){
    if(!state.report||state.report.status==='final')return;
    // Same contract as the previews: pending edits are saved first, because the
    // upload reloads the report and would otherwise discard them.
    if(state.dirty)await save();
    $('#dd-upload-list').innerHTML=''; $('#dd-upload-status').textContent='';
    uploadModal.hidden=false; document.body.style.overflow='hidden'; dropzone.focus();
  }
  function closeUploadModal(){uploadModal.hidden=true;document.body.style.overflow='';}

  $('#dd-save').onclick=()=>save().catch(err); $('#dd-email').onclick=()=>openPreview('email').catch(err); $('#dd-svms').onclick=()=>openPreview('svms').catch(err);
  $('#dd-final').onclick=async()=>{if(!state.report||!confirm('이 보고서를 확정하면 더 이상 수정할 수 없습니다. 확정할까요?'))return;try{if(state.dirty)await save();state.report=await api(`/api/dock-daily/reports/${state.report.id}`,{...json({revision:state.report.revision,status:'final',operations:[]}),method:'PUT'});await selectReport(state.report.id);}catch(e){err(e);}};
  $('#dd-generate').onclick=async()=>{if(!state.project)return;const report_date=$('#dd-generate-date').value;if(!report_date)return err(new Error('보고서 일자를 선택하세요.'));try{const report=await api(`/api/dock-daily/projects/${state.project.id}/reports/generate`,{...json({report_date}),method:'POST'});state.reports=await api(`/api/dock-daily/projects/${state.project.id}/reports`);await selectReport(report.id);}catch(e){err(e);}};
  $('#dd-attach').onclick=()=>openUploadModal().catch(err);
  $('#dd-upload-close').onclick=closeUploadModal; $('#dd-upload-done').onclick=closeUploadModal;
  $('#dd-file-input').onchange=async e=>{const files=[...e.target.files];e.target.value='';try{await uploadFiles(files);}catch(x){err(x);}};
  dropzone.onclick=()=>$('#dd-file-input').click();
  dropzone.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();$('#dd-file-input').click();}};
  ['dragenter','dragover'].forEach(t=>dropzone.addEventListener(t,e=>{e.preventDefault();dropzone.classList.add('over');}));
  ['dragleave','dragend','drop'].forEach(t=>dropzone.addEventListener(t,()=>dropzone.classList.remove('over')));
  dropzone.addEventListener('drop',e=>{e.preventDefault();uploadFiles(e.dataTransfer?.files).catch(err);});
  // Without this the browser navigates away to the dropped file when the user
  // misses the zone, which would silently lose unsaved edits.
  ['dragover','drop'].forEach(t=>window.addEventListener(t,e=>{if(!dropzone.contains(e.target))e.preventDefault();}));
  $('#dd-preview-close').onclick=closePreview;$('#dd-preview-done').onclick=closePreview;$('#dd-copy-all').onclick=copyEmail;$('#dd-svms-push').onclick=pushSvms;$('#dd-file-close').onclick=closeFilePreview;

  const projectModal=$('#dd-project-modal'),projectForm=$('#dd-project-form'),projectError=$('#ddp-error'),autoToggle=$('#ddp-auto');
  function setAutoFields(){const enabled=autoToggle.checked;$('#ddp-auto-fields').hidden=!enabled;['#ddp-active-from','#ddp-active-to','#ddp-source-ids'].forEach(s=>$(s).required=enabled);if(enabled&&!$('#ddp-active-from').value){$('#ddp-active-from').value=today();$('#ddp-active-to').value=today();}}
  async function openProjectModal(){projectForm.reset();projectError.textContent='';setAutoFields();try{if(!state.vessels.length)state.vessels=await api('/api/vessels');$('#ddp-vessel').innerHTML='<option value="">활성 선박을 선택하세요</option>'+state.vessels.map(v=>`<option value="${v.id}">${esc(v.name)}${v.vsl_cd?` · ${esc(v.vsl_cd)}`:''}</option>`).join('');projectModal.hidden=false;document.body.style.overflow='hidden';$('#ddp-vessel').focus();}catch(e){err(e);}}
  function closeProjectModal(){projectModal.hidden=true;document.body.style.overflow='';}
  autoToggle.onchange=setAutoFields;$('#dd-new-project').onclick=openProjectModal;$('#dd-project-close').onclick=closeProjectModal;$('#dd-project-cancel').onclick=closeProjectModal;
  document.addEventListener('keydown',e=>{if(e.key!=='Escape')return;if(!projectModal.hidden)closeProjectModal();else if(!previewModal.hidden)closePreview();else if(!$('#dd-file-modal').hidden)closeFilePreview();else if(!uploadModal.hidden)closeUploadModal();});
  projectForm.onsubmit=async e=>{e.preventDefault();projectError.textContent='';if(!projectForm.reportValidity())return;const auto_generate=autoToggle.checked,active_from=$('#ddp-active-from').value||null,active_to=$('#ddp-active-to').value||null,sourceIds=$('#ddp-source-ids').value.split(',').map(x=>x.trim()).filter(Boolean);if(auto_generate&&active_from>active_to)return projectError.textContent='자동작성 종료일은 시작일보다 빠를 수 없습니다.';if(auto_generate&&sourceIds.some(x=>!/^v_[A-Za-z0-9][A-Za-z0-9_.:-]*$/.test(x)))return projectError.textContent='Dock Manager 원천 ID는 모두 v_로 시작해야 합니다.';const button=$('#dd-project-create');button.disabled=true;button.textContent='생성 중…';try{const created=await api('/api/dock-daily/projects',{...json({vessel_id:Number($('#ddp-vessel').value),title:$('#ddp-title').value.trim(),berthing_date:$('#ddp-berthing').value||null,dock_in_date:$('#ddp-dock-in').value||null,dock_out_date:$('#ddp-dock-out').value||null,departure_date:$('#ddp-departure').value||null,active_from:auto_generate?active_from:null,active_to:auto_generate?active_to:null,auto_generate,dock_manager_project_ids:auto_generate?sourceIds:[],svms_dk_cd:$('#ddp-svms-dk').value.trim()||null,special_sections:$('#ddp-egcs').checked?[{section_key:'egcs',label:'EGCS Retrofit',enabled:true}]:[]}),method:'POST'});closeProjectModal();await loadProjects();await selectProject(created.id);}catch(error){projectError.textContent=error.message||String(error);}finally{button.disabled=false;button.textContent='프로젝트 생성';}};
  window.addEventListener('beforeunload',event=>{if(state.dirty){event.preventDefault();event.returnValue='';}});
  loadProjects().catch(err);
})();
