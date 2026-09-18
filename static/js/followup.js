/* Operator-selected evidence. Never changes approval/finding/issue state. */
(() => {
  'use strict';
  const names={daily:'후속 약속',cs:'조치증빙',vt:'조치증빙',aor:'협의 변경점',fundreq:'협의 변경점',invoice:'협의 변경점'};
  const states={queued:'조회 대기',running:'조회 중',candidate:'확인 필요한 후보',unsearchable:'연결 근거 부족 · 증빙 부재 아님',error:'조회 실패 · 회신 없음으로 판단하지 않음',stale:'원본 변경 · 다시 조회 필요'};
  let dialog, timer, seq=0;
  function el(tag,text,cls){const e=document.createElement(tag);if(text)e.textContent=text;if(cls)e.className=cls;return e;}
  async function api(url,body){const r=await fetch(url,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const d=await r.json().catch(()=>({error:'서버 응답을 읽지 못했습니다. 다시 시도하세요.'}));if(!r.ok)throw Error(d.error||'조회 실패');return d;}
  function button(kind,id){const b=el('button',names[kind]+' 확인','btn btn-outline btn-sm');b.type='button';b.addEventListener('click',e=>{e.stopPropagation();open(kind,id);});return b;}
  function ensure(){if(dialog)return;dialog=el('dialog',null,'followup-dialog');dialog.style.cssText='max-width:720px;width:92vw;max-height:88vh;overflow:auto;border:1px solid #888;border-radius:12px;padding:22px;background:var(--bg,#fff);color:var(--text,#222)';document.body.append(dialog);dialog.addEventListener('close',()=>{clearTimeout(timer);seq++;});}
  async function open(kind,id){ensure();clearTimeout(timer);const token=++seq;dialog.replaceChildren();const head=el('div');head.style.cssText='display:flex;justify-content:space-between;gap:12px';head.append(el('h3',names[kind]+' · Outlook 근거'));const close=el('button','닫기','btn btn-outline btn-sm');close.type='button';close.onclick=()=>dialog.close();head.append(close);dialog.append(head);const box=el('div','불러오는 중…');dialog.append(box);if(!dialog.open)dialog.showModal();
    const base=`/api/followup/${kind}/${id}`;
    try {const data=await api(base);if(token!==seq)return;box.replaceChildren();const c=data.context;box.append(el('p',`${c.vessel_name} · ${c.title||c.reference}`));
      box.append(el('p','선택한 메일 대화만 확인합니다. 메일 내용은 주장·후보이며, 승인·상신·종결·금액은 변경하지 않습니다.'));
      const baseline=el('details');baseline.append(el('summary','비교 기준 · 현재 TRMT 기록'));const labels={item_topic:'현안',description:'상세',due_date:'기한',status:'현재 상태',no:'Finding 번호',item:'항목',remark:'비고',user_remark:'감독 메모',inspection_date:'검사일',report_number:'보고서 번호',subj:'제목',subject:'제목',amt:'금액',cur_cd:'통화',vndr_nm:'업체',proposed_comment:'검토 초안',aor_cd:'AOR',opex_cd:'비용청구 번호',inv_no:'송장번호',ref_no:'참조번호'};for(const [key,label] of Object.entries(labels)){const val=c.record[key];if(val!==null&&val!==undefined&&val!=='')baseline.append(el('p',label+': '+val));}if(c.record.actions){try{const actions=typeof c.record.actions==='string'?JSON.parse(c.record.actions):c.record.actions;for(const a of actions)baseline.append(el('p',`${a.date||''} ${a.progress||''}`));}catch{}}box.append(baseline);
      const label=el('label','관련 메일 제목');const input=el('input');input.type='text';input.maxLength=300;input.style.cssText='display:block;width:100%;box-sizing:border-box;margin:8px 0;padding:8px';input.placeholder='Outlook 관련 메일 제목을 붙여넣으세요';input.value=data.job?.context_subject||c.search_subject||'';label.append(input);box.append(label);
      const scan=el('button','메일 근거 조회','btn btn-primary btn-sm');scan.type='button';box.append(scan);const status=el('p');box.append(status);const result=el('div');box.append(result);
      function render(d){result.replaceChildren();const j=d.job;const pending=j&&['queued','running'].includes(j.state);scan.disabled=!!pending;status.textContent=j?`${states[j.state]||j.state}${j.checked_at?' · 확인 '+j.checked_at:''}`:'아직 조회하지 않음';if(!j)return;
        if(j.stale){result.append(el('p','기존 결과는 원본 수정으로 숨겼습니다. 재조회하세요.'));return;}
        const r=j.result||{};if(r.source_subject)result.append(el('p','선택 메일: '+r.source_subject));if(r.coverage)result.append(el('p',r.coverage));if(r.summary)result.append(el('p',r.summary));
        for(const item of r.items||[]){const section=el('section');section.style.cssText='border-left:3px solid #d99b26;padding:8px 12px;margin:12px 0';section.append(el('strong',item.label));section.append(el('p',item.interpretation));const q=el('blockquote',item.quote);q.style.cssText='white-space:pre-wrap;font-size:12px;margin:8px 0';section.append(q);result.append(section);}
        if((r.attachments||[]).length){result.append(el('strong','첨부 이름 후보 · 내용 미검증'));const ul=el('ul');for(const a of r.attachments)ul.append(el('li',a));result.append(ul);}
        if(j.state==='candidate'){const review=el('button',j.reviewed_at?'검토함 · '+j.reviewed_at:'근거 검토함 표시','btn btn-outline btn-sm');review.type='button';review.disabled=!!j.reviewed_at;review.onclick=async()=>{review.disabled=true;try{await api(base+'/reviewed',{job_id:j.job_id});await refresh();}catch(e){status.textContent=e.message;review.disabled=false;}};result.append(review);result.append(el('p','이 표시는 근거를 읽었다는 기록이며, 조치완료·승인 확인이 아닙니다.'));}
        if(j.state==='queued'){const cancel=el('button','조회 대기 취소','btn btn-outline btn-sm');cancel.type='button';cancel.onclick=async()=>{try{await api(base+'/cancel',{job_id:j.job_id});await refresh();}catch(e){status.textContent=e.message;}};result.append(cancel);}
        if(pending&&j.delayed){result.append(el('p','조회가 지연되고 있습니다. 자동화 상태 탭을 확인하세요. 다른 작업이 실행 중일 수 있습니다.'));const retry=el('button','상태 새로고침','btn btn-outline btn-sm');retry.type='button';retry.onclick=refresh;result.append(retry);}
        if(pending&&!j.delayed){clearTimeout(timer);timer=setTimeout(refresh,7000);}
      }
      async function refresh(){try{const d=await api(base);if(token===seq&&dialog.open)render(d);}catch(e){if(token===seq)status.textContent=e.message;}}
      scan.onclick=async()=>{scan.disabled=true;status.textContent='조회 요청 중…';try{await api(base+'/scan',{search_subject:input.value.trim(),fingerprint:c.fingerprint});await refresh();}catch(e){status.textContent=e.message;scan.disabled=false;}};
      render(data);
    }catch(e){if(token===seq)box.textContent=e.message;}
  }
  window.Followup={button,open};
  document.addEventListener('click',e=>{const b=e.target.closest('[data-followup-kind]');if(b){e.preventDefault();e.stopPropagation();open(b.dataset.followupKind,Number(b.dataset.followupId));}});
})();
