(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let data;
  let busy = false;
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
  async function load() {
    data = await api('/status');
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
    try { await api(`/settings/${toggle.dataset.vid}/enabled`, 'POST', {enabled: toggle.checked}); $('cmNotice').textContent = 'ON/OFF 저장됨'; }
    catch (error) { $('cmNotice').textContent = error.message; }
    finally { busy = false; await load().catch(error => { $('cmNotice').textContent = error.message; }); }
  });
  $('cmRuns').addEventListener('click', async event => {
    const button = event.target.closest('button[data-recover]');
    if (!button || busy) return;
    if (!confirm('Outlook 보낸 편지함에서 이 주차·선박의 실제 발송을 확인했나요? 재발송하지 않고 발송완료로 복구하여 회신 처리를 허용합니다.')) return;
    const stamp = prompt('Outlook에 표시된 실제 발송시각 (한국시간 YYYY-MM-DD HH:MM:SS)');
    if (!stamp) return;
    busy = true; button.disabled = true;
    try { await api(`/runs/${button.dataset.recover}/recover-sent`, 'POST', {confirmed_sent: true, sent_at: stamp}); $('cmNotice').textContent = '발송 확인 복구됨 · 재발송 없음'; }
    catch (error) { $('cmNotice').textContent = error.message; }
    finally { busy = false; await load().catch(error => { $('cmNotice').textContent = error.message; }); }
  });
  $('cmSave').addEventListener('click', async () => {
    if (busy) return;
    busy = true; $('cmSave').disabled = true;
    try { await api('/template', 'PUT', {subject_tpl: $('cmSubject').value, body_tpl: $('cmBody').value}); $('cmNotice').textContent = '템플릿 저장됨'; await load(); }
    catch (error) { $('cmNotice').textContent = error.message; }
    finally { busy = false; $('cmSave').disabled = false; }
  });
  load().catch(error => { $('cmNotice').textContent = error.message; });
})();
