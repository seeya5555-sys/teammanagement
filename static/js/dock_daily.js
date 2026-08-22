(() => {
  'use strict';
  const $ = s => document.querySelector(s);
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const state = {projects:[], project:null, reports:[], report:null, dirty:false, vessels:[], tempId:-1, preview:null};
  async function api(url, options) {
    const r = await fetch(url, options); const body = await r.json().catch(() => ({}));
    // 상태코드와 서버가 준 `code` 를 에러에 실어 보낸다. 문구만 던지면 호출부가
    // `error` 문자열로 갈라 읽어야 하고, 그러면 서버 문구 한 글자만 바뀌어도
    // 조용히 엉뚱한 안내를 낸다(→ conflictText).
    if (!r.ok) {
      const e = new Error(body.error || `요청 실패 (${r.status})`);
      e.status = r.status; e.code = body.code; e.body = body; throw e;
    }
    return body;
  }
  const json = v => ({headers:{'Content-Type':'application/json'}, body:JSON.stringify(v)});
  const err = e => { $('#dd-error').textContent = e.message || String(e); };
  const notice = m => { $('#dd-notice').textContent = m || ''; };
  const clearErr = () => { $('#dd-error').textContent = ''; notice(''); };
  // PUT /reports/<id> 의 409 는 revision 충돌·확정잠금·날짜중복 셋이고 사람이 할 일이
  // 서로 다르다. 확정잠금에 "최신본을 다시 불러오세요" 를 띄우면 형은 해결되지 않는
  // 동작을 반복한다 — 해법은 확정 취소다. 아이폰 앱과 같은 분기다.
  const CONFLICT = {
    date_taken: '그 날짜에는 이미 보고서가 있습니다. 다른 날짜를 고르세요.',
    final_locked: '확정된 보고서는 수정할 수 없습니다. 확정을 취소한 뒤 고치세요.',
    revision_conflict: '다른 곳에서 먼저 저장했습니다. 보고서를 다시 열어 확인한 뒤 고치세요.',
  };
  function conflictText(e) {
    // 409 일 때만 `code` 로 갈라 읽는다. 다른 상태코드가 같은 이름의 `code` 를 실어보내면
    // 엉뚱한 해법을 안내하게 된다(올마이트 지적 2026-08-21).
    if (!e || e.status !== 409) return (e && e.message) || String(e);
    if (CONFLICT[e.code]) return CONFLICT[e.code];
    // `code` 를 안 주는 구버전 응답 폴백: 날짜 중복만은 충돌 상대 id 로 알아본다.
    if (e.body && e.body.conflicting_report_id) return CONFLICT.date_taken;
    return e.message || String(e);
  }
  const today = () => new Date().toLocaleDateString('en-CA');

  async function loadProjects() {
    state.projects = await api('/api/dock-daily/projects');
    $('#dd-project-list').innerHTML = state.projects.length ? state.projects.map(p =>
      `<div class="dd-list-row"><button data-project="${p.id}" class="${state.project?.id===p.id?'active':''}"><b>${esc(p.vessel_name)}</b><br><span class="dd-muted">${esc(p.title)} · ${p.report_count||0}일</span></button><button class="dd-list-del" type="button" data-del-project="${p.id}" title="프로젝트 삭제" aria-label="${esc(p.vessel_name)} 프로젝트 삭제">삭제</button></div>`).join('') : '<p class="dd-muted">등록된 프로젝트가 없습니다.</p>';
    document.querySelectorAll('[data-project]').forEach(b => b.onclick = () => {if(canLeaveDraft())selectProject(+b.dataset.project);});
    document.querySelectorAll('[data-del-project]').forEach(b => b.onclick = () => once(b, () => deleteProject(+b.dataset.delProject)));
  }
  // 필터 규칙 정본은 static/js/dock_daily_filter.js 다(실행형 테스트로 잠긴다).
  const FILTER = window.DockDailyReportFilter;
  function visibleReports() {
    // 서버가 준 날짜 역순을 그대로 쓴다 — 다시 정렬하면 앱과 순서가 갈린다.
    return FILTER.apply(state.reports, $('#dd-report-search').value, $('#dd-report-status').value);
  }
  function renderReportDates() {
    const sel = $('#dd-report-select'), rows = visibleReports(), open = state.report?.id ?? null;
    // 열린 보고서가 필터 밖으로 밀려나면 드롭다운은 첫 행을 선택한 것처럼 보인다.
    // 그러면 화면 본문과 선택값이 어긋나므로, 열린 일자를 맨 위에 명시해 붙인다.
    const stray = open && !rows.some(r => r.id === open) ? state.reports.find(r => r.id === open) : null;
    // SVMS 꼬리는 넘어간 일자에만 붙는다(목록 응답이 `svms_sync_status` 를 함께 준다) —
    // 어느 일자가 이미 SVMS 로 갔는지 보고서를 하나씩 열지 않고 알아야 한다.
    const svms = r => { const s = window.DockDailySVMS.listSuffix(r.svms_sync_status); return s ? ` · ${s}` : ''; };
    const option = (r, tail) => `<option value="${esc(r.id)}"${r.id===open?' selected':''}>${esc(r.report_date)} · ${esc(r.status)}${esc(svms(r))}${tail||''}</option>`;
    sel.innerHTML = (stray ? [option(stray, ' · 필터 밖(열림)')] : [])
      .concat(rows.map(r => option(r)))
      .join('') || '<option value="">해당 조건의 보고서가 없습니다</option>';
    sel.disabled = !rows.length && !stray;
    $('#dd-report-del').disabled = !open;
    $('#dd-report-count').textContent = !state.reports.length ? '생성된 일자가 없습니다.'
      : `${rows.length}/${state.reports.length}건`;
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
  // 🔴 프로젝트 전환에도 순서 방어가 필요하다(`selectSeq` 는 일자 전환만 막았다).
  // A→B 를 빠르게 누르면 A 의 목록 응답이 나중에 도착해, 사이드바는 B 를 칠했는데
  // 본문·드롭다운은 A 가 된다 -- 그 상태에서 ＋ 섹션을 누르면 B 에 만들면서 화면은
  // A 의 보고서다.
  let projectSeq = 0;
  async function selectProject(id) {
    const pseq = ++projectSeq;
    clearErr(); state.project = state.projects.find(p => p.id === id) || null; if (!state.project) return;
    state.report = null; const reports = await api(`/api/dock-daily/projects/${id}/reports`);
    if (pseq !== projectSeq) return;            // 형이 그 사이 다른 프로젝트를 골랐다
    state.reports = reports; await loadProjects();
    if (pseq !== projectSeq) return;
    // 프로젝트를 바꿀 때 필터를 비운다. 안 비우면 앞 프로젝트에 맞춰둔 검색어가
    // 새 프로젝트의 일자를 전부 숨겨 "보고서가 없다" 처럼 보인다.
    $('#dd-report-search').value = ''; $('#dd-report-status').value = '';
    $('#dd-project-tools').style.display = 'block'; $('#dd-generate-date').value = today(); renderReportDates();
    renderSpecialTools();
    $('#dd-empty').style.display='block'; $('#dd-report').classList.remove('show'); if (state.reports.length) await selectReport(state.reports[0].id);
  }
  // 표는 다른 카드의 하위항목이 아니라 **제목을 가진 자기 섹션**이다(형 지시 2026-08-21).
  // 그 섹션이 곧 special 섹션이므로 새 저장소도, 새 라우트도 필요 없다 — 메일에서도
  // 다른 섹션과 같은 `N. 제목` 머리글을 받는다.
  //
  // 🔴 이 markup 은 여기 한 곳에서만 만든다. 전엔 `selectProject` 와 `addSection` 이
  // 같은 HTML 을 각자 들고 있어서, 행에 버튼을 하나 더 다는 순간 한쪽만 고치면
  // "섹션을 추가하고 나면 삭제 버튼이 사라지는" 화면이 된다.
  function renderSpecialTools() {
    const specials = (state.project?.sections||[]).filter(s => s.kind === 'special');
    const rows = specials.length
      ? '<b>Special 항목</b>' + specials.map(s =>
          `<div class="dd-list-row" style="border-bottom:0;margin-top:7px"><label style="flex:1;min-width:0"><input type="checkbox" class="dd-special-toggle" data-key="${esc(s.section_key)}" ${s.enabled?'checked':''}> ${esc(s.label||s.section_key)}</label>`
          + `<button class="dd-list-del" type="button" data-del-section="${esc(s.section_key)}" title="이 섹션을 아주 삭제" aria-label="${esc(s.label||s.section_key)} 섹션 삭제">삭제</button></div>`).join('')
      : '<span class="dd-muted">Special 항목 없음</span>';
    $('#dd-special-tools').innerHTML = rows
      + '<input class="dd-input" id="dd-section-label" placeholder="새 섹션 제목 (예: 비용 정산표)" maxlength="60" style="margin:9px 0 7px">'
      + '<div class="dd-row"><button class="dd-btn alt" id="dd-section-add" type="button">＋ 섹션</button><button class="dd-btn alt" id="dd-section-add-table" type="button">＋ 표 섹션</button></div>'
      + '<p class="dd-muted dd-block-note">섹션은 이 프로젝트의 모든 일자에 생깁니다. 비어 있는 날은 메일에 NIL 로 나갑니다. 잠시 감추려면 체크를 해제하고, 아주 지우려면 삭제를 누르세요. <b>＋ 표 섹션</b>은 제목을 가진 빈 표 카드를 열린 일자에 만듭니다(앱과 같은 기능).</p>';
    // 🔴 실패를 삼키지 않는다. 전엔 `.catch` 가 없어서 PATCH 가 500 이 나면 unhandled
    // rejection 으로 조용히 끝나고, 체크박스만 바뀐 채로 남아 화면이 서버와 반대를
    // 말했다(다시 열기 전까지 형은 감춘 줄 안다). 실패하면 체크를 되돌려 놓는다.
    document.querySelectorAll('.dd-special-toggle').forEach(t => t.onchange = () => {
      const wanted = t.checked;
      toggleSpecial(t.dataset.key, wanted).catch(e => { t.checked = !wanted; err(e); });
    });
    document.querySelectorAll('[data-del-section]').forEach(b => b.onclick = () => once(b, () => deleteSection(b.dataset.delSection)));
    // ＋ 섹션도 `once()` 로 막는다(옆의 ＋ 표 섹션은 이미 막혀 있다). 두 번 누르면 서버가
    // UNIQUE 충돌을 다음 번호로 피해 가므로 **같은 제목의 섹션이 두 개** 생기고, 이
    // 프로젝트의 모든 일자와 메일에 두 번 나간다.
    $('#dd-section-add').onclick = () => once($('#dd-section-add'), () => addSection($('#dd-section-label').value));
    $('#dd-section-add-table').onclick = () => once($('#dd-section-add-table'), () => addSection($('#dd-section-label').value, true));
  }
  function ensureSectionEditors() {
    // 🔴 확정본에는 초안 칸을 깔지 않는다(앱 `needsTextDraft` 는 `guard !readOnly`).
    // 깔면 잠긴 보고서 안에 회색 입력칸이 뜨는데 앱에는 없다 -- 파리티가 갈리고,
    // 못 쓰는 칸이 "여기 쓸 수 있다" 로 읽힌다.
    if (state.report.status === 'final') return;
    for (const s of (state.report.sections||[]).filter(x => x.enabled)) {
      // 🔴 표를 담은 special 섹션은 **표만** 있는 카드다(형 지시 2026-08-22) — 글 칸을
      // 끼워 넣으면 형이 안 쓴 문단이 카드마다 하나씩 붙는다. 앱과 같은 값 기준 판정
      // (`DockDailySectionEditing.needsTextDraft`).
      const own = (state.report.blocks||[]).filter(b => !b._delete && b.section_key === s.section_key);
      if (s.kind === 'special' && own.some(b => b.block_type === 'table')) {
        // 🔴 방금 표를 넣은 섹션에는 조금 전 깔아둔 빈 글 초안이 남아 있다. 저장되지는
        // 않지만(`_new` + 빈 글은 안 올린다) 화면에는 빈 문단이 그대로 보여서, 형이
        // 없애라고 한 "표 카드 속 일반 입력칸" 이 되살아난 것처럼 읽힌다.
        state.report.blocks = (state.report.blocks||[]).filter(
          b => !(b._new && b.section_key === s.section_key && isTextBlock(b) && !blockText(b).trim()));
        continue;
      }
      if (!own.some(b => isTextBlock(b))) {
        state.report.blocks.push({id:0,_key:state.tempId--,section_key:s.section_key,block_type:'paragraph',content:{body:''},sort_order:0,origin:'manual',manual_override:1,_new:true});
      }
    }
  }
  // 드롭다운을 빠르게 여러 번 바꾸면 응답이 역전될 수 있다. 늦게 온 이전 요청이
  // 화면을 덮으면 목록 선택값과 본문이 어긋나므로, 마지막 요청만 화면에 반영한다
  // (올마이트 지적 2026-08-21).
  let selectSeq = 0;
  async function selectReport(id) {
    const seq = ++selectSeq;
    clearErr(); const loaded = await api(`/api/dock-daily/reports/${id}`);
    if (seq !== selectSeq) return;              // 형이 그 사이 다른 일자를 골랐다
    state.report = loaded; state.dirty=false; ensureSectionEditors();
    $('#dd-empty').style.display='none'; $('#dd-report').classList.add('show');
    $('#dd-report-title').textContent=`${state.report.vessel_name} · 입거 Daily Report`;
    $('#dd-report-meta').textContent=`${state.report.report_date} · ${state.report.status} · revision ${state.report.revision}`;
    renderSvmsState(); renderReportDates(); renderItinerary(); renderSections(); renderAttachments();
    const locked = state.report.status === 'final'; ['#dd-save','#dd-attach'].forEach(s => $(s).disabled=locked);
    // 확정 버튼만은 잠긴 상태에서도 살아있어야 한다 — 잠금을 여는 유일한 통로다.
    // 여기서 disabled 를 걸면 확정된 보고서는 영구히 잠긴다.
    const finalBtn=$('#dd-final'); finalBtn.disabled=false;
    finalBtn.textContent=locked?'확정취소':'확정';
    // '취소' 한 단어는 옆의 '저장' 과 붙어 "편집 취소" 로 읽힌다.
    finalBtn.classList.toggle('warn',locked); finalBtn.classList.toggle('alt',!locked);
  }
  /* SVMS 반영 상태 줄. 확정 전에는 숨긴다 — 상신 자체가 확정본만 되므로 편집 중인
   * 보고서에 "SVMS 미반영" 을 달면 결함처럼 읽힌다(앱과 동일 규칙).
   *
   * 🔴 상신은 맥 러너가 나중에 처리하는 비동기 경로다. 이 줄이 없으면 형은 상신을 누른 뒤
   *    결과(반영됨 / 본문만 / 실패 / 불명)를 웹에서 영구히 알 수 없다. */
  function renderSvmsState(){
    const box=$('#dd-svms-state'); if(!box) return;
    const r=state.report;
    if(!r||r.status!=='final'){box.hidden=true;box.innerHTML='';return;}
    const S=window.DockDailySVMS, raw=r.svms_sync_status;
    const seq=String(r.svms_dk_seq||'').trim(), note=S.guidance(raw);
    const err=String(r.svms_error||'').trim();
    // DK_SEQ 는 SVMS 에서 그 행을 찾는 키다. 반영됐다는 말만으로는 어느 행인지 못 찾는다.
    let html=`<span class="dd-badge dd-badge-${S.tone(raw)}">${esc(S.title(raw))}</span>`;
    if(seq) html+=`<span class="dd-muted">DK_SEQ ${esc(seq)}</span>`;
    // 결과는 러너가 나중에 써넣으므로 형이 직접 당겨볼 수단이 필요하다.
    const pending=S.normalize(raw)==='approved'||S.normalize(raw)==='submitting';
    if(pending) html+=`<button class="dd-btn alt" id="dd-svms-refresh" type="button" title="SVMS 반영 상태 새로고침">상태 새로고침</button>`;
    if(err) html+=`<span class="dd-svms-err">${esc(err)}</span>`;
    if(note) html+=`<span class="dd-svms-note">${esc(note)}</span>`;
    // 🔴 수동 확인 출구. 없으면 `unknown`/`partial` 이 영구 고착이다(올마이트 blocking).
    const manual=S.needsManualCheck(raw);
    if(manual) html+=`<button class="dd-btn alt" id="dd-svms-saved" type="button">SVMS에 저장됨</button>`
                    +`<button class="dd-btn alt" id="dd-svms-notsaved" type="button">저장 안 됨</button>`;
    box.innerHTML=html; box.hidden=false;
    if(pending) $('#dd-svms-refresh').onclick=()=>refreshSvmsState();
    if(manual){
      $('#dd-svms-saved').onclick=()=>reconcileSvms(true);
      $('#dd-svms-notsaved').onclick=()=>reconcileSvms(false);
    }
  }
  /* 형이 직접 누른 조회다 — 실패를 조용히 넘기면 화면이 멈춘 것처럼 보이므로 알린다. */
  async function refreshSvmsState(){
    if(!state.report) return;
    const rid=state.report.id, before=window.DockDailySVMS.normalize(state.report.svms_sync_status);
    try{
      const fresh=await api(`/api/dock-daily/reports/${rid}`);
      // 응답이 늦게 온 사이 형이 다른 일자를 골랐을 수 있다. 그때 덮으면 방금 연
      // 보고서가 지난 보고서로 바뀐다.
      if(!state.report||state.report.id!==rid) return;
      state.report=fresh; ensureSectionEditors(); renderSvmsState();
      const after=window.DockDailySVMS.normalize(fresh.svms_sync_status);
      notice(after===before?`SVMS 상태 변화 없음 — ${window.DockDailySVMS.title(after)}.`
                           :`SVMS 상태: ${window.DockDailySVMS.title(after)}.`);
    }catch(e){ err(e); }
  }
  function renderItinerary() {
    const values=[['berthing_date','BERTHING'],['dock_in_date','DRY DOCK IN'],['dock_out_date','DRY DOCK OUT'],['departure_date','DEPARTURE']];
    $('#dd-itinerary').innerHTML=values.map(([key,label])=>`<label>${label}<input class="dd-input dd-itinerary-date" data-key="${key}" type="date" value="${esc(state.report[key]||'')}" ${state.report.status==='final'?'disabled':''}></label>`).join('');
    document.querySelectorAll('.dd-itinerary-date').forEach(i=>i.onchange=()=>{state.dirty=true;});
    // 🔴 일정은 프로젝트 열이고 확정본도 조인으로 읽는다 → 초안에서 저장한 일정이
    // **이미 확정된 보고서에도** 반영된다. 막으면 정상적인 출거일 연기가 영구히
    // 불가능해지므로 계약으로 두고, 몇 건이 함께 바뀌는지 굳이 세어 알린다(앱과 동일).
    const finals=state.reports.filter(r=>r.status==='final').length;
    $('#dd-itinerary-note').textContent=finals
      ?`변경 후 저장을 누르면 이 프로젝트의 모든 보고서(확정 ${finals}건 포함)에 반영됩니다.`
      :'변경 후 저장을 누르면 이 프로젝트의 모든 보고서에 반영됩니다.';
  }
  // 표·이미지는 textarea 한 칸으로 표현되지 않는다. 전엔 표를 `JSON.stringify(rows)` 로
  // 뿌리고, 그 칸을 한 번 건드리면 commitItems 가 block_type 을 'paragraph' 로 바꿔
  // **표를 JSON 문자열 문단으로 뭉갰다**. 앱에서 표를 실제로 만들 수 있게 되면서(형 지시
  // 2026-08-21) 그 경로가 곧 데이터 유실이므로, 웹에서는 글 블록만 편집한다.
  function isTextBlock(b){return b.block_type!=='table'&&b.block_type!=='image';}
  // 무엇을 서버로 올릴지. 🔴 사진 블록은 웹에서 편집기가 없으니 **절대 올리지 않는다** --
  // 화면이 읽어들인 모양(옛 한 장 카드 등)을 그대로 되쓰면 앱이 만든 계약을 덮어쓴다.
  // 표는 이제 웹에서 고칠 수 있으므로 올린다. 글 블록은 종전대로, 한 글자도 안 쓴 새
  // 초안만 걸러낸다(비어 있는 카드가 매 저장마다 늘어나지 않게).
  function savable(b){
    if(b.block_type==='image')return false;
    if(b.block_type==='table')return true;
    return !b._new||blockText(b).trim();
  }
  function blockText(b){const c=b.content||{};return c.title||c.body||c.text||'';}
  // 캡션 표시 규칙. 서버 `photo_grid.wrap` · 앱 `DockDailyImageContent.captionLabel` 과
  // 같은 글자를 내야 한다. 🔴 이미 꺾쇠가 있는 캡션은 다시 감싸지 않는다 -- 감싸면
  // `<<내용>>` 이 된다(옛 데이터에 꺾쇠가 들어있을 수 있음, 올마이트 지적).
  function captionLabel(caption){
    const text=String(caption||'').trim();
    if(!text)return '';
    if(text.length>1&&text.startsWith('<')&&text.endsWith('>'))return text;
    return `<${text}>`;
  }
  // 사진은 읽기 전용으로 보여준다(메일 렌더와 같은 모양) -- 업로드 정본이 첨부 카드라
  // 웹에서는 편집기를 두지 않는다. 표는 이제 웹에서도 고칠 수 있고(→ `tableEditor`),
  // 여기로는 **확정본**만 온다.
  function readOnlyBlock(b){
    const c=b.content||{};
    if(b.block_type==='table'){
      // 🔴 `read` 가 아니라 `grid` -- 확정본은 보여주기만 하므로 서버 `_table_grid` 와
      // 한 글자도 다르면 안 된다(빈 표에 기본 골격을 넣으면 메일에 없는 표가 화면에 뜬다).
      const g=TABLE.grid(c);
      const head=g.columns.map(v=>`<th>${esc(v)}</th>`).join('');
      const body=g.rows.map(r=>`<tr>${r.map(v=>`<td>${esc(v)}</td>`).join('')}</tr>`).join('');
      return `<table class="dd-block-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table><p class="dd-muted dd-block-note">확정된 보고서의 표는 고칠 수 없습니다. 확정을 취소하면 편집할 수 있습니다.</p>`;
    }
    // 사진 카드는 도크 리포트와 같은 `{images:[{attachment_id,caption}], columns:N}` 이다.
    // 옛 한 장짜리 카드(`attachment_id`/`caption`)도 그대로 열려야 하므로 둘 다 읽는다.
    const items=Array.isArray(c.images)?c.images.filter(x=>x&&typeof x==='object')
      :((c.attachment_id||c.caption)?[{attachment_id:c.attachment_id,caption:c.caption}]:[]);
    const cols=Math.max(1,Math.min(4,parseInt(c.columns,10)||1));
    if(!items.length)return '<p class="dd-muted">연결된 이미지 없음</p><p class="dd-muted dd-block-note">사진은 앱에서 편집합니다.</p>';
    // 서버는 `str(attachment_id).isdigit()` 로만 이미지를 만든다. Number() 로 읽으면
    // -5·12.5 가 통과해 웹에만 깨진 <img> 가 뜨므로 같은 판정을 쓴다(올마이트 지적).
    const cells=items.map(x=>{
      const raw=String(x.attachment_id==null?'':x.attachment_id);
      const aid=/^\d+$/.test(raw)?raw:'';
      const caption=String(x.caption||'');
      // 이 라우트는 세션 인증이라 웹에서는 URL 직결이 된다(앱은 Bearer 라 바이트를 받아온다).
      const img=aid?`<img class="dd-block-image" src="/api/dock-daily/attachments/${aid}" alt="${esc(caption||'dock image')}">`
        :'<p class="dd-muted">연결된 사진 없음</p>';
      // 캡션은 서버 메일과 같은 규칙 -- 가운데 정렬(CSS)에 `<내용>` 꺾쇠, 빈 캡션은
      // 감싸지 않는다. 서버 `photo_grid.wrap` 과 어긋나면 미리보기와 메일이 달라진다.
      const marked=captionLabel(caption);
      return `<div class="dd-img-cell">${img}<p class="dd-img-cap">${esc(marked)||'&nbsp;'}</p></div>`;
    }).join('');
    // 2열 이상은 4:3 프레임에 aspect-fit 해서 높이를 맞추되 사진 전체를 보인다.
    // 1열은 맞출 옆 칸이 없으므로 원본 비율 그대로 둔다. 서버 메일·iOS 와 같은 게이트다.
    const gridClass=cols>1?'dd-img-grid dd-img-grid--fit':'dd-img-grid';
    return `<div class="${gridClass}" style="grid-template-columns:repeat(${cols},1fr)">${cells}</div>`
      +`<p class="dd-muted dd-block-note">사진 ${items.length}장 · ${cols}열 · 사진은 앱에서 편집합니다.</p>`;
  }
  // Cards carry their own "1) " numbering so what the supervisor types is what
  // the Outlook mail shows. The renderers strip any stored number before
  // applying their own, so a renumbered card never double-numbers.
  const NUM=window.DockDailyNumbering;
  const ORDER=window.DockDailySectionOrder;
  // 표 규칙 정본은 static/js/dock_daily_table.js 다(실행형 테스트로 잠긴다).
  const TABLE=window.DockDailyTable;
  // 표 편집기(형 질문 2026-08-22 "표 섹션 넣는 버튼은 웹에 있니?"). 앱과 같은 규칙을
  // 쓰되 조작은 버튼이다 -- 마우스가 있으니 스와이프 대신 행/열 옆의 ✕ 를 둔다.
  //
  // 🔴 셀 입력은 **다시 그리지 않는다**. 한 글자마다 renderSections 를 돌리면 포커스와
  // 커서 위치가 매번 날아가 한글 조합이 깨진다. 구조가 바뀌는 행·열 추가/삭제에서만
  // 다시 그린다.
  function tableEditor(b,key){
    const g=TABLE.read(b.content);
    const rowX=TABLE.canRemoveRow(g), colX=TABLE.canRemoveColumn(g);
    const head=g.columns.map((v,c)=>`<th><div class="dd-tbl-headcell"><input class="dd-tbl-cell" data-key="${key}" data-col="${c}" value="${esc(v)}" placeholder="열 이름" aria-label="${c+1}번째 열 이름"><button class="dd-tbl-x" type="button" data-tbl-del-col="${c}" data-key="${key}" title="이 열 삭제" aria-label="${c+1}번째 열 삭제"${colX?'':' disabled'}>✕</button></div></th>`).join('');
    const body=g.rows.map((r,ri)=>`<tr>${r.map((v,c)=>`<td><input class="dd-tbl-cell" data-key="${key}" data-row="${ri}" data-col="${c}" value="${esc(v)}" aria-label="${ri+1}행 ${c+1}열"></td>`).join('')}<td class="dd-tbl-rowtool"><button class="dd-tbl-x" type="button" data-tbl-del-row="${ri}" data-key="${key}" title="이 행 삭제" aria-label="${ri+1}번째 행 삭제"${rowX?'':' disabled'}>✕</button></td></tr>`).join('');
    return `<table class="dd-block-table dd-table-edit"><thead><tr>${head}<th class="dd-tbl-rowtool"></th></tr></thead><tbody>${body}</tbody></table>`
      +`<div class="dd-tbl-tools"><button class="dd-btn alt" type="button" data-tbl-add-row="1" data-key="${key}">＋ 행</button>`
      +`<button class="dd-btn alt" type="button" data-tbl-add-col="1" data-key="${key}">＋ 열</button>`
      +`<span class="dd-muted">고친 표는 저장을 눌러야 반영됩니다.</span></div>`;
  }
  // 내용은 통째로 교체한다 -- 서버 upsert 도 content 를 통째로 바꾸므로 남은 옛 키가
  // 있으면 저장 전후 모양이 달라진다.
  function setTable(b,grid){b.content={columns:grid.columns,rows:grid.rows};b._edit=true;state.dirty=true;}
  function bindTableEditors(){
    document.querySelectorAll('.dd-tbl-cell').forEach(i=>i.oninput=()=>{
      const b=findBlock(i.dataset.key); if(!b)return;
      const g=TABLE.read(b.content), col=Number(i.dataset.col);
      // 헤더 칸에는 data-row 가 없다.
      setTable(b,i.dataset.row===undefined?TABLE.setColumn(g,col,i.value)
                                          :TABLE.setCell(g,Number(i.dataset.row),col,i.value));
    });
    const mutate=(btn,fn)=>{const b=findBlock(btn.dataset.key);if(!b)return;setTable(b,fn(TABLE.read(b.content)));renderSections();};
    document.querySelectorAll('[data-tbl-add-row]').forEach(x=>x.onclick=()=>mutate(x,g=>TABLE.addRow(g)));
    document.querySelectorAll('[data-tbl-add-col]').forEach(x=>x.onclick=()=>mutate(x,g=>TABLE.addColumn(g)));
    document.querySelectorAll('[data-tbl-del-row]').forEach(x=>x.onclick=()=>mutate(x,g=>TABLE.removeRow(g,Number(x.dataset.tblDelRow))));
    document.querySelectorAll('[data-tbl-del-col]').forEach(x=>x.onclick=()=>mutate(x,g=>TABLE.removeColumn(g,Number(x.dataset.tblDelCol))));
  }
  // 카드는 내용만큼 늘어난다(형 지시 2026-08-21). 안쪽 스크롤바도, 손으로 끄는
  // 리사이즈 핸들도 없다 — 작업 개수가 칸을 넘으면 카드 자체가 커지고 페이지가 스크롤된다.
  // 'auto' 로 먼저 줄이는 이유: 이미 늘어난 높이가 남아 있으면 scrollHeight 가 그 높이를
  // 그대로 되돌려줘서 지워도 줄어들지 않는다. CSS 의 min-height 가 하한을 잡는다.
  function autoGrow(ta){
    if(!ta)return;
    ta.style.height='auto';
    const h=ta.scrollHeight;
    // 숨어 있는 동안(display:none)에는 scrollHeight 가 0 이다. 그때 0px 을 박아두면
    // 다시 보일 때 카드가 한 줄로 잘린 채 남으므로, 인라인 높이를 아예 지워
    // CSS min-height 에 맡기고 다음 계산 기회를 기다린다.
    if(!h){ta.style.removeProperty('height');return;}
    ta.style.height=h+'px';
  }
  function growAll(){document.querySelectorAll('.dd-section-edit').forEach(autoGrow);}
  function applyNumbering(ta,result){
    if(ta.value!==result.value)ta.value=result.value;
    ta.selectionStart=ta.selectionEnd=result.caret;
    // 엔터·번호붙이기는 value 를 직접 갈아끼우므로 input 이벤트가 안 뜬다. 여기서
    // 같이 키우지 않으면 줄이 늘 때마다 한 줄씩 잘려 보인다.
    autoGrow(ta);
  }
  // blockText 가 읽은 키에 그대로 되돌려 쓴다. 항상 body 에 쓰면, 서버 렌더가 title 을
  // 먼저 보므로(title||body||text) title 을 읽어 보여준 카드는 수정이 조용히 무시된다.
  function textKey(b){const c=b.content||{};if(c.title)return 'title';if(c.text&&!c.body)return 'text';return 'body';}
  // 🔴 **있는 글 키는 전부** 같은 값으로 맞춘다. 정본은 앱 `DockDailyBlockText.content`
  // (`for name in ["title","body","text"] where object[name] != nil`) 이고, 웹만 한 키에
  // 써서 계약이 갈려 있었다. 한 키만 쓰면 `{title:'OLD', body:'STALE'}` 같은 수집 블록에서
  // title 을 비운 순간 `blockText` 가 옛 body 로 되떨어진다 -- 다시 그리면 형이 지운 글이
  // 화면에 되살아나고, 저장하면 서버 `_plain`(title→body→text)이 그 문장을 메일로 보낸다.
  // 없는 키는 만들지 않는다(앱과 동일). 아무 키도 없으면 읽던 키에 쓴다.
  function writeText(b,value){
    const c={...(b.content||{})};
    let wrote=false;
    for(const k of ['title','body','text'])if(c[k]!==undefined&&c[k]!==null){c[k]=value;wrote=true;}
    if(!wrote)c[textKey(b)]=value;
    return c;
  }
  function commitItems(ta){
    const b=findBlock(ta.dataset.key); if(!b||blockText(b)===ta.value)return;
    // block_type 을 'paragraph' 로 갈아치우지 않는다 — item 블록이 문단으로 바뀌면서
    // 서버가 content 를 통째로 교체해 progress/status 가 사라졌다(올마이트 지적).
    b.content=writeText(b,ta.value); b._edit=true; state.dirty=true;
  }
  function normalizeItems(ta,keepCaretLine){
    applyNumbering(ta,NUM.renumber(ta.value,ta.selectionStart,keepCaretLine));
    commitItems(ta);
  }
  function bindItemNumbering(ta){
    let composing=false;
    // oninput 프로퍼티는 아래에서 번호 로직이 쓰고 있으므로 높이는 별도 리스너로 붙인다
    // (프로퍼티 핸들러와 addEventListener 는 둘 다 뜬다). 붙는 즉시 한 번 키운다 —
    // 저장된 카드는 처음 그려질 때 이미 여러 줄이다.
    ta.addEventListener('input',()=>autoGrow(ta));
    autoGrow(ta);
    // Hangul/IME input must never be renumbered mid-composition, so the empty
    // card gets its "1) " on focus instead of on the first keystroke.
    ta.onfocus=()=>{if(!ta.value.trim()){ta.value='1) ';ta.selectionStart=ta.selectionEnd=3;}};
    ta.oncompositionstart=()=>{composing=true;};
    // A composed run can finish on a line that still has no number (legacy card
    // or a paste), so normalize once the IME hands the text back.
    ta.oncompositionend=()=>{composing=false;if(NUM.needsNumbering(ta.value))normalizeItems(ta,true);else commitItems(ta);};
    ta.onkeydown=e=>{
      if(e.isComposing||composing)return;
      // 번호 접두어 안에서의 백스페이스는 그 줄을 앞줄과 합친다(= 엔터 취소). 기본 삭제에
      // 맡기면 남은 조각이 작업내용으로 보여 번호가 다시 붙는다(`2) 2`).
      // Shift 여부는 보지 않는다 — Shift+Backspace 도 한 글자를 지우므로 그냥 두면
      // 접두어가 부서지는 같은 버그가 그 조합에서만 되살아난다(올마이트 지적).
      if(e.key==='Backspace'&&ta.selectionStart===ta.selectionEnd){
        const back=NUM.deleteBackward(ta.value,ta.selectionStart);
        // 첫 줄 no-op 은 값이 그대로다 → 기본 삭제만 막고 저장 표시는 건드리지 않는다.
        if(back){e.preventDefault();if(back.value!==ta.value){applyNumbering(ta,back);commitItems(ta);}}
        return;
      }
      if(e.key!=='Enter'||e.shiftKey)return;
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
  // 카드 제목줄의 순서·삭제 도구(형 지시 2026-08-22). 앱은 제목줄 롱프레스 메뉴로
  // 같은 일을 한다 -- 웹은 마우스가 있으니 버튼을 그대로 둔다.
  // 🔴 끝단 방향은 `disabled` 로 남긴다(앱은 메뉴 항목을 아예 감춘다): 버튼이 사라지면
  // 카드마다 도구 위치가 달라져 옆 버튼을 잘못 누른다.
  // `blockCount` 는 **서버에 실제로 있는** 블록 수다(`_new` 초안 제외). 앱은 초안을
  // `report.blocks` 에 넣지 않는데 웹은 `ensureSectionEditors` 가 빈 카드에 글칸 초안을
  // 밀어넣으므로, 그걸 세면 고아 카드에서도 복구 버튼이 사라져 앱과 갈린다.
  function sectionTools(s,all,locked,blockCount){
    if(locked)return '';                            // 확정본은 서버가 409 로 끊는다
    const btn=(delta,glyph,word)=>`<button class="dd-sec-btn" type="button" data-move-section="${esc(s.section_key)}" data-delta="${delta}" title="${word}" aria-label="${esc(s.label||s.section_key)} ${word}"${ORDER.canMove(all,s.section_key,delta)?'':' disabled'}>${glyph}</button>`;
    // 고정 섹션(Shipyard/Survey/Vendor/Remark)은 순서만 바꿀 수 있다 -- 삭제는 서버가
    // `fixed_section` 으로 거절하므로 버튼을 아예 내지 않는다.
    const del=s.kind==='special'
      ? `<button class="dd-sec-btn danger" type="button" data-del-section-card="${esc(s.section_key)}" title="이 섹션을 아주 삭제" aria-label="${esc(s.label||s.section_key)} 섹션 삭제">삭제</button>`
      : '';
    // 🔴 기존 섹션에 표를 붙이는 버튼은 두지 않는다(형 지시 2026-08-22, 앱과 동일).
    // 표는 제목을 직접 받는 `＋ 표 섹션` 으로만 만든다 -- 남의 섹션 제목 아래 딸려
    // 들어가면 무슨 표인지 적을 데가 없다. 예외는 **빈 special 섹션** = 섹션은 만들어졌는데
    // 표 삽입이 실패해 남은 고아 카드. 여기서 못 채우면 되살릴 길이 없다(앱 canAddTable).
    const tbl=TABLE.canAddTable(s,blockCount,locked)
      ? `<button class="dd-sec-btn" type="button" data-add-table="${esc(s.section_key)}" title="이 빈 섹션에 표를 넣습니다" aria-label="${esc(s.label||s.section_key)} 에 표 추가">＋ 표</button>`
      : '';
    return `<span class="dd-sec-tools">${tbl}${btn(-1,'▲','위로')}${btn(1,'▼','아래로')}${del}</span>`;
  }
  function renderSections() {
    const locked=state.report.status==='final'; const blocks=state.report.blocks||[];
    const all=state.report.sections||[];
    $('#dd-sections').innerHTML=all.filter(s=>s.enabled).map(s=>{
      const bs=blocks.filter(b=>b.section_key===s.section_key&&!b._delete);
      return `<div class="dd-card dd-section" data-section="${esc(s.section_key)}"><div class="dd-section-head"><h3>${esc(s.label)}</h3><span class="dd-section-aside"><span class="dd-muted">엔터를 누르면 1) 2) 번호가 붙습니다</span>${sectionTools(s,all,locked,bs.filter(b=>!b._new).length)}</span></div>${bs.map((b,i)=>{const key=b._key??b.id;
      // Provenance badges only carry meaning for auto-collected blocks; hand
      // written cards showed a permanent "수동" pair that said nothing.
      const badges=b.origin==='dock_auto'?`<span class="dd-badge auto">자동수집</span>${b.manual_override?'<span class="dd-badge">수동 수정 보호</span>':''}`:'';
      const del=bs.length>1?`<button class="dd-btn alt delete-inline" type="button" data-key="${key}" ${locked?'disabled':''}>삭제</button>`:'';
      const meta=(badges||del)?`<div class="dd-block-meta"><span>${badges}</span>${del}</div>`:'';
      const body=isTextBlock(b)
        ?`<textarea class="dd-section-edit" data-key="${key}" placeholder="${esc(s.label)} 내용을 입력하세요" ${locked?'disabled':''}>${esc(blockText(b))}</textarea>`
        :(b.block_type==='table'&&!locked?tableEditor(b,key):readOnlyBlock(b));
      return `<div class="dd-block-editor">${meta}${body}</div>`}).join('')}</div>`;
    }).join('');
    document.querySelectorAll('.dd-section-edit').forEach(bindItemNumbering);
    bindTableEditors();
    document.querySelectorAll('[data-add-table]').forEach(b=>b.onclick=()=>once(b,()=>addTable(b.dataset.addTable)));
    document.querySelectorAll('[data-move-section]').forEach(b=>b.onclick=()=>once(b,()=>moveSection(b.dataset.moveSection,Number(b.dataset.delta))));
    document.querySelectorAll('[data-del-section-card]').forEach(b=>b.onclick=()=>once(b,()=>deleteSection(b.dataset.delSectionCard)));
    document.querySelectorAll('.delete-inline').forEach(btn=>btn.onclick=()=>{const b=findBlock(btn.dataset.key);if(!b)return;
      // 표·이미지는 웹에서 다시 만들 수 없고(편집기는 앱에만 있다), 이미지 블록을 지우면
      // 서버가 연결된 첨부까지 함께 지운다. 한 번 확인을 받는다.
      if(!isTextBlock(b)&&!confirm(b.block_type==='image'?'이미지 블록을 삭제할까요?\n\n연결된 첨부파일도 함께 삭제되고, 이미지 블록은 앱에서만 다시 만들 수 있습니다.':'표 블록을 삭제할까요?\n\n적어 둔 표 내용이 사라집니다. 빈 표는 제목줄의 ＋ 표 로 다시 만들 수 있습니다.'))return;if(b._new)state.report.blocks=state.report.blocks.filter(x=>x!==b);else b._delete=true;state.dirty=true;ensureSectionEditors();renderSections();});
  }
  function findBlock(key){return (state.report.blocks||[]).find(b=>String(b._key??b.id)===String(key));}
  function canLeaveDraft(){return !state.dirty||confirm('저장되지 않은 수정사항이 있습니다. 저장하지 않고 이동할까요?');}
  function renderAttachments(){
    const ats=state.report.attachments||[]; const locked=state.report.status==='final';
    // The delete cannot sit inside the preview button (invalid HTML, and one
    // click would fire both), so each attachment is a flex pair. On a 확정본 the
    // x is left out entirely rather than shown disabled: the server answers 409
    // there, and an always-refused button reads as a bug.
    $('#dd-attachments').innerHTML=ats.length?ats.map(a=>`<div class="dd-attachment-row"><button class="dd-attachment" type="button" data-attachment="${a.id}" data-name="${esc(a.original_name)}"><b>${esc(a.original_name)}</b><span>${esc(a.mime_type)} · ${(a.size/1024).toFixed(1)} KB · 미리보기</span></button>${locked?'':`<button class="dd-att-del" type="button" data-del-attachment="${a.id}" title="첨부 삭제" aria-label="${esc(a.original_name)} 삭제">✕</button>`}</div>`).join(''):`<p class="dd-muted">등록된 첨부파일이 없습니다.</p>`;
    document.querySelectorAll('[data-attachment]').forEach(b=>b.onclick=()=>openFilePreview(+b.dataset.attachment,b.dataset.name));
    document.querySelectorAll('[data-del-attachment]').forEach(b=>b.onclick=()=>once(b,()=>deleteAttachment(+b.dataset.delAttachment)));
  }
  // Removing the row from local state instead of re-fetching the report: the
  // server only touched attachments, and a reload here would throw away
  // unsaved section edits (uploadFiles guards on state.dirty for the same
  // reason). A failed request keeps the row, so the list never lies.
  async function deleteAttachment(id){
    if(!state.report)return; const a=(state.report.attachments||[]).find(x=>x.id===id); if(!a)return; clearErr();
    const linked=a.block_id?'\n\n본문 블록에 연결된 파일입니다. 블록과 설명은 남고 사진만 사라집니다.':'';
    if(!confirm(`${a.original_name||'첨부'} 을(를) 삭제할까요?${linked}\n\n파일이 서버에서 완전히 지워지고 되돌릴 수 없습니다.`))return;
    // 404 는 성공과 같이 다룬다: 다른 탭·기기에서 이미 지운 행이므로 목록에 남겨두면
    // 화면이 서버보다 뒤처진다. api() 는 상태코드를 안 넘기므로 여기서만 fetch 를 쓴다.
    const r=await fetch(`/api/dock-daily/attachments/${id}`,{method:'DELETE'});
    if(!r.ok&&r.status!==404){const b=await r.json().catch(()=>({}));throw new Error(b.error||`삭제 실패 (${r.status})`);}
    state.report.attachments=(state.report.attachments||[]).filter(x=>x.id!==id);
    renderAttachments();
  }
  // `sectionUpdates` 를 주면 글 저장과 섹션 순서를 **한 번의 CAS PUT** 으로 보낸다
  // (앱 `save(sectionUpdates:)` 와 같은 계약, 올마이트 지적 2026-08-22). 두 번 나눠 보내면
  // 첫 요청이 revision 을 올려 두 번째가 409 로 튕기고, 그 사이 계산해 둔 순서는 다른
  // 기기가 방금 바꾼 순서를 조용히 되돌린다.
  //
  // 🔴 반환값은 "이 응답을 화면에 반영했는가" 다. 요청 도중 형이 다른 일자를 열었으면
  // 옛 응답으로 새 화면을 덮지 않고 false 를 준다 -- 덮으면 목록은 B 일자를 가리키는데
  // 본문은 A 일자가 뜬다(`selectReport` 와 같은 방어, 올마이트 지적).
  async function save(sectionUpdates) {
    if(!state.report)return false; clearErr();
    const seq=selectSeq, rid=state.report.id;
    const itinerary={}; document.querySelectorAll('.dd-itinerary-date').forEach(i=>itinerary[i.dataset.key]=i.value||null);
    if(Object.keys(itinerary).some(k=>(state.project[k]||null)!==itinerary[k])){
      const updated=await api(`/api/dock-daily/projects/${state.project.id}`,{...json(itinerary),method:'PATCH'});
      state.project=updated; state.projects=state.projects.map(p=>p.id===updated.id?updated:p);
    }
    const operations=[];
    state.report.blocks.filter(b=>b._delete).forEach(b=>operations.push({op:'delete',id:b.id}));
    state.report.blocks.filter(b=>!b._delete&&(b._new||b._edit)&&savable(b)).forEach(b=>operations.push({op:'upsert',id:b._new?undefined:b.id,section_key:b.section_key,block_type:b.block_type||'paragraph',content:{...(b.content||{})},sort_order:b.sort_order||0}));
    const payload={revision:state.report.revision,operations};
    if(sectionUpdates)payload.section_updates=sectionUpdates;
    const saved=await api(`/api/dock-daily/reports/${rid}`,{...json(payload),method:'PUT'});
    if(seq!==selectSeq||state.report?.id!==rid)return false;
    state.report=saved; state.dirty=false; ensureSectionEditors();
    $('#dd-report-meta').textContent=`${state.report.report_date} · ${state.report.status} · revision ${state.report.revision}`; renderItinerary(); renderSections(); renderAttachments();
    return true;
  }
  // section_key 는 서버가 만든다(POST .../sections). 웹과 앱이 각자 키를 만들면 규칙이
  // 두 벌이 되고 서로 다른 키를 뱉는다 -- 제목만 보내고 키는 받는다.
  //
  // `wantTable` 이면 그 섹션 안에 빈 표를 하나 넣는다(형 질문 2026-08-22 "표 섹션 넣는
  // 버튼은 웹에 있니?" -- 앱 `addTableSection` 파리티). 표는 다른 카드의 하위항목이
  // 아니라 제목을 가진 자기 섹션이어야 한다는 형 지시(2026-08-21)를 웹에도 편다.
  async function addSection(label,wantTable){
    const name=String(label||'').trim();
    if(!name){err(new Error('섹션 제목을 입력하세요.'));return;}
    clearErr();
    // 🔴 확정 판정이 **섹션 생성보다 먼저**다(앱과 같은 순서). 나중에 보면 표 저장만
    // 409 로 튕기고 빈 섹션은 프로젝트의 모든 일자에 영구히 남는다.
    if(wantTable){
      if(!state.report){err(new Error('표를 넣을 보고서 일자를 먼저 고르세요.'));return;}
      if(state.report.status==='final'){err(new Error('확정된 보고서에는 표 섹션을 추가할 수 없습니다. 확정을 취소한 뒤 다시 시도하세요.'));return;}
    }
    // 🔴 요청 전에 어느 프로젝트·일자에서 눌렀는지 붙잡아 둔다. POST 가 도는 동안 형이
    // 다른 프로젝트를 고르면, 응답으로 state.project 를 덮는 순간 화면은 B 프로젝트인데
    // 내용은 A 프로젝트가 된다 -- 이어지는 표도 엉뚱한 일자에 들어간다(올마이트 지적).
    const pid=state.project.id, seq=selectSeq, rid=state.report?.id??null;
    let updated;
    try{
      updated=await api(`/api/dock-daily/projects/${pid}/sections`,{...json({label:name}),method:'POST'});
    }catch(error){err(error);return;}
    if(state.project?.id!==pid){
      // 목록만 조용히 갱신하고 화면은 건드리지 않는다. 섹션은 서버에 이미 만들어졌다.
      state.projects=state.projects.map(p=>p.id===updated.id?updated:p);
      notice(`섹션 "${name}" 을 추가했습니다. 그 사이 다른 프로젝트를 열어서 여기에는 반영하지 않았습니다.`);
      return;
    }
    state.project=updated;state.projects=state.projects.map(p=>p.id===updated.id?updated:p);
    $('#dd-section-label').value='';
    // 섹션 목록을 다시 그린다. 열린 보고서에도 바로 카드가 생겨야 한다.
    renderSpecialTools();
    const sameReport=!!state.report&&state.report.id===rid&&seq===selectSeq;
    if(state.report&&sameReport){state.report.sections=updated.sections;}
    // 🔴 방금 만든 키는 서버가 `created_section_key` 로 알려준다. 응답 목록의 차집합으로
    // 되짚으면 다른 기기가 같은 순간에 만든 **남의 섹션**을 고른다(서버 주석과 같은 이유).
    const key=updated.created_section_key;
    if(!(wantTable&&sameReport)){
      if(state.report&&sameReport){ensureSectionEditors();renderSections();}
      notice(wantTable
        ? `섹션 "${name}" 을 추가했습니다. 그 사이 다른 일자를 열어서 표는 넣지 않았습니다 -- 카드의 ＋ 표 로 넣으세요.`
        : `섹션 "${name}" 을 추가했습니다.`);
      return;
    }
    if(!key){
      ensureSectionEditors();renderSections();
      err(new Error(`섹션 "${name}" 은 만들었지만 어느 섹션인지 확인하지 못해 표를 넣지 못했습니다. 새로고침한 뒤 그 카드의 ＋ 표 를 누르세요.`));
      return;
    }
    const outcome=await addTable(key,{silent:true});
    // 🔴 실패 문구를 단정하지 않는다(앱 `verify()` 와 같은 이유). 서버가 커밋한 뒤
    // 응답만 유실될 수 있어서 "넣지 못했습니다" 가 거짓이 될 수 있다.
    if(outcome==='ok'){
      notice(`표 섹션 "${name}" 을 추가했습니다. 섹션은 이 프로젝트의 모든 일자에 생기고, 표가 없는 날은 메일에 NIL 로 나갑니다.`);
    }else if(outcome==='stale'){
      notice(`섹션 "${name}" 을 추가했습니다. 그 사이 다른 일자를 열어서 표는 넣지 않았습니다 -- 카드의 ＋ 표 로 넣으세요.`);
    }else if(outcome==='failed'){
      err(new Error(`섹션 "${name}" 은 만들었지만 표를 넣지 못했습니다. 그 빈 카드의 ＋ 표 로 채우거나, 왼쪽 목록에서 삭제하세요.`));
    }else{
      err(new Error(`섹션 "${name}" 은 만들었지만 표가 들어갔는지 확인하지 못했습니다. 새로고침해서 그 카드를 확인하세요.`));
    }
  }
  // 섹션 하나에 빈 표를 넣는다. 서버에는 표 전용 라우트가 없고 블록 upsert 계약이
  // 그대로 표를 받으므로(BLOCK_TYPES 에 'table'), 새 블록을 state 에 넣고 평소 저장을 탄다.
  //
  // 🔴 미저장 글도 같은 PUT 에 함께 실린다. 따로 두 번 보내면 첫 요청이 revision 을
  // 올려 두 번째가 409 로 튕긴다(`moveSection` 과 같은 이유).
  // 반환값은 'ok' | 'stale' | 'failed' | 'unknown' 이다. 🔴 실패와 "모르겠다" 를 합치지
  // 않는다 -- 서버가 커밋한 뒤 응답만 유실될 수 있어서, 그때 "못 넣었다" 고 말하면 형은
  // 이미 들어간 표를 한 번 더 넣는다(앱 `verify()` 와 같은 계약).
  async function addTable(key,opts){
    const silent=!!(opts&&opts.silent);
    if(!state.report)return 'failed';
    if(state.report.status==='final'){err(new Error('확정된 보고서에는 표를 넣을 수 없습니다. 확정을 취소한 뒤 다시 시도하세요.'));return 'failed';}
    const section=(state.report.sections||[]).find(s=>s.section_key===key);
    if(!section){err(new Error('그 섹션을 찾지 못했습니다. 새로고침한 뒤 다시 시도하세요.'));return 'failed';}
    if(!silent)clearErr();
    const rid=state.report.id, g=TABLE.empty();
    const tables=r=>(r.blocks||[]).filter(b=>b.section_key===key&&b.block_type==='table').length;
    // 🔴 "그 섹션에 표가 있는가" 로 성공을 판정하면 안 된다 -- 이미 표가 있던 섹션에
    // ＋ 표 를 눌렀다가 저장이 실패해도 `있다` 라서 성공으로 보고한다(올마이트 지적).
    // 새 블록에는 아직 서버 id 가 없으므로 **개수가 늘었는가**로 센다.
    const before=tables(state.report);
    // 카드 안에서 맨 뒤에 붙인다 -- 글이 이미 있는 섹션에 표를 넣으면 글 다음에 온다.
    const tail=(state.report.blocks||[]).filter(b=>!b._delete&&b.section_key===key)
      .reduce((m,b)=>Math.max(m,b.sort_order||0),0);
    const draft={id:0,_key:state.tempId--,section_key:key,block_type:'table',
      content:{columns:g.columns,rows:g.rows},sort_order:tail+1,origin:'manual',manual_override:1,_new:true};
    const dropDraft=()=>{state.report.blocks=(state.report.blocks||[]).filter(b=>b!==draft);};
    state.report.blocks.push(draft);
    state.dirty=true;
    let applied;
    try{ applied=await save(); }
    catch(error){
      if(!silent)err(new Error(conflictText(error)));
      // 저장이 실제로 들어갔는지 서버에 되묻는다.
      let latest;
      try{ latest=await api(`/api/dock-daily/reports/${rid}`); }catch(_){ latest=null; }
      if(state.report?.id!==rid)return 'unknown';   // 그 사이 다른 일자를 열었다
      // 🔴 확인 자체를 못 했어도 낙관적 draft 는 반드시 걷어낸다. 남겨두면 실제로는
      // 커밋됐던 표를 다음 저장이 한 번 더 만든다(올마이트 지적). 나머지 미저장 수정과
      // dirty 는 그대로 두고, 형에게는 확인 못 했다고 말한다.
      if(!latest){dropDraft();renderSections();return 'unknown';}
      if(tables(latest)>before){
        // 커밋은 됐고 응답만 유실됐다. 이때만 서버본을 채택한다 -- 같은 PUT 에 실려간
        // 다른 수정도 함께 들어갔으므로 로컬을 버려도 잃는 게 없다.
        state.report=latest; state.dirty=false; ensureSectionEditors(); renderSections(); renderAttachments();
        if(!silent){clearErr();notice(`"${section.label||key}" 에 빈 표를 넣었습니다. (응답이 늦어 서버에서 다시 읽었습니다)`);}
        return 'ok';
      }
      // 🔴 확실히 실패했을 때는 서버본으로 덮지 않는다. 같은 PUT 에 실려 있던 형의
      // 미저장 글까지 통째로 사라진다(올마이트 지적) -- 표 draft 만 빼고 그대로 둔다.
      dropDraft(); renderSections();
      return 'failed';
    }
    // `save()` 가 false 면 요청 도중 다른 일자를 열었다는 뜻이다. 그 표는 서버에 들어갔지만
    // 지금 화면은 다른 보고서라 여기서 덮지 않는다.
    if(!applied)return 'stale';
    if(!silent)notice(`"${section.label||key}" 에 빈 표를 넣었습니다. 칸을 채운 뒤 저장하세요.`);
    return 'ok';
  }
  // 섹션을 목록에서 아주 지운다(형 지시 2026-08-22). 체크 해제는 숨김일 뿐이라,
  // 잘못 만든 빈 카드가 프로젝트에 영원히 남아 있었다.
  //
  // 🔴 서버가 그 섹션의 블록을 **모든 일자에서** 함께 지운다(안 지우면 `sec_N` 번호를
  // 재사용할 때 옛 블록이 새 섹션에서 되살아난다). 그래서 내용이 있으면 서버가 409
  // `section_not_empty` 로 한 번 끊고, 그때만 개수를 보여주고 다시 물어본다.
  async function deleteSection(key){
    const section=(state.project?.sections||[]).find(s=>s.section_key===key);
    const name=String(section?.label||'').trim()||key;
    clearErr();
    // 🔴 삭제하면 열린 보고서를 다시 읽어야 하는데(서버가 블록을 지웠다), 그 재조회가
    // 저장 안 한 편집을 통째로 버린다 -- 형은 지운 적 없는 문장이 사라진 걸 본다.
    // 미리보기와 같은 계약으로 먼저 저장한다(올마이트 지적).
    // 🔴 프로젝트 id 를 `await` **전에** 붙잡는다(`addSection` 과 같은 이유). 저장이 도는
    // 동안 형이 다른 프로젝트로 옮기면 `state.project` 는 그쪽이 되고, 그때 DELETE 가
    // 나가면 **남의 프로젝트의 같은 이름 섹션**을 모든 일자에서 지운다 -- 되돌릴 수 없다.
    const pid=state.project.id;
    if(state.dirty)await save();
    const send=async confirmed=>api(`/api/dock-daily/projects/${pid}/sections/${encodeURIComponent(key)}`,
      {...json(confirmed?{confirm:'delete-section'}:{}),method:'DELETE'});
    let body;
    try{ body=await send(false); }
    catch(error){
      if(error.status!==409||error.code!=='section_not_empty'){err(error);return;}
      const dates=(error.body?.dates||[]).join(', ');
      // 🔴 "이 날짜만" 이 아니라 모든 일자에서 사라진다는 걸 반드시 말한다.
      if(!confirm(`섹션 "${name}" 에 내용 ${error.body?.blocks||0}개가 있습니다${dates?` (${dates})`:''}.\n\n`
        +'지우면 이 프로젝트의 모든 일자에서 함께 사라지고 되돌릴 수 없습니다.')) return;
      try{ body=await send(true); }catch(retry){err(retry);return;}
    }
    state.projects=state.projects.map(p=>p.id===body.id?body:p);
    if(state.project?.id!==pid){
      // 그 사이 다른 프로젝트를 열었다. 목록만 갱신하고 화면은 건드리지 않는다
      // (`addSection` 과 같은 계약) -- 섹션은 서버에서 이미 지워졌다.
      notice(`섹션 "${name}" 을 지웠습니다. 그 사이 다른 프로젝트를 열어서 여기에는 반영하지 않았습니다.`);
      return;
    }
    state.project=body;
    renderSpecialTools();
    // 열려 있는 보고서에서도 그 카드와 내용이 사라져야 한다. 서버가 블록을 지웠으므로
    // 여기서 다시 읽는다 -- 화면의 옛 블록을 그대로 두면 다음 저장이 없는 블록을 올린다.
    if(state.report)await selectReport(state.report.id);
    notice(body.deleted_blocks>0
      ? `섹션 "${name}" 과 그 안의 내용 ${body.deleted_blocks}개를 지웠습니다.`
      : `섹션 "${name}" 을 지웠습니다.`);
  }
  // 섹션 카드 순서 바꾸기(형 지시 2026-08-22). 앱은 카드 제목줄 롱프레스, 웹은 ▲▼ 버튼.
  // 규칙은 `dock_daily_section_order.js` 한 곳에 있고 앱과 같은 값을 낸다.
  //
  // 🔴 미저장 글을 먼저 따로 저장하지 않는다. `save()` 한 번에 순서를 실어 보낸다 --
  // 두 번 나눠 보내면 첫 요청이 revision 을 올려 두 번째가 409 로 튕기고, 그때는 이미
  // 계산해 둔 순서가 옛 목록 기준이라 다른 기기가 방금 바꾼 순서를 되돌린다(올마이트 지적).
  async function moveSection(key,delta){
    if(!state.report)return;
    const next=ORDER.movingVisible(state.report.sections||[],key,delta);
    if(!next)return;                                // 끝단 -- 버튼도 이미 disabled 다
    clearErr();
    // 409 는 revision 충돌·확정잠금이 서로 다른 해법이라 `conflictText` 로 갈라 읽는다.
    // 여기서 안 잡으면 `once()` 가 서버 원문을 그대로 띄운다.
    let applied;
    try{ applied=await save(ORDER.payload(next)); }
    catch(error){ err(new Error(conflictText(error))); return; }
    if(!applied)return;                             // 그 사이 형이 다른 일자를 열었다
    // 순서는 프로젝트 값이라 왼쪽 Special 목록도 같은 순서를 따라가야 한다.
    if(state.project){
      state.project={...state.project,sections:state.report.sections};
      state.projects=state.projects.map(p=>p.id===state.project.id?state.project:p);
      renderSpecialTools();
    }
    notice('섹션 순서를 바꿨습니다. 이 프로젝트의 모든 일자·확정본·메일에 함께 적용됩니다.');
  }
  async function toggleSpecial(key,enabled){const updated=await api(`/api/dock-daily/projects/${state.project.id}`,{...json({sections:[{section_key:key,enabled}]}),method:'PATCH'});state.project=updated;state.projects=state.projects.map(p=>p.id===updated.id?updated:p);if(state.report){state.report.sections=updated.sections;ensureSectionEditors();renderSections();}}

  const previewModal=$('#dd-preview-modal');
  function closePreview(){previewModal.hidden=true;document.body.style.overflow='';state.preview=null;}
  async function openPreview(kind){
    if(state.dirty)await save(); const v=await api(`/api/dock-daily/reports/${state.report.id}/${kind}-preview`); state.preview={kind,data:v};
    $('#dd-preview-title').textContent=kind==='email'?'이메일 미리보기':'SVMS 미리보기'; $('#dd-preview-status').textContent='';
    $('#dd-copy-all').hidden=kind!=='email'; $('#dd-svms-push').hidden=kind!=='svms';
    if(kind==='email') $('#dd-preview-content').innerHTML=`<div class="dd-email-subject"><b>제목</b><br>${esc(v.subject)}</div><div class="dd-email-html">${v.html}</div>`;
    else {const f=v.fields||{},push=$('#dd-svms-push');
      // 🔴 계약(publishable)만으로 버튼을 열면 이미 상신한 보고서에서 계속 눌린다.
      //    반영 상태도 함께 본다(앱과 동일 게이트).
      const S=window.DockDailySVMS, sync=state.report?state.report.svms_sync_status:null;
      const reason=svmsPublishBlock(), allowed=!reason;
      push.disabled=!allowed;
      push.textContent=S.normalize(sync)==='failed'?'SVMS 재상신':'SVMS 상신';
      // 사유는 규칙 모듈 한 곳에서만 만든다(앱과 같은 문구).
      push.title=allowed?'미리보기 내용을 SVMS에 반영':S.publishBlockText(reason,sync);
      $('#dd-preview-status').textContent=allowed?'':S.publishBlockText(reason,sync);
      // 🔴 사유를 적는다. 전엔 "상신 불가" 만 떠서 형이 뭘 고쳐야 하는지 화면에 없었다.
      const blockers=S.blockerList(v);
      const why=blockers.length?`<div class="dd-svms-blockers"><b>상신 불가 사유</b><ul>${blockers.map(b=>`<li>${esc(b)}</li>`).join('')}</ul></div>`:'';
      $('#dd-preview-content').innerHTML=`<p class="dd-modal-intro"><b>${v.publishable?'SVMS 반영 준비 완료':'Preview only 안전게이트'}</b><br>DK_CD와 byte limit 계약이 모두 확인되어야 실제 반영됩니다.<br>표·사진은 SVMS 본문에 넣지 않습니다(이메일 본문에만 나갑니다).</p>${why}<div id="dd-svms-dock-link"></div><div class="dd-svms-grid"><b>DK_CD</b><pre>${esc(f.DK_CD||'')}</pre><b>DR_DT</b><pre>${esc(f.DR_DT||'')}</pre><b>Shipyard</b><pre>${esc(f.RMK_SYD||'')}</pre><b>Vendor</b><pre>${esc(f.RMK_VNDR||'')}</pre><b>Remark</b><pre>${esc(f.RMK||'')}</pre></div>`;
      if(S.needsDockLink(v)) renderDockLink();}
    previewModal.hidden=false;document.body.style.overflow='hidden';
  }
  /* `DK_CD 미설정` 을 미리보기 안에서 바로 푼다.
   * 🔴 전엔 프로젝트 **생성 화면**에만 입력칸이 있어서, 비워 두고 만든 프로젝트는 상신이
   *    영구히 불가였다(라이브 프로젝트 2건 다 그랬다). 후보는 맥 러너가 SVMS `SP_GET_DOCK`
   *    으로 채워 둔 캐시이고, 열린 후보가 딱 1건이면 서버가 이미 자동연결해 둔다. */
  async function renderDockLink(){
    const host=$('#dd-svms-dock-link'); if(!host||!state.project) return;
    host.innerHTML='<p class="dd-field-help">SVMS 입거(Dock) 후보 조회 중…</p>';
    let info;
    try{info=await api(`/api/dock-daily/projects/${state.project.id}/svms-docks`);}
    catch(e){host.innerHTML=`<p class="dd-field-help">Dock 후보를 불러오지 못했습니다: ${esc(e.message||String(e))}</p>`;return;}
    if(info.locked){host.innerHTML=`<p class="dd-field-help">${esc(info.locked_reason||'Dock 연결이 잠겨 있습니다.')}</p>`;return;}
    const S=window.DockDailySVMS,cands=info.candidates||[];
    if(!cands.length){host.innerHTML=`<p class="dd-field-help">SVMS 입거 후보가 아직 없습니다(선박 ${esc(info.vsl_cd||'')}). 맥 러너가 SVMS를 조회한 뒤 다시 열어보세요.</p>`;return;}
    host.innerHTML=`<div class="dd-svms-dock"><label class="form-field"><span class="form-label">SVMS 입거(Dock) 연결</span><select id="dd-dock-select" class="dd-select">${cands.map(c=>`<option value="${esc(c.dk_cd)}" data-open="${c.open===false?'0':'1'}"${c.dk_cd===info.dk_cd?' selected':''}>${esc(S.candidateLabel(c))}</option>`).join('')}</select></label><button type="button" class="dd-btn" id="dd-dock-bind">이 Dock에 연결</button> <span id="dd-dock-status" class="dd-field-help"></span></div>`;
    $('#dd-dock-bind').onclick=()=>bindDockCd();
  }
  async function bindDockCd(){
    const sel=$('#dd-dock-select'),st=$('#dd-dock-status'),btn=$('#dd-dock-bind');
    if(!sel||!state.project)return;
    const opt=sel.options[sel.selectedIndex];
    const dk=sel.value,label=opt.textContent;
    /* 🔴 종료된 입거도 고를 수는 있게 두되(SVMS 상태가 늦게 닫히는 일이 있다) 경고는 반드시
       띄운다. 앱과 같은 문구다 -- 조용히 붙이면 남의 끝난 dock 에 daily report 가 쌓인다. */
    const warn=opt.dataset.open==='0'
      ? '\n\n⚠️ 이 입거는 SVMS에서 이미 종료(출거·완료)된 것으로 보입니다.'
      : '';
    if(!confirm(`이 프로젝트의 SVMS 입거를 다음으로 연결할까요?\n\n${label}${warn}\n\n이후 이 프로젝트의 Daily Report는 이 Dock에 저장됩니다.`))return;
    btn.disabled=true;st.textContent='연결 중…';
    try{
      await api(`/api/dock-daily/projects/${state.project.id}/svms-dk-cd`,
                {...json({dk_cd:dk,confirmation:'user_selected_dock'}),method:'POST'});
      state.project.svms_dk_cd=dk;
      state.projects=(state.projects||[]).map(p=>p.id===state.project.id?state.project:p);
      await openPreview('svms');            // DK_CD·publishable 을 서버에서 다시 읽는다
      notice('SVMS 입거(Dock)를 연결했습니다.');
    }catch(e){btn.disabled=false;st.textContent='연결 실패: '+(e.message||String(e));}
  }
  async function copyEmail(){
    const v=state.preview?.data;if(!v)return;const plain=`제목: ${v.subject}\n\n${v.text}`;
    try{if(window.ClipboardItem&&navigator.clipboard?.write){await navigator.clipboard.write([new ClipboardItem({'text/plain':new Blob([plain],{type:'text/plain'}),'text/html':new Blob([`<div style="font-family:Arial,Helvetica,sans-serif;font-size:11pt;line-height:1.5;color:#222"><p style="margin:0"><span style="font-family:Arial,Helvetica,sans-serif;font-size:11pt"><b>제목: ${esc(v.subject)}</b></span></p><p style="margin:0;line-height:1.5"><span style="font-family:Arial,Helvetica,sans-serif;font-size:11pt">&nbsp;</span></p></div>${v.html}`],{type:'text/html'})})]);}else await navigator.clipboard.writeText(plain);$('#dd-preview-status').textContent='전체 내용이 복사되었습니다.';}catch(e){$('#dd-preview-status').textContent='복사 실패: '+e.message;}
  }
  async function pushSvms(){
    if(!confirm('현재 미리보기 내용으로 SVMS 입거 Daily Report에 반영할까요?'))return;
    const btn=$('#dd-svms-push');btn.disabled=true;$('#dd-preview-status').textContent='SVMS 반영 요청 중…';
    const rid=state.report.id;
    try{
      const v=await api(`/api/dock-daily/reports/${rid}/svms-publish`,{...json({confirmation:'user_preview_approved'}),method:'POST'});
      $('#dd-preview-status').textContent=v.message||'SVMS 반영 대기열 등록 완료 — 맥 runner가 저장·첨부 후 결과를 이 화면에 표시합니다.';
      // 🔴 재조회 **전에** 로컬 상태를 잠근다(올마이트 blocking). 아래 재조회가 실패하면
      //    화면은 여전히 `preview_only` 라서 버튼이 다시 열리고, 그 두 번째 클릭이 SVMS 에
      //    중복 행을 만든다(`SP_SET_DOCK_DR` 비멱등).
      applySvmsAck(rid,v);
      // 🔴 상신 뒤 보고서를 다시 읽는다. 상신은 `svms_sync_status` 를 바꾸고 그 값이 상태
      //    배지·상신 버튼 활성을 결정한다. 다시 읽지 않으면 모달을 닫아도 화면이 "SVMS
      //    미반영" 그대로여서 같은 버튼을 다시 누르게 된다(= 중복 저장 시도).
      //    다시 읽기가 실패해도 상신 자체는 성공했으므로 에러로 뒤집지 않는다.
      try{
        const fresh=await api(`/api/dock-daily/reports/${rid}`);
        if(state.report&&state.report.id===rid){state.report=fresh;ensureSectionEditors();renderSvmsState();}
      }catch(_){}
    }
    catch(e){$('#dd-preview-status').textContent=e.message;}
    finally{
      // 🔴 무조건 되살리지 않는다. 성공했으면 이 보고서는 더 이상 상신 대상이 아니다
      //    (`SP_SET_DOCK_DR` 는 멱등이 아니라 같은 날짜 재저장이 새 seq 행을 만든다).
      btn.disabled=!svmsPublishAllowed();
    }
  }
  /* 상신·수동확인 2xx 응답을 로컬(열린 보고서 + 목록 행)에 반영한다. 앱
   * `applyPublishAck` 와 같은 규칙이다.
   * 🔴 상태를 내리지 않는다 — 서버가 status 를 안 줬으면 큐에 들어간 것으로 보고
   *    `approved` 로 접는다(모르면 잠그는 쪽이 안전한 방향). */
  function applySvmsAck(rid,resp){
    const S=window.DockDailySVMS;
    const status=S.normalize(resp&&resp.status)==='preview_only'?'approved':S.normalize(resp&&resp.status);
    const seq=String((resp&&resp.dk_seq)||'').trim();
    if(state.report&&state.report.id===rid){
      state.report.svms_sync_status=status;
      if(seq) state.report.svms_dk_seq=seq;
      renderSvmsState();
    }
    // 목록 꼬리도 같이 바꾼다. 안 바꾸면 드롭다운이 "미반영" 으로 남는다.
    const row=(state.reports||[]).find(r=>r.id===rid);
    if(row){row.svms_sync_status=status; if(seq) row.svms_dk_seq=seq; renderReportDates();}
  }
  /* `unknown`/`partial` 을 형이 SVMS 화면에서 본 결과로 닫는다. 이 경로가 없으면 그 두
   * 상태는 영구 고착이다(재상신은 상태로 막혀 있고 상태를 내릴 방법이 없다). */
  async function reconcileSvms(saved){
    const r=state.report; if(!r) return;
    const rid=r.id; let seq=null;
    if(saved){
      // 🔴 DK_SEQ 없이 반영됨으로 닫으면 SVMS 의 어느 행인지 영구히 모른다.
      seq=(prompt('SVMS 화면에서 확인한 DK_SEQ를 입력하세요 (예: 2)',String(r.svms_dk_seq||'').trim())||'').trim();
      if(!seq) return;
    }else if(!confirm('SVMS 입거수리 Daily Report 목록에 그 날짜 행이 없는 것을 확인했나요? 기록하면 다시 상신할 수 있게 됩니다.')){
      return;
    }
    try{
      const body={confirmation:'user_checked_svms',resolution:saved?'synced':'not_saved'};
      if(saved) body.dk_seq=seq;
      const v=await api(`/api/dock-daily/reports/${rid}/svms-reconcile`,{...json(body),method:'POST'});
      applySvmsAck(rid,v);
      notice(saved?`SVMS 반영됨으로 기록했습니다 (DK_SEQ ${v.dk_seq||seq}).`
                  :'SVMS에 저장되지 않은 것으로 기록했습니다. 다시 상신할 수 있습니다.');
      try{
        const fresh=await api(`/api/dock-daily/reports/${rid}`);
        if(state.report&&state.report.id===rid){state.report=fresh;ensureSectionEditors();renderSvmsState();}
      }catch(_){}
    }catch(e){ err(e); }
  }
  /* 지금 열린 보고서를 상신할 수 있는가. 미리보기 계약(publishable)과 반영 상태 둘 다 봐야
   * 한다 — 계약만 보면 이미 상신한 보고서에서 버튼이 계속 열린다. */
  /* 🔴 `status` 도 봐야 한다. 전엔 계약+반영상태만 봐서 **편집 중인 보고서에서도 버튼이
     활성**이었고, 누르면 서버가 409 `final_required` 로 거절했다(화면이 거짓말). */
  function svmsPublishBlock(){
    const v=state.preview&&state.preview.kind==='svms'?state.preview.data:null;
    if(!v||!state.report) return 'contract';
    return window.DockDailySVMS.publishBlockReason(
      {publishable:v.publishable, status:state.report.status, sync:state.report.svms_sync_status});
  }
  function svmsPublishAllowed(){ return !svmsPublishBlock(); }
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

  // 보고서 날짜 정정. 새 라우트가 아니라 기존 PUT 을 탄다 — BEGIN IMMEDIATE·revision
  // CAS·확정잠금·revision bump·스냅샷을 그대로 물려받는다. 삭제 후 재생성으로 고치면
  // 본문과 첨부가 함께 날아가므로 제자리 정정이어야 한다.
  const dateModal=$('#dd-date-modal');
  function closeDateModal(){dateModal.hidden=true;document.body.style.overflow='';}
  async function openDateModal(){
    if(!state.report)return; clearErr();
    const rid=state.report.id, locked=state.report.status==='final';
    // 미리보기·업로드와 같은 계약: 남은 편집을 먼저 저장한다. 날짜 정정도 같은 PUT 을
    // 타고 revision 을 올리므로, 안 저장하면 그 다음 저장이 409 로 막힌다.
    if(state.dirty&&!locked)await save();
    if(state.report?.id!==rid)return;          // await 사이에 다른 일자로 옮겨갔다
    $('#dd-date-new').value=state.report.report_date||'';
    $('#dd-date-error').textContent=locked?CONFLICT.final_locked:'';
    $('#dd-date-save').disabled=locked;
    dateModal.hidden=false;document.body.style.overflow='hidden';$('#dd-date-new').focus();
  }
  async function saveReportDate(){
    if(!state.report)return;
    // 프로젝트·보고서 id 를 먼저 고정한다. await 뒤에 state 를 다시 읽으면, 그 사이
    // 형이 다른 프로젝트로 옮겨갔을 때 남의 목록을 덮어쓴다(올마이트 지적 2026-08-21).
    const rid=state.report.id, pid=state.project.id, next=$('#dd-date-new').value;
    if(!next){$('#dd-date-error').textContent='새 보고서 일자를 선택하세요.';return;}
    if(next===state.report.report_date){closeDateModal();return;}
    const btn=$('#dd-date-save'); btn.disabled=true; $('#dd-date-error').textContent='';
    try{
      await api(`/api/dock-daily/reports/${rid}`,
        {...json({revision:state.report.revision,operations:[],report_date:next}),method:'PUT'});
      const rows=await api(`/api/dock-daily/projects/${pid}/reports`);
      closeDateModal();
      if(state.project?.id!==pid)return;        // 옮겨간 화면은 건드리지 않는다
      state.reports=rows;
      if(state.report?.id===rid)await selectReport(rid); else renderReportDates();
      // 자동수집을 새 날짜로 다시 돌리지 않는 건 의도다 — 돌리면 사람이 고친 본문을 덮는다.
      notice(`보고서 일자를 ${next} 로 변경했습니다. 자동수집 블록과 원천 링크는 다시 수집하지 않습니다.`);
    }catch(e){$('#dd-date-error').textContent=conflictText(e);}
    finally{btn.disabled=state.report?.status==='final';}
  }
  // 이전 일자 가져오기. 자동초안을 폐기한 대신 들어온 경로다(형 지시 2026-08-21) —
  // 입거공사는 전날 작업이 그대로 이어지는 날이 많아서, 자동으로 만든 문구보다
  // 어제 형이 쓴 문장을 복사해 고치는 쪽이 실제 작업 방식에 맞는다.
  const copyModal=$('#dd-copy-modal');
  function closeCopyModal(){copyModal.hidden=true;document.body.style.overflow='';}
  function copyTargetNote(){
    const n=(state.report?.blocks||[]).length;
    return n?`이 보고서에는 이미 카드 ${n}개가 있습니다. "가져오기"는 그 카드를 지우고 덮어씁니다.`
            :'이 보고서는 아직 비어 있습니다.';
  }
  async function openCopyModal(){
    if(!state.report)return; clearErr();
    const rid=state.report.id, locked=state.report.status==='final';
    // 날짜 정정·미리보기와 같은 계약: 남은 편집을 먼저 저장한다. 가져오기도 revision 을
    // 올리므로 저장하지 않으면 그 다음 저장이 409 로 막힌다.
    if(state.dirty&&!locked)await save();
    if(state.report?.id!==rid)return;            // await 사이에 다른 일자로 옮겨갔다
    // 기능 이름이 "이전 일자" 다. 목록도 실제로 앞선 날짜만 준다(올마이트 지적 2026-08-21) —
    // 아직 오지 않은 날짜에서 당겨오는 건 "이어지는 작업" 이 아니다. 서버는 방향을 따지지
    // 않으므로(일반 복사 라우트) 이 제한은 화면 계약이고, 첫 일자에서는 후보가 0 이 된다.
    const today=state.report.report_date;
    const others=(state.reports||[]).filter(r=>r.id!==rid&&r.report_date<today)
      .sort((a,b)=>b.report_date.localeCompare(a.report_date));
    const sel=$('#dd-copy-src');
    sel.innerHTML=others.map(r=>`<option value="${esc(r.id)}">${esc(r.report_date)} · ${esc(r.status)}</option>`).join('')
      ||'<option value="">이 일자보다 앞선 보고서가 없습니다</option>';
    sel.disabled=!others.length;
    // 확정본에서 가져오는 건 막지 않는다(읽기다). 확정본으로 가져오는 것만 막힌다.
    const blocked=locked||!others.length;
    $('#dd-copy-run').disabled=blocked; $('#dd-copy-append').disabled=blocked;
    $('#dd-copy-target').textContent=copyTargetNote();
    $('#dd-copy-error').textContent=locked?CONFLICT.final_locked:'';
    copyModal.hidden=false;document.body.style.overflow='hidden';sel.focus();
  }
  async function runCopy(mode){
    if(!state.report)return;
    // 날짜 정정과 같은 이유로 id 를 먼저 고정한다 — await 뒤 state 를 다시 읽으면
    // 그 사이 옮겨간 화면을 덮어쓴다.
    const rid=state.report.id, pid=state.project.id, src=+$('#dd-copy-src').value;
    if(!src){$('#dd-copy-error').textContent='가져올 보고서를 선택하세요.';return;}
    if(mode==='replace'&&(state.report.blocks||[]).length
       &&!confirm('이 보고서에 이미 쓴 카드를 지우고 고른 일자의 내용으로 덮어씁니다. 계속할까요?'))return;
    const run=$('#dd-copy-run'),app=$('#dd-copy-append');
    run.disabled=true;app.disabled=true;$('#dd-copy-error').textContent='';
    try{
      const out=await api(`/api/dock-daily/reports/${rid}/copy-from`,
        {...json({revision:state.report.revision,source_report_id:src,mode}),method:'POST'});
      const rows=await api(`/api/dock-daily/projects/${pid}/reports`);
      closeCopyModal();
      if(state.project?.id!==pid)return;
      state.reports=rows;
      if(state.report?.id===rid)await selectReport(rid); else renderReportDates();
      const from=(rows.find(r=>r.id===src)||{}).report_date||src;
      // skipped_blocks 는 이미지 카드와 "지금 프로젝트에 없는 섹션" 의 카드 수다. 첨부는
      // 애초에 세지 않으므로 첨부라고 말하면 거짓이다(올마이트 지적 2026-08-21).
      const skipped=out.skipped_blocks?` 이미지 카드와 없어진 섹션의 카드 ${out.skipped_blocks}개는 따라오지 않았습니다.`:'';
      notice(out.copied_blocks
        ?`${from} 보고서에서 카드 ${out.copied_blocks}개를 ${mode==='append'?'뒤에 붙였습니다':'가져왔습니다'}.${skipped}`
        :`${from} 보고서에 가져올 카드가 없었습니다.${skipped}`);
    }catch(e){$('#dd-copy-error').textContent=conflictText(e);}
    finally{const locked=state.report?.status==='final';run.disabled=locked;app.disabled=locked;}
  }
  $('#dd-copy-from').onclick=()=>openCopyModal().catch(err);
  $('#dd-copy-run').onclick=()=>runCopy('replace').catch(e=>{$('#dd-copy-error').textContent=conflictText(e);});
  $('#dd-copy-append').onclick=()=>runCopy('append').catch(e=>{$('#dd-copy-error').textContent=conflictText(e);});
  $('#dd-copy-close').onclick=closeCopyModal; $('#dd-copy-cancel').onclick=closeCopyModal;

  $('#dd-date-edit').onclick=()=>openDateModal().catch(err);
  $('#dd-date-save').onclick=()=>saveReportDate().catch(e=>{$('#dd-date-error').textContent=conflictText(e);});
  $('#dd-date-close').onclick=closeDateModal; $('#dd-date-cancel').onclick=closeDateModal;

  // 드롭다운·필터. 저장 안 한 편집이 있으면 물어보고, 이동을 취소하면 선택값을
  // 열린 일자로 되돌린다(안 되돌리면 목록과 본문이 어긋난다).
  $('#dd-report-select').onchange=e=>{
    const id=+e.target.value;
    if(!id||id===state.report?.id||!canLeaveDraft()){renderReportDates();return;}
    selectReport(id).catch(err);
  };
  $('#dd-report-search').oninput=renderReportDates;
  $('#dd-report-status').onchange=renderReportDates;
  // once() 의 finally 가 버튼을 되살리므로, 삭제 뒤 열린 보고서가 없으면
  // 다시 잠글 기회를 준다 — 누를 대상이 없는 버튼이 살아 있으면 안 된다.
  $('#dd-report-del').onclick=()=>{const id=state.report?.id;if(id)once($('#dd-report-del'),()=>deleteReport(id)).then(renderReportDates);};

  // 🔴 저장은 `once()` 로 막고 오류는 `conflictText` 로 번역한다. 없으면 ①두 번 누른 두
  // 번째 PUT 이 같은 revision 을 들고 가 409 를 받고, 저장은 됐는데 형은 "revision
  // conflict" 라는 영어 원문을 본다 ②진짜 충돌에서도 다른 버튼들이 주는 안내가 안 뜬다.
  $('#dd-save').onclick=()=>once($('#dd-save'),()=>save().catch(e=>{$('#dd-error').textContent=conflictText(e);}));
  $('#dd-email').onclick=()=>openPreview('email').catch(err); $('#dd-svms').onclick=()=>openPreview('svms').catch(err);
  // 확정 / 확정취소 한 버튼 토글.  잠금을 여는 쪽은 전용 라우트를 쓴다 —
  // PUT(=내용 저장) 은 잠긴 행에서 409 로 막히고, 그 거절이 곧 잠금이다.
  async function setReportStatus(want){
    if(!state.report)return;
    const ask=want==='final'
      ? '이 보고서를 확정하면 수정이 잠깁니다. 확정할까요?'
      : '확정을 취소하면 다시 수정할 수 있게 됩니다. 확정을 취소할까요?';
    if(!confirm(ask))return;
    // 🔴 어느 보고서를 확정하는지 `await` **전에** 고정한다(`openDateModal` 과 같은 계약).
    // 저장이 도는 동안 형이 다른 일자로 옮기면 `state.report` 는 그쪽이 되고, POST 는
    // **형이 확정한다고 답하지 않은 보고서**를 잠근다.
    const rid=state.report.id;
    try{
      // 확정 전에만 저장한다. 확정취소 시점엔 잠겨 있어서 저장할 편집 자체가 없다.
      if(want==='final'&&state.dirty&&!await save())return;
      if(state.report?.id!==rid)return;        // await 사이에 다른 일자로 옮겨갔다
      const updated=await api(`/api/dock-daily/reports/${rid}/status`,
        {...json({status:want,revision:state.report.revision}),method:'POST'});
      // 사이드바는 state.reports 의 status 를 그대로 찍는다. 여기서 갈아주지
      // 않으면 본문은 편집 가능인데 목록만 'final' 로 남는다.
      state.reports=state.reports.map(r=>r.id===updated.id?{...r,status:updated.status,revision:updated.revision}:r);
      await selectReport(updated.id);
    }catch(e){err(e);}
  }
  $('#dd-final').onclick=()=>setReportStatus(state.report?.status==='final'?'editing':'final');
  // 🔴 여기도 `canLeaveDraft()` 를 물어본다. 드롭다운·프로젝트 목록은 묻는데 이 버튼만
  // 안 물어서, 카드에 글을 쓰다가 새 일자를 만들면 `selectReport` 가 `state.report` 를
  // 갈아치우며 방금 쓴 글이 **확인 한 번 없이** 사라졌다.
  $('#dd-generate').onclick=async()=>{if(!state.project)return;const report_date=$('#dd-generate-date').value;if(!report_date)return err(new Error('보고서 일자를 선택하세요.'));if(!canLeaveDraft())return;const pid=state.project.id;try{const report=await api(`/api/dock-daily/projects/${pid}/reports/generate`,{...json({report_date}),method:'POST'});const reports=await api(`/api/dock-daily/projects/${pid}/reports`);if(state.project?.id!==pid)return;state.reports=reports;await selectReport(report.id);}catch(e){err(e);}};
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

  const projectModal=$('#dd-project-modal'),projectForm=$('#dd-project-form'),projectError=$('#ddp-error');
  async function openProjectModal(){projectForm.reset();projectError.textContent='';try{if(!state.vessels.length)state.vessels=await api('/api/vessels');$('#ddp-vessel').innerHTML='<option value="">활성 선박을 선택하세요</option>'+state.vessels.map(v=>`<option value="${v.id}">${esc(v.name)}${v.vsl_cd?` · ${esc(v.vsl_cd)}`:''}</option>`).join('');projectModal.hidden=false;document.body.style.overflow='hidden';$('#ddp-vessel').focus();}catch(e){err(e);}}
  function closeProjectModal(){projectModal.hidden=true;document.body.style.overflow='';}
  $('#dd-new-project').onclick=openProjectModal;$('#dd-project-close').onclick=closeProjectModal;$('#dd-project-cancel').onclick=closeProjectModal;
  document.addEventListener('keydown',e=>{if(e.key!=='Escape')return;if(!projectModal.hidden)closeProjectModal();else if(!copyModal.hidden)closeCopyModal();else if(!dateModal.hidden)closeDateModal();else if(!previewModal.hidden)closePreview();else if(!$('#dd-file-modal').hidden)closeFilePreview();else if(!uploadModal.hidden)closeUploadModal();});
  // 자동초안 폐기(형 지시 2026-08-21) 이후 프로젝트 생성은 자동작성 관련 값을 아예
  // 보내지 않는다. 서버 기본값이 auto_generate=0 이라 컬럼은 그대로 두고 꺼진다.
  projectForm.onsubmit=async e=>{e.preventDefault();projectError.textContent='';if(!projectForm.reportValidity())return;const button=$('#dd-project-create');button.disabled=true;button.textContent='생성 중…';try{const created=await api('/api/dock-daily/projects',{...json({vessel_id:Number($('#ddp-vessel').value),title:$('#ddp-title').value.trim(),berthing_date:$('#ddp-berthing').value||null,dock_in_date:$('#ddp-dock-in').value||null,dock_out_date:$('#ddp-dock-out').value||null,departure_date:$('#ddp-departure').value||null,svms_dk_cd:$('#ddp-svms-dk').value.trim()||null,special_sections:$('#ddp-egcs').checked?[{section_key:'egcs',label:'EGCS Retrofit',enabled:true}]:[]}),method:'POST'});closeProjectModal();await loadProjects();await selectProject(created.id);}catch(error){projectError.textContent=error.message||String(error);}finally{button.disabled=false;button.textContent='프로젝트 생성';}};
  window.addEventListener('beforeunload',event=>{if(state.dirty){event.preventDefault();event.returnValue='';}});
  // 폭이 바뀌면 줄바꿈이 다시 계산되므로 필요한 높이도 달라진다. 창 리사이즈 대신 카드
  // 컨테이너를 직접 관찰하는 이유는 두 가지다: (1) 창 크기와 무관한 레이아웃 변화도 잡는다,
  // (2) display:none 이던 보고서가 보이는 순간 폭이 0→N 으로 바뀌며 여기서 걸린다 —
  // 숨은 동안 autoGrow 가 scrollHeight 0 을 보고 포기한 카드를 이때 다시 잰다
  // (올마이트 지적 2026-08-21: 재측정 경로 없음).
  // 폭만 보는 이유: 높이를 바꾸는 게 growAll 자신이라 높이까지 보면 스스로를 계속 다시 부른다.
  let lastGrowWidth=-1;
  new ResizeObserver(entries=>{const w=entries[entries.length-1].contentRect.width;if(w===lastGrowWidth)return;lastGrowWidth=w;if(w)growAll();}).observe($('#dd-report'));
  loadProjects().catch(err);
})();
