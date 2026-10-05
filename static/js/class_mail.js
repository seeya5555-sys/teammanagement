(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let data;
  let busy = false;
  const notify = (msg, kind) => {
    $('cmNotice').textContent = msg;
    if (window.InlineEdit) InlineEdit.toast(msg, kind);
  };
  const pad = n => String(n).padStart(2, '0');
  // datetime-local 값('YYYY-MM-DDTHH:MM' 또는 ':SS' 포함) → 'YYYY-MM-DD HH:MM:SS'. 잘못되면 null.
  function toStamp(v) {
    const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/.exec(v || '');
    if (!m) return null;
    const d = new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +(m[6] || 0));
    if (isNaN(d) || d.getMonth() !== +m[2] - 1 || d.getDate() !== +m[3]) return null;
    return `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}:${m[6] || '00'}`;
  }
  function nowLocal() {
    // 입력은 한국시간 — 브라우저 timezone 무관하게 KST(UTC+9) 기준
    const d = new Date(Date.now() + 9 * 3600 * 1000);
    return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}T${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}`;
  }
  async function api(path, method = 'GET', body) {
    const response = await fetch('/api/class-mail' + path, {method, headers: {'Content-Type':'application/json'}, body: body === undefined ? undefined : JSON.stringify(body)});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
    return result;
  }
  const table = (headers, rows) => rows.length ? '<table class="cm-table"><thead><tr>' + headers.map(h => `<th>${esc(h)}</th>`).join('') + '</tr></thead><tbody>' + rows.join('') + '</tbody></table>' : '<p class="cm-muted">없음</p>';
  function preview() {
    const vessel = data?.vessels.find(v => String(v.vessel_id) === $('cmSelect').value);
    $('cmPreview').textContent = vessel ? `To: ${vessel.to_emails || '-'}\nCC: ${vessel.cc_emails || '-'}\nSubject: ${vessel.subject}\n\n${vessel.body}\n\n[Outlook 서명: 자동화 / 첨부: 영문 Class Status.xlsx]` : '대상 선박 없음';
  }
  let loadedOnce = false;
  async function load() {
    if (!loadedOnce) for (const id of ['cmRuns', 'cmReplies']) $(id).innerHTML = '<p class="cm-muted">불러오는 중…</p>';
    data = await api('/status');
    loadedOnce = true;
    const vessels = data.vessels;
    $('cmVessels').innerHTML = table(['선박','COC / 기국','가장 가까운 Due','주기 · 다음 대상일','To / CC / Dear','자동발송'], vessels.map(v => `<tr><td>${esc(v.name)}</td><td>${v.coc} / ${v.statutory}</td><td>${esc(v.nearest_due || '-')}</td><td>${esc(v.reason)}<br>${esc(v.next_send || '-')}<br><span class="cm-error">${esc(v.blocker)}</span></td><td>${esc(v.to_emails || '-')}<br>${esc(v.cc_emails || '-')}<br>Dear ${esc(v.dear_name || 'Sir/Madam')}</td><td><label><input type="checkbox" data-vid="${v.vessel_id}" ${v.enabled ? 'checked' : ''} ${busy ? 'disabled' : ''}> ON</label></td></tr>`));
    $('cmSubject').value = data.template.subject_tpl;
    $('cmBody').value = data.template.body_tpl;
    const selected = $('cmSelect').value;
    $('cmSelect').innerHTML = vessels.filter(v => v.count).map(v => `<option value="${v.vessel_id}">${esc(v.name)}</option>`).join('');
    if (vessels.some(v => String(v.vessel_id) === selected && v.count)) $('cmSelect').value = selected;
    preview();
    $('cmRuns').innerHTML = table(['선박','주차','상태','발송','결과'], data.runs.map(r => `<tr><td>${esc(r.vessel_name)}</td><td>${esc(r.iso_week)}</td><td>${esc({sending:'발송 확인 대기', sent:'발송 완료', failed:'실패·확인 필요'}[r.state] || r.state)}</td><td>${esc(r.sent_at || '-')}</td><td>${esc(r.error || '')}${r.state !== 'sent' ? `<br><button data-recover="${r.id}">보낸 편지함 확인 후 복구</button>` : ''}</td></tr>`));
    $('cmReplies').innerHTML = table(['선박','발신자','수신','조치사항 반영','비고'], data.replies.map(r => `<tr><td>${esc(r.vessel_name)}</td><td>${esc(r.sender)}</td><td>${esc(r.received_at || r.created_at)}</td><td>${r.updated}건</td><td>${esc(r.note)}</td></tr>`));
  }
  $('cmSelect').addEventListener('change', preview);
  $('cmVessels').addEventListener('change', async event => {
    const toggle = event.target.closest('input[data-vid]');
    if (!toggle || busy) return;
    if (toggle.checked && !confirm('이 선박의 Class Status 메일 자동발송을 켤까요? 표시된 수신처·템플릿으로 월요일 07:30 실제 메일이 나갑니다.')) { toggle.checked = false; return; }
    busy = true; toggle.disabled = true;
    try { await api(`/settings/${toggle.dataset.vid}/enabled`, 'POST', {enabled: toggle.checked}); notify(toggle.checked ? '자동발송 ON 저장됨' : '자동발송 OFF 저장됨'); }
    catch (error) { notify('저장 실패: ' + error.message, 'error'); }
    finally { busy = false; await load().catch(error => { $('cmNotice').textContent = error.message; }); }
  });
  $('cmRuns').addEventListener('click', async event => {
    const cancel = event.target.closest('button[data-recover-cancel]');
    if (cancel) { cancel.closest('.cm-recover').remove(); return; }
    const ok = event.target.closest('button[data-recover-ok]');
    if (ok) { await submitRecover(ok); return; }
    const button = event.target.closest('button[data-recover]');
    if (!button || busy) return;
    const cell = button.parentElement;
    if (cell.querySelector('.cm-recover')) { cell.querySelector('.cm-recover input').focus(); return; }
    if (!confirm('Outlook 보낸 편지함에서 이 주차·선박의 실제 발송을 확인했나요? 재발송하지 않고 발송완료로 복구하여 회신 처리를 허용합니다.')) return;
    // prompt() 대신 그 자리 입력칸 — 실제 발송시각(한국시간)
    const box = document.createElement('div');
    box.className = 'cm-recover';
    box.innerHTML = `<label class="cm-muted">실제 발송시각(한국시간)</label><input type="datetime-local" step="1" max="${nowLocal()}" aria-label="Outlook에 표시된 실제 발송시각"><button type="button" class="btn btn-primary btn-sm" data-recover-ok="${esc(button.dataset.recover)}">복구</button><button type="button" class="btn btn-outline btn-sm" data-recover-cancel>취소</button>`;
    cell.appendChild(box);
    const input = box.querySelector('input');
    input.addEventListener('keydown', e => {
      if (e.key === 'Enter') { e.preventDefault(); submitRecover(box.querySelector('[data-recover-ok]')); }
      else if (e.key === 'Escape') { box.remove(); }
    });
    input.focus();
  });
  async function submitRecover(ok) {
    if (busy) return;
    const box = ok.closest('.cm-recover');
    const input = box.querySelector('input');
    const stamp = toStamp(input.value);
    if (!stamp) { notify('발송시각을 YYYY-MM-DD HH:MM:SS 형식으로 입력하세요', 'error'); input.focus(); return; }
    if (stamp > toStamp(nowLocal())) { notify('발송시각이 현재보다 미래입니다', 'error'); input.focus(); return; }
    busy = true;
    box.querySelectorAll('input,button').forEach(x => { x.disabled = true; });
    try { await api(`/runs/${ok.dataset.recoverOk}/recover-sent`, 'POST', {confirmed_sent: true, sent_at: stamp}); notify('발송 확인 복구됨 · 재발송 없음'); }
    catch (error) { notify('복구 실패: ' + error.message, 'error'); }
    finally { busy = false; await load().catch(error => { notify(error.message, 'error'); }); }
  }
  $('cmSave').addEventListener('click', async () => {
    if (busy) return;
    busy = true; $('cmSave').disabled = true;
    try { await api('/template', 'PUT', {subject_tpl: $('cmSubject').value, body_tpl: $('cmBody').value}); notify('템플릿 저장됨'); await load(); }
    catch (error) { notify('저장 실패: ' + error.message, 'error'); }
    finally { busy = false; $('cmSave').disabled = false; }
  });
  load().catch(error => { $('cmVessels').innerHTML = `<p class="cm-error">${esc(error.message)}</p>`; notify(error.message, 'error'); });
})();
