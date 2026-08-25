(function () {
  'use strict';

  const originalCsvUpload = window.uploadJobsCSV;

  function money(value) {
    return '$' + Number(value || 0).toLocaleString(undefined, {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2
    });
  }

  function escapeHtml(value) {
    return String(value == null ? '' : value).replace(/[&<>'"]/g, ch => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'
    })[ch]);
  }

  function closePreview() {
    const modal = document.getElementById('yard-xlsx-preview');
    if (modal) modal.remove();
  }

  function showPreview(data, onApply) {
    closePreview();
    const rows = (data.jobs || []).map(job => `
      <tr>
        <td>${escapeHtml(job.number)}</td><td>${escapeHtml(job.section)}</td>
        <td>${escapeHtml(job.description)}</td><td style="text-align:right">${money(job.budget)}</td>
      </tr>`).join('');
    const warnings = (data.warnings || []).map(item => `<li>${escapeHtml(item)}</li>`).join('');
    const modal = document.createElement('div');
    modal.id = 'yard-xlsx-preview';
    modal.innerHTML = `
      <style>
        #yard-xlsx-preview{position:fixed;inset:0;z-index:10000;background:#0f172acc;display:flex;align-items:center;justify-content:center;padding:24px}
        #yard-xlsx-preview .yx-card{background:#fff;color:#172033;width:min(920px,96vw);max-height:88vh;overflow:auto;border-radius:14px;box-shadow:0 24px 70px #0008;padding:24px}
        #yard-xlsx-preview .yx-head{display:flex;justify-content:space-between;gap:16px;align-items:start}
        #yard-xlsx-preview h3{margin:0 0 5px;font-size:20px} #yard-xlsx-preview p{margin:4px 0;color:#64748b;font-size:13px}
        #yard-xlsx-preview .yx-stats{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:18px 0}
        #yard-xlsx-preview .yx-stat{border:1px solid #e2e8f0;border-radius:9px;padding:10px;background:#f8fafc}
        #yard-xlsx-preview .yx-stat b{display:block;font-size:16px;margin-top:4px}
        #yard-xlsx-preview table{width:100%;border-collapse:collapse;font-size:12px} #yard-xlsx-preview th,#yard-xlsx-preview td{padding:7px;border-bottom:1px solid #e2e8f0;text-align:left}
        #yard-xlsx-preview .yx-warn{color:#9a6700;background:#fff7d6;border-radius:8px;padding:8px 12px;font-size:12px}
        #yard-xlsx-preview .yx-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:18px}
        #yard-xlsx-preview button{border:1px solid #cbd5e1;background:#fff;border-radius:8px;padding:9px 16px;cursor:pointer;font-weight:700}
        #yard-xlsx-preview .yx-apply{background:#17233a;color:#fff;border-color:#17233a}
        @media(max-width:680px){#yard-xlsx-preview .yx-stats{grid-template-columns:1fr 1fr}}
      </style>
      <div class="yx-card" role="dialog" aria-modal="true" aria-labelledby="yx-title">
        <div class="yx-head"><div><h3 id="yx-title">조선소 견적서 파싱 미리보기</h3>
          <p>${escapeHtml(data.sheet)} 시트 · 기존 진행률/소비액/비고/수동분류는 보존됨</p></div>
          <button type="button" data-action="close" aria-label="닫기">×</button></div>
        <div class="yx-stats">
          <div class="yx-stat">Job 리스트<b>${Number(data.job_count || 0).toLocaleString()}개</b></div>
          <div class="yx-stat">금액 있는 Job<b>${Number(data.priced_count || 0).toLocaleString()}개</b></div>
          <div class="yx-stat">Gross Budget<b>${money(data.gross_total)}</b></div>
          <div class="yx-stat">Final D/C ${data.discount_rate == null ? '-' : escapeHtml(data.discount_rate) + '%'}<b>${money(data.after_discount)}</b></div>
        </div>
        ${warnings ? `<ul class="yx-warn">${warnings}</ul>` : ''}
        <table><thead><tr><th>No.</th><th>Section</th><th>Description (앞 12개)</th><th style="text-align:right">Budget</th></tr></thead><tbody>${rows}</tbody></table>
        <div class="yx-actions"><button type="button" data-action="close">취소</button><button type="button" class="yx-apply" data-action="apply">Job Progress에 반영</button></div>
      </div>`;
    document.body.appendChild(modal);
    modal.querySelectorAll('[data-action="close"]').forEach(button => button.onclick = closePreview);
    modal.querySelector('[data-action="apply"]').onclick = async event => {
      event.currentTarget.disabled = true;
      event.currentTarget.textContent = '반영 중…';
      await onApply();
    };
  }

  function showSvmsPreview(data, onApply) {
    closePreview();
    const rows = (data.jobs || []).map(job => `
      <tr><td>${escapeHtml(job.number)}</td><td>${escapeHtml(job.category)}</td>
        <td>${escapeHtml(job.description)}</td><td>${escapeHtml(job.vendor)}</td>
        <td style="text-align:right">${money(job.budget)}</td></tr>`).join('');
    const warnings = (data.warnings || []).map(item => `<li>${escapeHtml(item)}</li>`).join('');
    const counts = data.counts || {};
    const modal = document.createElement('div');
    modal.id = 'yard-xlsx-preview';
    modal.innerHTML = `
      <style>
        #yard-xlsx-preview{position:fixed;inset:0;z-index:10000;background:#0f172acc;display:flex;align-items:center;justify-content:center;padding:24px}
        #yard-xlsx-preview .yx-card{background:#fff;color:#172033;width:min(980px,96vw);max-height:88vh;overflow:auto;border-radius:14px;box-shadow:0 24px 70px #0005;padding:22px}
        #yard-xlsx-preview .yx-head{display:flex;justify-content:space-between;gap:20px;align-items:start}
        #yard-xlsx-preview h3{margin:0 0 5px;font-size:20px} #yard-xlsx-preview p{margin:4px 0;color:#64748b;font-size:13px}
        #yard-xlsx-preview .yx-stats{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin:18px 0}
        #yard-xlsx-preview .yx-stat{border:1px solid #e2e8f0;border-radius:9px;padding:10px;background:#f8fafc;font-size:12px}
        #yard-xlsx-preview .yx-stat b{display:block;font-size:16px;margin-top:4px}
        #yard-xlsx-preview table{width:100%;border-collapse:collapse;font-size:12px} #yard-xlsx-preview th,#yard-xlsx-preview td{padding:7px;border-bottom:1px solid #e2e8f0;text-align:left}
        #yard-xlsx-preview .yx-warn{color:#9a6700;background:#fff7d6;border-radius:8px;padding:8px 12px;font-size:12px}
        #yard-xlsx-preview .yx-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:18px}
        #yard-xlsx-preview button{border:1px solid #cbd5e1;background:#fff;border-radius:8px;padding:9px 16px;cursor:pointer;font-weight:700}
        #yard-xlsx-preview .yx-apply{background:#17233a;color:#fff;border-color:#17233a}
        @media(max-width:680px){#yard-xlsx-preview .yx-stats{grid-template-columns:1fr 1fr}}
      </style>
      <div class="yx-card" role="dialog" aria-modal="true" aria-labelledby="yx-title">
        <div class="yx-head"><div><h3 id="yx-title">SVMS 입거수리 기안 미리보기</h3>
          <p>Job No/제목·Vendor·SVMS 환산 USD Budget 반영 · 기존 진행률/소비액/날짜/비고 보존</p></div>
          <button type="button" data-action="close" aria-label="닫기">×</button></div>
        <div class="yx-stats">
          <div class="yx-stat">PAINT<b>${Number(counts.Paint || 0)}</b></div>
          <div class="yx-stat">SPARE<b>${Number(counts.Spare || 0)}</b></div>
          <div class="yx-stat">STORE<b>${Number(counts.Store || 0)}</b></div>
          <div class="yx-stat">SHORE REPAIR<b>${Number(counts['Shore Repair'] || 0)}</b></div>
          <div class="yx-stat">Total Budget<b>${money(data.total_budget)}</b></div>
        </div>
        ${warnings ? `<ul class="yx-warn">${warnings}</ul>` : ''}
        <table><thead><tr><th>No.</th><th>Category</th><th>Description (앞 20개)</th><th>Vendor</th><th style="text-align:right">Budget (USD)</th></tr></thead><tbody>${rows}</tbody></table>
        <div class="yx-actions"><button type="button" data-action="close">취소</button><button type="button" class="yx-apply" data-action="apply">Job Progress에 반영</button></div>
      </div>`;
    document.body.appendChild(modal);
    modal.querySelectorAll('[data-action="close"]').forEach(button => button.onclick = closePreview);
    modal.querySelector('[data-action="apply"]').onclick = async event => {
      event.currentTarget.disabled = true;
      event.currentTarget.textContent = '반영 중…';
      await onApply();
    };
  }

  async function waitForSvmsPreview(token) {
    for (let attempt = 0; attempt < 100; attempt += 1) {
      const response = await fetch(`${API}/vessels/${VID}/jobs/svms-import/status/${encodeURIComponent(token)}`);
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'SVMS 가져오기 상태 확인 실패');
      if (data.status === 'ready') return data;
      if (data.status === 'failed') throw new Error(data.error || 'SVMS 읽기 실패');
      await new Promise(resolve => setTimeout(resolve, 1000));
    }
    throw new Error('SVMS 응답 대기시간을 초과했습니다. 잠시 후 다시 눌러주세요.');
  }

  async function importSvmsDraft(button) {
    if (!VID) { toast('선박을 먼저 선택하세요', true); return; }
    button.disabled = true;
    const original = button.textContent;
    button.textContent = 'SVMS 조회 요청 중…';
    setSS('saving');
    try {
      const requestResponse = await fetch(`${API}/vessels/${VID}/jobs/svms-import/request`, {method: 'POST'});
      const requested = await requestResponse.json();
      if (!requestResponse.ok) throw new Error(requested.error || 'SVMS 가져오기 요청 실패');
      button.textContent = 'SVMS 읽는 중…';
      const preview = await waitForSvmsPreview(requested.preview_token);
      setSS('synced');
      showSvmsPreview(preview, async () => {
        try {
          setSS('saving');
          const response = await fetch(`${API}/vessels/${VID}/jobs/svms-import/apply`, {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({preview_token: requested.preview_token})
          });
          const applied = await response.json();
          if (!response.ok) throw new Error(applied.error || 'SVMS Job 반영 실패');
          const newJobs = await apiFetch(`${API}/vessels/${VID}/jobs`);
          FLEET[VID].jobs = newJobs.map(dbJ);
          buildJFilters(); renderJobs(); renderDash(); closePreview(); setSS('synced');
          toast(`✓ SVMS 기안 반영: ${applied.inserted}개 추가, ${applied.updated}개 갱신`);
        } catch (error) {
          setSS('error'); toast(error.message, true);
          const apply = document.querySelector('#yard-xlsx-preview [data-action="apply"]');
          if (apply) { apply.disabled = false; apply.textContent = 'Job Progress에 반영'; }
        }
      });
    } catch (error) {
      setSS('error'); toast(error.message, true);
    } finally {
      button.disabled = false;
      button.textContent = original;
    }
  }

  async function uploadYardXlsx(input) {
    if (!VID) { toast('선박을 먼저 선택하세요', true); return; }
    const file = input.files && input.files[0];
    if (!file) return;
    const form = new FormData();
    form.append('file', file);
    setSS('saving');
    try {
      const response = await fetch(`${API}/vessels/${VID}/jobs/xlsx/preview`, {method: 'POST', body: form});
      const preview = await response.json();
      if (!response.ok) throw new Error(preview.error || '견적서 파싱 실패');
      setSS('synced');
      showPreview(preview, async () => {
        try {
          setSS('saving');
          const appliedResponse = await fetch(`${API}/vessels/${VID}/jobs/xlsx/apply`, {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({preview_token: preview.preview_token})
          });
          const applied = await appliedResponse.json();
          if (!appliedResponse.ok) throw new Error(applied.error || '견적서 반영 실패');
          const newJobs = await apiFetch(`${API}/vessels/${VID}/jobs`);
          FLEET[VID].jobs = newJobs.map(dbJ);
          buildJFilters(); renderJobs(); renderDash();
          closePreview(); setSS('synced');
          toast(`✓ 견적서 반영: ${applied.inserted}개 추가, ${applied.updated}개 예산 갱신`);
        } catch (error) {
          setSS('error'); toast(error.message, true);
          const apply = document.querySelector('#yard-xlsx-preview [data-action="apply"]');
          if (apply) { apply.disabled = false; apply.textContent = 'Job Progress에 반영'; }
        }
      });
    } catch (error) {
      setSS('error'); toast(error.message, true);
    } finally {
      input.value = '';
    }
  }

  window.uploadJobsCSV = function (input) {
    const file = input.files && input.files[0];
    if (file && /\.xls[xm]$/i.test(file.name)) return uploadYardXlsx(input);
    return originalCsvUpload(input);
  };

  document.addEventListener('DOMContentLoaded', () => {
    const input = document.getElementById('csv-upload-input');
    if (!input) return;
    input.accept = '.csv,.xlsx,.xlsm';
    const button = input.previousElementSibling;
    if (button && button.tagName === 'BUTTON') {
      button.textContent = '📂 견적서 / CSV 업로드';
      button.title = '조선소 견적서 Excel 자동 파싱 또는 Job CSV 업로드';
      const svmsButton = document.createElement('button');
      svmsButton.type = 'button';
      svmsButton.className = 'btn-sec';
      svmsButton.textContent = 'SVMS 기안가져오기';
      svmsButton.title = 'SVMS 입거수리의 Dock Paint, Spare/Store, Shore Repair를 가져옵니다';
      svmsButton.onclick = () => importSvmsDraft(svmsButton);
      button.parentElement.insertBefore(svmsButton, button);
    }
  });
})();
