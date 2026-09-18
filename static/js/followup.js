/* Operator-selected evidence. Never changes approval/finding/issue state. */
(() => {
  'use strict';
  const names={daily:'후속 약속',cs:'조치증빙',vt:'조치증빙',aor:'협의 변경점',fundreq:'협의 변경점',invoice:'협의 변경점'};
  const states={queued:'조회 대기',running:'조회 중',candidate:'확인 필요한 후보',unsearchable:'연결 근거 부족 · 증빙 부재 아님',error:'조회 실패 · 회신 없음으로 판단하지 않음',stale:'원본 변경 · 다시 조회 필요'};
  let dialog, timer, seq=0;
  function el(tag,text,cls){const e=document.createElement(tag);if(text)e.textContent=text;if(cls)e.className=cls;return e;}
  async function api(url,body){const r=await fetch(url,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const d=await r.json().catch(()=>({error:'서버 응답을 읽지 못했습니다. 다시 시도하세요.'}));if(!r.ok)throw Error(d.error||'조회 실패');return d;}
  function button(kind,id){const b=el('button',names[kind]+' 확인','btn btn-outline btn-sm');b.type='button';b.dataset.followupBadge=kind+':'+id;paintBadge(b,kind,id);b.addEventListener('click',e=>{e.stopPropagation();open(kind,id);});return b;}
  function ensure(){if(dialog)return;dialog=el('dialog',null,'followup-dialog');dialog.style.cssText='max-width:720px;width:92vw;max-height:88vh;overflow:auto;border:1px solid #888;border-radius:12px;padding:22px;background:var(--bg,#fff);color:var(--text,#222)';document.body.append(dialog);dialog.addEventListener('close',()=>{clearTimeout(timer);seq++;});}
  async function open(kind,id){ensure();clearTimeout(timer);const token=++seq;dialog.replaceChildren();const head=el('div');head.style.cssText='display:flex;justify-content:space-between;gap:12px';head.append(el('h3',names[kind]+' · Outlook 근거'));const close=el('button','닫기','btn btn-outline btn-sm');close.type='button';close.onclick=()=>dialog.close();head.append(close);dialog.append(head);const box=el('div','불러오는 중…');dialog.append(box);if(!dialog.open)dialog.showModal();
    const base=`/api/followup/${kind}/${id}`;
    try {const data=await api(base);if(token!==seq)return;box.replaceChildren();const c=data.context;box.append(el('p',`${c.vessel_name} · ${c.title||c.reference}`));
      box.append(el('p','선택한 메일 대화만 확인합니다. 메일 내용은 주장·후보이며, 승인·상신·종결·금액은 변경하지 않습니다.'));
      const baseline=el('details');baseline.append(el('summary','비교 기준 · 현재 TRMT 기록'));const labels={item_topic:'현안',description:'상세',due_date:'기한',status:'현재 상태',no:'Finding 번호',item:'항목',remark:'비고',user_remark:'감독 메모',inspection_date:'검사일',report_number:'보고서 번호',subj:'제목',subject:'제목',amt:'금액',cur_cd:'통화',vndr_nm:'업체',proposed_comment:'검토 초안',aor_cd:'AOR',opex_cd:'비용청구 번호',inv_no:'송장번호',ref_no:'참조번호'};for(const [key,label] of Object.entries(labels)){const val=c.record[key];if(val!==null&&val!==undefined&&val!=='')baseline.append(el('p',label+': '+val));}if(c.record.actions){try{const actions=typeof c.record.actions==='string'?JSON.parse(c.record.actions):c.record.actions;for(const a of actions)baseline.append(el('p',`${a.date||''} ${a.progress||''}`));}catch{}}box.append(baseline);
      const label=el('label','관련 메일 제목');const input=el('input');input.type='text';input.maxLength=300;input.style.cssText='display:block;width:100%;box-sizing:border-box;margin:8px 0;padding:8px';input.placeholder='Outlook 관련 메일 제목을 붙여넣으세요';input.value=data.tracking?.search_subject||data.job?.context_subject||c.search_subject||'';label.append(input);box.append(label);
      const scan=el('button','메일 근거 조회','btn btn-primary btn-sm');scan.type='button';box.append(scan);const track=el('button','', 'btn btn-outline btn-sm');track.type='button';box.append(track);const trackStatus=el('p');box.append(trackStatus);
      let tracking=data.tracking;
      track.onclick=async()=>{track.disabled=true;try{await api(base+'/tracking',{enabled:!tracking?.enabled,search_subject:input.value.trim(),fingerprint:c.fingerprint});await refresh();}catch(e){status.textContent=e.message;}finally{track.disabled=false;}};
      const status=el('p');box.append(status);const result=el('div');box.append(result);
      function render(d){tracking=d.tracking;track.textContent=tracking?.enabled?'선택 추적 끄기':'이 항목만 6시간마다 재조회';trackStatus.textContent=tracking?.enabled?('선택 추적 중 · 다음 확인 '+tracking.next_check):tracking?.reason||'선택 추적은 최대 10건. 원본 수정 시 중지됩니다.';result.replaceChildren();const j=d.job;const pending=j&&['queued','running'].includes(j.state);scan.disabled=!!pending;status.textContent=j?`${states[j.state]||j.state}${j.checked_at?' · 확인 '+j.checked_at:''}`:'아직 조회하지 않음';if(!j)return;
        if(j.stale){result.append(el('p','기존 결과는 원본 수정으로 숨겼습니다. 재조회하세요.'));return;}
        const r=j.result||{};if(r.source_subject)result.append(el('p','선택 메일: '+r.source_subject));if(r.coverage)result.append(el('p',r.coverage));if(r.summary)result.append(el('p',r.summary));
        if(j.changes){result.append(el('p',`새 근거 ${j.changes.new} · 기존 근거 ${j.changes.repeat} · 최초 확인 ${j.changes.first}`));result.append(el('p',`기준 이후 새 포착 ${j.changes.observed_after||0} · 기준 이전 포착 ${j.changes.observed_before||0}`));result.append(el('p','포착시각 = TRMT에 해당 인용이 처음 저장된 시각(한국시간). 실제 메일 수신시각과는 다릅니다. 오래된 메일도 처음 추출되면 새 포착입니다.'));}
        if(j.comparison_baseline)result.append(el('p',(j.comparison_baseline_kind==='review'?'검토 기준시각: ':'이전 조회 기준시각: ')+j.comparison_baseline+' (한국시간)'));
        const repeated=el('details');repeated.append(el('summary','이전 조회와 같은 근거 펼치기'));
        for(const item of r.items||[]){const section=el('section');section.style.cssText='border-left:3px solid #d99b26;padding:8px 12px;margin:12px 0';section.append(el('strong',item.label+(item.change==='repeat'?' · 기존 근거':'')));section.append(el('p',item.interpretation));const q=el('blockquote',item.quote);q.style.cssText='white-space:pre-wrap;font-size:12px;margin:8px 0';section.append(q);
          if(item.first_seen_at)section.append(el('p','최초 포착: '+item.first_seen_at.replace('T',' ').replace('+09:00',' KST')));
          const observed={after:'기준 이후 새로 포착',before:'기준 이전부터 확인된 근거',initial:'첫 조회 · 다음 비교의 기준',unknown:'포착시각 비교 기준 없음'};
          section.append(el('p',observed[item.observation_status]||observed.unknown));
          if(item.received_after_baseline!==null&&item.received_after_baseline!==undefined)section.append(el('p',item.received_after_baseline?'별도 확인: 기준 이후 메일 수신':'별도 확인: 기준 이전 메일 수신'));
          if(item.source?.received_at)section.append(el('p','메일 수신: '+item.source.received_at));
          const decisions={confirmed:'확인함 · 완료판정 아님',excluded:'제외함',applied:'Daily 진행이력 반영함'};
          if(item.review)section.append(el('p',decisions[item.review.decision]+' · '+item.review.reviewed_at));
          for(const [decision,title] of [['confirmed','근거 확인'],['excluded','후보 제외'],...(kind==='daily'?[['applied','이 인용을 진행이력에 추가']]:[])]){
            const b=el('button',title,'btn btn-outline btn-sm');b.type='button';b.disabled=item.review?.decision===decision||item.review?.decision==='applied';
            b.onclick=async()=>{if(decision==='applied'&&!window.confirm('아래 인용을 오늘의 Daily 진행이력에 추가합니다. 완료 상태나 기한은 변경하지 않습니다.\n\n'+item.quote))return;b.disabled=true;try{await api(base+'/items/review',{job_id:j.job_id,item_id:item.item_id,decision});if(decision==='applied'){await open(kind,id);}else{await refresh();}}catch(e){status.textContent=e.message;b.disabled=false;}};section.append(b);
          }
          if(item.change==='repeat'&&item.review)repeated.append(section);else result.append(section);}
        if(repeated.children.length>1)result.append(repeated);
        if((r.attachments||[]).length){result.append(el('strong','첨부 이름 후보 · 내용 미검증'));const ul=el('ul');for(const a of r.attachments)ul.append(el('li',a));result.append(ul);}
        if(j.state==='candidate'){const review=el('button',j.reviewed_at?'검토함 · '+j.reviewed_at:'근거 검토함 표시','btn btn-outline btn-sm');review.type='button';review.disabled=!!j.reviewed_at;review.onclick=async()=>{review.disabled=true;try{await api(base+'/reviewed',{job_id:j.job_id});await refresh();}catch(e){status.textContent=e.message;review.disabled=false;}};result.append(review);result.append(el('p','이 표시는 근거를 읽었다는 기록이며, 조치완료·승인 확인이 아닙니다.'));}
        if(j.state==='queued'){const cancel=el('button','조회 대기 취소','btn btn-outline btn-sm');cancel.type='button';cancel.onclick=async()=>{try{await api(base+'/cancel',{job_id:j.job_id});await refresh();}catch(e){status.textContent=e.message;}};result.append(cancel);}
        if(pending&&j.delayed){result.append(el('p','조회가 지연되고 있습니다. 자동화 상태 탭을 확인하세요. 다른 작업이 실행 중일 수 있습니다.'));const retry=el('button','상태 새로고침','btn btn-outline btn-sm');retry.type='button';retry.onclick=refresh;result.append(retry);}
        if(pending&&!j.delayed){clearTimeout(timer);timer=setTimeout(refresh,7000);}
      }
      async function refresh(){try{const d=await api(base);if(token===seq&&dialog.open){render(d);loadBadges();}}catch(e){if(token===seq)status.textContent=e.message;}}
      scan.onclick=async()=>{scan.disabled=true;status.textContent='조회 요청 중…';try{await api(base+'/scan',{search_subject:input.value.trim(),fingerprint:c.fingerprint});await refresh();}catch(e){status.textContent=e.message;scan.disabled=false;}};
      render(data);
    }catch(e){if(token===seq)box.textContent=e.message;}
  }
  let badgeCounts={};
  function paintBadge(b,kind,id){const count=badgeCounts[kind+':'+id];b.textContent=names[kind]+' 확인'+(count?' · 미확인 '+count:'');}
  async function loadBadges(){try{const d=await api('/api/followup/indicators');badgeCounts=Object.fromEntries(d.items.map(x=>[x.kind+':'+x.target_id,x.count]));document.querySelectorAll('[data-followup-kind],[data-followup-badge]').forEach(b=>{const pair=b.dataset.followupBadge?.split(':');paintBadge(b,b.dataset.followupKind||pair[0],b.dataset.followupId||pair[1]);});}catch{}}
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',loadBadges);else loadBadges();
  window.Followup={button,open};
  document.addEventListener('click',e=>{const b=e.target.closest('[data-followup-kind]');if(b){e.preventDefault();e.stopPropagation();open(b.dataset.followupKind,Number(b.dataset.followupId));}});
})();
