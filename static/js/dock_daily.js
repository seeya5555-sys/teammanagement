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
    const option = (r, tail) => `<option value="${esc(r.id)}"${r.id===open?' selected':''}>${esc(r.report_date)} · ${esc(r.status)}${tail||''}</option>`;
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
  async function selectProject(id) {
    clearErr(); state.project = state.projects.find(p => p.id === id) || null; if (!state.project) return;
    state.report = null; state.reports = await api(`/api/dock-daily/projects/${id}/reports`); await loadProjects();
    // 프로젝트를 바꿀 때 필터를 비운다. 안 비우면 앞 프로젝트에 맞춰둔 검색어가
    // 새 프로젝트의 일자를 전부 숨겨 "보고서가 없다" 처럼 보인다.
    $('#dd-report-search').value = ''; $('#dd-report-status').value = '';
    $('#dd-project-tools').style.display = 'block'; $('#dd-generate-date').value = today(); renderReportDates();
    const specials = (state.project.sections||[]).filter(s => s.kind === 'special');
    // 표는 다른 카드의 하위항목이 아니라 **제목을 가진 자기 섹션**이다(형 지시 2026-08-21).
    // 그 섹션이 곧 special 섹션이므로 새 저장소도, 새 라우트도 필요 없다 — 메일에서도
    // 다른 섹션과 같은 `N. 제목` 머리글을 받는다.
    const add = '<div class="dd-row" style="margin-top:9px"><input class="dd-input" id="dd-section-label" placeholder="새 섹션 제목 (예: 비용 정산표)" maxlength="60" style="margin:0"><button class="dd-btn alt" id="dd-section-add" type="button">＋ 섹션</button></div>'
      + '<p class="dd-muted dd-block-note">섹션은 이 프로젝트의 모든 일자에 생깁니다. 비어 있는 날은 메일에 NIL 로 나갑니다. 지우려면 체크를 해제하세요.</p>';
    $('#dd-special-tools').innerHTML = (specials.length ? '<b>Special 항목</b>'+specials.map(s => `<label style="display:block;margin-top:7px"><input type="checkbox" class="dd-special-toggle" data-key="${esc(s.section_key)}" ${s.enabled?'checked':''}> ${esc(s.label)}</label>`).join('') : '<span class="dd-muted">Special 항목 없음</span>') + add;
    document.querySelectorAll('.dd-special-toggle').forEach(t => t.onchange = () => toggleSpecial(t.dataset.key, t.checked));
    $('#dd-section-add').onclick = () => addSection($('#dd-section-label').value);
    $('#dd-empty').style.display='block'; $('#dd-report').classList.remove('show'); if (state.reports.length) await selectReport(state.reports[0].id);
  }
  function ensureSectionEditors() {
    for (const s of (state.report.sections||[]).filter(x => x.enabled)) {
      // 표만 있는 섹션도 글 칸이 필요하다 — 표는 본문 문장을 대신하지 못하고, 웹에서
      // 표는 편집 대상이 아니다(앱과 같은 판정: DockDailySectionEditing.needsTextDraft).
      if (!(state.report.blocks||[]).some(b => !b._delete && b.section_key === s.section_key && isTextBlock(b))) {
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
    renderReportDates(); renderItinerary(); renderSections(); renderAttachments();
    const locked = state.report.status === 'final'; ['#dd-save','#dd-attach'].forEach(s => $(s).disabled=locked);
    // 확정 버튼만은 잠긴 상태에서도 살아있어야 한다 — 잠금을 여는 유일한 통로다.
    // 여기서 disabled 를 걸면 확정된 보고서는 영구히 잠긴다.
    const finalBtn=$('#dd-final'); finalBtn.disabled=false;
    finalBtn.textContent=locked?'확정취소':'확정';
    // '취소' 한 단어는 옆의 '저장' 과 붙어 "편집 취소" 로 읽힌다.
    finalBtn.classList.toggle('warn',locked); finalBtn.classList.toggle('alt',!locked);
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
  function blockText(b){const c=b.content||{};return c.title||c.body||c.text||'';}
  // 표·이미지는 읽기 전용으로 보여준다(메일 렌더와 같은 모양). 편집은 앱에서 한다.
  function readOnlyBlock(b){
    const c=b.content||{};
    if(b.block_type==='table'){
      const head=(c.columns||[]).map(v=>`<th>${esc(String(v))}</th>`).join('');
      const body=(c.rows||[]).filter(Array.isArray).map(r=>`<tr>${r.map(v=>`<td>${esc(String(v))}</td>`).join('')}</tr>`).join('');
      return `<table class="dd-block-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table><p class="dd-muted dd-block-note">표 내용은 앱에서 편집합니다.</p>`;
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
      return `<div class="dd-img-cell">${img}<p class="dd-img-cap">${esc(caption)||'&nbsp;'}</p></div>`;
    }).join('');
    return `<div class="dd-img-grid" style="grid-template-columns:repeat(${cols},1fr)">${cells}</div>`
      +`<p class="dd-muted dd-block-note">사진 ${items.length}장 · ${cols}열 · 사진은 앱에서 편집합니다.</p>`;
  }
  // Cards carry their own "1) " numbering so what the supervisor types is what
  // the Outlook mail shows. The renderers strip any stored number before
  // applying their own, so a renumbered card never double-numbers.
  const NUM=window.DockDailyNumbering;
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
  function commitItems(ta){
    const b=findBlock(ta.dataset.key); if(!b||blockText(b)===ta.value)return;
    // block_type 을 'paragraph' 로 갈아치우지 않는다 — item 블록이 문단으로 바뀌면서
    // 서버가 content 를 통째로 교체해 progress/status 가 사라졌다(올마이트 지적).
    b.content={...(b.content||{}),[textKey(b)]:ta.value}; b._edit=true; state.dirty=true;
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
      const body=isTextBlock(b)
        ?`<textarea class="dd-section-edit" data-key="${key}" placeholder="${esc(s.label)} 내용을 입력하세요" ${locked?'disabled':''}>${esc(blockText(b))}</textarea>`
        :readOnlyBlock(b);
      return `<div class="dd-block-editor">${meta}${body}</div>`}).join('')}</div>`;
    }).join('');
    document.querySelectorAll('.dd-section-edit').forEach(bindItemNumbering);
    document.querySelectorAll('.delete-inline').forEach(btn=>btn.onclick=()=>{const b=findBlock(btn.dataset.key);if(!b)return;
      // 표·이미지는 웹에서 다시 만들 수 없고(편집기는 앱에만 있다), 이미지 블록을 지우면
      // 서버가 연결된 첨부까지 함께 지운다. 한 번 확인을 받는다.
      if(!isTextBlock(b)&&!confirm(b.block_type==='image'?'이미지 블록을 삭제할까요?\n\n연결된 첨부파일도 함께 삭제되고, 이미지 블록은 앱에서만 다시 만들 수 있습니다.':'표 블록을 삭제할까요?\n\n표는 앱에서만 다시 만들 수 있습니다.'))return;if(b._new)state.report.blocks=state.report.blocks.filter(x=>x!==b);else b._delete=true;state.dirty=true;ensureSectionEditors();renderSections();});
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
  async function save() {
    if(!state.report)return; clearErr();
    const itinerary={}; document.querySelectorAll('.dd-itinerary-date').forEach(i=>itinerary[i.dataset.key]=i.value||null);
    if(Object.keys(itinerary).some(k=>(state.project[k]||null)!==itinerary[k])){
      const updated=await api(`/api/dock-daily/projects/${state.project.id}`,{...json(itinerary),method:'PATCH'});
      state.project=updated; state.projects=state.projects.map(p=>p.id===updated.id?updated:p);
    }
    const operations=[];
    state.report.blocks.filter(b=>b._delete).forEach(b=>operations.push({op:'delete',id:b.id}));
    state.report.blocks.filter(b=>!b._delete&&isTextBlock(b)&&(b._new||b._edit)&&(!b._new||blockText(b).trim())).forEach(b=>operations.push({op:'upsert',id:b._new?undefined:b.id,section_key:b.section_key,block_type:b.block_type||'paragraph',content:{...(b.content||{})},sort_order:b.sort_order||0}));
    state.report=await api(`/api/dock-daily/reports/${state.report.id}`,{...json({revision:state.report.revision,operations}),method:'PUT'}); state.dirty=false; ensureSectionEditors();
    $('#dd-report-meta').textContent=`${state.report.report_date} · ${state.report.status} · revision ${state.report.revision}`; renderItinerary(); renderSections(); renderAttachments();
  }
  // section_key 는 서버가 만든다(POST .../sections). 웹과 앱이 각자 키를 만들면 규칙이
  // 두 벌이 되고 서로 다른 키를 뱉는다 -- 제목만 보내고 키는 받는다.
  async function addSection(label){
    const name=String(label||'').trim();
    if(!name){err(new Error('섹션 제목을 입력하세요.'));return;}
    clearErr();
    try{
      const updated=await api(`/api/dock-daily/projects/${state.project.id}/sections`,{...json({label:name}),method:'POST'});
      state.project=updated;state.projects=state.projects.map(p=>p.id===updated.id?updated:p);
      $('#dd-section-label').value='';
      // 섹션 목록을 다시 그린다. 열린 보고서에도 바로 카드가 생겨야 한다.
      const specials=(updated.sections||[]).filter(x=>x.kind==='special');
      $('#dd-special-tools').innerHTML='<b>Special 항목</b>'+specials.map(x=>`<label style="display:block;margin-top:7px"><input type="checkbox" class="dd-special-toggle" data-key="${esc(x.section_key)}" ${x.enabled?'checked':''}> ${esc(x.label)}</label>`).join('')
        +'<div class="dd-row" style="margin-top:9px"><input class="dd-input" id="dd-section-label" placeholder="새 섹션 제목 (예: 비용 정산표)" maxlength="60" style="margin:0"><button class="dd-btn alt" id="dd-section-add" type="button">＋ 섹션</button></div>'
        +'<p class="dd-muted dd-block-note">섹션은 이 프로젝트의 모든 일자에 생깁니다. 비어 있는 날은 메일에 NIL 로 나갑니다. 지우려면 체크를 해제하세요.</p>';
      document.querySelectorAll('.dd-special-toggle').forEach(t=>t.onchange=()=>toggleSpecial(t.dataset.key,t.checked));
      $('#dd-section-add').onclick=()=>addSection($('#dd-section-label').value);
      if(state.report){state.report.sections=updated.sections;ensureSectionEditors();renderSections();}
      notice(`섹션 "${name}" 을 추가했습니다.`);
    }catch(error){err(error);}
  }
  async function toggleSpecial(key,enabled){const updated=await api(`/api/dock-daily/projects/${state.project.id}`,{...json({sections:[{section_key:key,enabled}]}),method:'PATCH'});state.project=updated;state.projects=state.projects.map(p=>p.id===updated.id?updated:p);if(state.report){state.report.sections=updated.sections;ensureSectionEditors();renderSections();}}

  const previewModal=$('#dd-preview-modal');
  function closePreview(){previewModal.hidden=true;document.body.style.overflow='';state.preview=null;}
  async function openPreview(kind){
    if(state.dirty)await save(); const v=await api(`/api/dock-daily/reports/${state.report.id}/${kind}-preview`); state.preview={kind,data:v};
    $('#dd-preview-title').textContent=kind==='email'?'이메일 미리보기':'SVMS 미리보기'; $('#dd-preview-status').textContent='';
    $('#dd-copy-all').hidden=kind!=='email'; $('#dd-svms-push').hidden=kind!=='svms';
    if(kind==='email') $('#dd-preview-content').innerHTML=`<div class="dd-email-subject"><b>제목</b><br>${esc(v.subject)}</div><div class="dd-email-html">${v.html}</div>`;
    else {const f=v.fields||{},push=$('#dd-svms-push');push.disabled=!v.publishable;push.title=v.publishable?'미리보기 내용을 SVMS에 반영':'SVMS 저장 계약과 byte limit 검증 전에는 반영할 수 없습니다.';$('#dd-preview-status').textContent=v.publishable?'':'실제 푸싱은 SVMS 저장 계약 검증 후 활성화됩니다.';$('#dd-preview-content').innerHTML=`<p class="dd-modal-intro"><b>${v.publishable?'SVMS 반영 준비 완료':'Preview only 안전게이트'}</b><br>DK_CD와 byte limit 계약이 모두 확인되어야 실제 반영됩니다.<br>표·사진은 SVMS 본문에 넣지 않습니다(이메일 본문에만 나갑니다).</p><div class="dd-svms-grid"><b>DK_CD</b><pre>${esc(f.DK_CD||'')}</pre><b>DR_DT</b><pre>${esc(f.DR_DT||'')}</pre><b>Shipyard</b><pre>${esc(f.RMK_SYD||'')}</pre><b>Vendor</b><pre>${esc(f.RMK_VNDR||'')}</pre><b>Remark</b><pre>${esc(f.RMK||'')}</pre></div>`;}
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

  $('#dd-save').onclick=()=>save().catch(err); $('#dd-email').onclick=()=>openPreview('email').catch(err); $('#dd-svms').onclick=()=>openPreview('svms').catch(err);
  // 확정 / 확정취소 한 버튼 토글.  잠금을 여는 쪽은 전용 라우트를 쓴다 —
  // PUT(=내용 저장) 은 잠긴 행에서 409 로 막히고, 그 거절이 곧 잠금이다.
  async function setReportStatus(want){
    if(!state.report)return;
    const ask=want==='final'
      ? '이 보고서를 확정하면 수정이 잠깁니다. 확정할까요?'
      : '확정을 취소하면 다시 수정할 수 있게 됩니다. 확정을 취소할까요?';
    if(!confirm(ask))return;
    try{
      // 확정 전에만 저장한다. 확정취소 시점엔 잠겨 있어서 저장할 편집 자체가 없다.
      if(want==='final'&&state.dirty)await save();
      const updated=await api(`/api/dock-daily/reports/${state.report.id}/status`,
        {...json({status:want,revision:state.report.revision}),method:'POST'});
      // 사이드바는 state.reports 의 status 를 그대로 찍는다. 여기서 갈아주지
      // 않으면 본문은 편집 가능인데 목록만 'final' 로 남는다.
      state.reports=state.reports.map(r=>r.id===updated.id?{...r,status:updated.status,revision:updated.revision}:r);
      await selectReport(updated.id);
    }catch(e){err(e);}
  }
  $('#dd-final').onclick=()=>setReportStatus(state.report?.status==='final'?'editing':'final');
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
