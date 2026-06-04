// ════════════════════════════════════════════════════════════════
//  출장 경비 상세 — 영수증 표 + 증빙 갤러리 + 촬영/업로드 + Haiku 추출
// ════════════════════════════════════════════════════════════════
const E = {
  tripId: window.EXP_TRIP_ID,
  trip: null,
  receipts: [],
  pending: null,   // 리뷰 모달에서 대기 중인 업로드 {filename,url}
};

const COST_TYPES = ['교통비', '숙박비', '접대비', '복리후생비', '기타'];
const USE_TYPES  = ['법인카드', '개인카드', '현금'];

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
  const isForm = opts.body instanceof FormData;
  const headers = isForm
    ? (opts.headers || {})
    : { 'Content-Type': 'application/json', ...(opts.headers || {}) };
  const r = await fetch(url, { headers, ...opts });
  if (!r.ok) {
    let msg = `${r.status} ${r.statusText}`;
    try { const j = await r.json(); if (j.error) msg = j.error; } catch {}
    throw new Error(msg);
  }
  return r.status === 204 ? null : r.json();
}

function setSaveStatus(text, kind) {
  const s = $('#expd-save-status');
  if (!s) return;
  s.textContent = text || '';
  s.className = 'dde-save-status' + (kind ? ' dde-save-' + kind : '');
}

function fmtDate(s) { return s ? s.replace(/-/g, '.') : ''; }
function fmtMoney(cur, amt) {
  const n = Number(amt || 0);
  return `${cur || ''} ${n.toLocaleString(undefined, { maximumFractionDigits: 2 })}`.trim();
}

// ─── Load & Render ───────────────────────────────────────────
async function init() {
  bindEvents();
  bindCropEvents();
  await load();
}

async function load() {
  try {
    E.trip = await api(`/api/biz-trips/${E.tripId}`);
  } catch (e) {
    alert('출장 로드 실패: ' + e.message);
    return;
  }
  E.receipts = E.trip.receipts || [];
  renderHeader();
  renderTable();
  renderGallery();
}

function renderHeader() {
  $('#expd-title').textContent = E.trip.title || '출장';
  const period = (E.trip.trip_start || E.trip.trip_end)
    ? `${fmtDate(E.trip.trip_start)} ~ ${fmtDate(E.trip.trip_end)}` : '';
  const cards = (E.trip.corp_cards || []).join(', ');
  const bits = [];
  if (period) bits.push('📅 ' + period);
  bits.push(E.trip.status === 'settled' ? '정산완료' : '진행 중');
  if (cards) bits.push('💳 ' + cards);
  $('#expd-subtitle').textContent = bits.join('  ·  ');
  renderTotals();

  // 인쇄용 머리글
  $('#expd-print-head').innerHTML = '';
  $('#expd-print-head').append(
    el('div', { class: 'expd-print-title' }, E.trip.title || '출장 경비'),
    el('div', { class: 'expd-print-sub' }, [period, cards ? '법인카드: ' + cards : ''].filter(Boolean).join('   '))
  );
}

function renderTotals() {
  const wrap = $('#expd-totals');
  wrap.innerHTML = '';
  const totals = E.trip.totals || {};
  const keys = Object.keys(totals);
  wrap.append(el('span', { class: 'expd-total-label' }, '합계'));
  if (!keys.length) {
    wrap.append(el('span', { class: 'expd-total-chip' }, '—'));
  } else {
    for (const k of keys) wrap.append(el('span', { class: 'expd-total-chip' }, fmtMoney(k, totals[k])));
  }
}

function recomputeTotals() {
  const totals = {};
  for (const r of E.receipts) {
    const cur = r.currency || '?';
    totals[cur] = (totals[cur] || 0) + Number(r.amount || 0);
  }
  E.trip.totals = totals;
  renderTotals();
}

// ─── 영수증 표 ───────────────────────────────────────────────
function renderTable() {
  const tb = $('#expd-tbody');
  tb.innerHTML = '';
  $('#expd-empty').hidden = E.receipts.length !== 0;
  E.receipts.forEach((r, i) => tb.append(renderRow(r, i)));
}

function selectEl(options, value, onChange, opts = {}) {
  const sel = el('select', { class: 'expd-cell-input' + (opts.cls ? ' ' + opts.cls : '') });
  if (opts.blank) sel.append(el('option', { value: '' }, opts.blank));
  for (const o of options) sel.append(el('option', { value: o }, o));
  sel.value = value || '';
  sel.addEventListener('change', () => onChange(sel.value));
  return sel;
}

function markReq(input, filled) {
  input.classList.toggle('expd-req-missing', !filled);
}

function renderRow(r, idx) {
  const canEdit = !!E.trip.can_edit;
  const tr = el('tr', { 'data-id': r.id });

  // SEQ
  tr.append(el('td', { class: 'seq' }, String(idx + 1).padStart(4, '0')));

  // Bz Trip Cost Type
  const costSel = selectEl(COST_TYPES, r.cost_type || '교통비', (v) => save(r, { cost_type: v }), { cls: 'sel' });
  tr.append(el('td', {}, costSel));

  // Cost Use Type
  const useSel = selectEl(USE_TYPES, r.use_type || '법인카드', (v) => onUseTypeChange(r, v, cardSel), { cls: 'sel' });
  tr.append(el('td', {}, useSel));

  // Occur Date (필수)
  const dateInp = el('input', {
    type: 'date', class: 'expd-cell-input', value: r.occur_date || '',
    onchange: () => { markReq(dateInp, !!dateInp.value); save(r, { occur_date: dateInp.value }); },
  });
  markReq(dateInp, !!r.occur_date);
  tr.append(el('td', {}, dateInp));

  // Bz Card No (법인카드 등록분에서 선택)
  const cardOpts = E.trip.corp_cards || [];
  const cardSel = selectEl(cardOpts, r.card_no || '', (v) => save(r, { card_no: v }), { blank: '(없음)', cls: 'card' });
  if ((r.use_type || '법인카드') !== '법인카드') cardSel.disabled = true;
  tr.append(el('td', {}, cardSel));

  // Remarks
  const remarkInp = el('input', {
    type: 'text', class: 'expd-cell-input', value: r.remark || '', placeholder: '직접 입력',
    onchange: () => save(r, { remark: remarkInp.value }),
  });
  tr.append(el('td', {}, remarkInp));

  // Currency (필수)
  const curInp = el('input', {
    type: 'text', class: 'expd-cell-input cur', value: r.currency || '', placeholder: 'KRW',
    onchange: () => { const v = curInp.value.trim().toUpperCase(); curInp.value = v; markReq(curInp, !!v); save(r, { currency: v }); },
  });
  markReq(curInp, !!r.currency);
  tr.append(el('td', {}, curInp));

  // Occur Amount (필수)
  const amtInp = el('input', {
    type: 'number', step: '0.01', class: 'expd-cell-input num', value: (r.amount != null ? r.amount : ''),
    onchange: () => { markReq(amtInp, amtInp.value !== ''); save(r, { amount: amtInp.value }); },
  });
  markReq(amtInp, r.amount != null && r.amount !== '');
  tr.append(el('td', { class: 'num' }, amtInp));

  // 사진 썸네일
  const phTd = el('td', { class: 'ph' });
  if (r.image_url) {
    phTd.append(el('a', { href: r.image_url, target: '_blank', title: '원본 보기' },
      el('img', { class: 'expd-thumb', src: r.image_url, alt: '영수증' })));
  } else {
    phTd.append(el('span', { class: 'expd-nophoto' }, '—'));
  }
  tr.append(phTd);

  // 삭제
  const delTd = el('td', { class: 'del' });
  if (canEdit) {
    delTd.append(el('button', {
      class: 'expd-del-btn', type: 'button', title: '삭제',
      onclick: () => deleteReceipt(r),
    }, '×'));
  }
  tr.append(delTd);

  if (!canEdit) $$('input,select', tr).forEach(n => n.disabled = true);
  return tr;
}

function onUseTypeChange(r, v, cardSel) {
  if (v === '법인카드') {
    cardSel.disabled = false;
    if (!cardSel.value && (E.trip.corp_cards || []).length === 1) {
      cardSel.value = E.trip.corp_cards[0];
      save(r, { use_type: v, card_no: cardSel.value });
      return;
    }
  } else {
    cardSel.value = '';
    cardSel.disabled = true;
    save(r, { use_type: v, card_no: '' });
    return;
  }
  save(r, { use_type: v });
}

const _saveTimers = {};
function save(r, patch) {
  Object.assign(r, patch);
  // 합계/갤러리에 영향 주는 변경은 즉시 반영
  if ('amount' in patch || 'currency' in patch) recomputeTotals();
  if ('amount' in patch || 'currency' in patch || 'vendor' in patch || 'remark' in patch) renderGallery();
  clearTimeout(_saveTimers[r.id]);
  setSaveStatus('저장 중...', 'busy');
  _saveTimers[r.id] = setTimeout(async () => {
    try {
      await api(`/api/biz-receipts/${r.id}`, { method: 'PUT', body: JSON.stringify(patch) });
      setSaveStatus('저장됨', 'ok');
    } catch (e) {
      setSaveStatus('저장 실패: ' + e.message, 'err');
    }
  }, 500);
}

async function deleteReceipt(r) {
  if (!confirm('이 영수증을 삭제할까요?')) return;
  try {
    await api(`/api/biz-receipts/${r.id}`, { method: 'DELETE' });
    E.receipts = E.receipts.filter(x => x.id !== r.id);
    renderTable();
    recomputeTotals();
    renderGallery();
  } catch (e) {
    alert('삭제 실패: ' + e.message);
  }
}

// ─── 증빙 갤러리 (인쇄 최적화) ───────────────────────────────
function renderGallery() {
  const grid = $('#expd-receipt-grid');
  grid.innerHTML = '';
  const withImg = E.receipts.filter(r => r.image_url);
  if (!withImg.length) {
    grid.append(el('div', { class: 'expd-gallery-empty no-print' }, '증빙 사진이 없습니다.'));
    return;
  }
  E.receipts.forEach((r, i) => {
    if (!r.image_url) return;
    // 자동 헤더 (SEQ · 통화 금액) — 인쇄 시 증빙 식별용
    const head = [
      String(i + 1).padStart(4, '0'),
      fmtMoney(r.currency, r.amount),
    ].filter(Boolean).join('  ·  ');

    // 수정 가능한 캡션 (= remark, 비어 있으면 상호로 초기 표시)
    const capInput = el('input', {
      type: 'text', class: 'expd-receipt-cap-input',
      value: (r.remark != null && r.remark !== '') ? r.remark : (r.vendor || ''),
      placeholder: '메모 입력 (예: 호텔 3박)',
      onchange: () => save(r, { remark: capInput.value }),
    });
    if (!E.trip.can_edit) capInput.disabled = true;

    grid.append(
      el('div', { class: 'expd-receipt-cell' },
        el('div', { class: 'expd-receipt-imgbox' },
          el('img', { class: 'expd-receipt-img', src: r.image_url, alt: '영수증 ' + (i + 1) })),
        el('div', { class: 'expd-receipt-cap' },
          el('div', { class: 'expd-receipt-cap-head' }, head),
          capInput,
        ),
      )
    );
  });
}

// ─── 촬영 / 업로드 → 추출 → (재촬영) → 표 추가 ───────────────
function pickFiles(inp) {
  return new Promise((resolve) => {
    const onChange = () => {
      inp.removeEventListener('change', onChange);
      const files = [...(inp.files || [])];
      inp.value = '';
      resolve(files);
    };
    inp.addEventListener('change', onChange);
    inp.click();
  });
}

async function handleFiles(files) {
  files = files.filter(f => f && (f.type || '').startsWith('image/'));
  if (!files.length) return;
  for (let i = 0; i < files.length; i++) {
    setSaveStatus(`영수증 처리 중 (${i + 1}/${files.length})...`, 'busy');
    try {
      await processOne(files[i]);
    } catch (e) {
      setSaveStatus('처리 실패: ' + e.message, 'err');
      alert('처리 실패: ' + e.message);
    }
  }
  setSaveStatus('저장됨', 'ok');
}

async function processOne(file) {
  // 0) 크롭/원근보정 (사용자가 모서리 맞춤) — 취소 시 이 파일 건너뜀
  const prepared = await openCrop(file);
  if (!prepared) return;

  // 1) 업로드 (리사이즈/증빙 저장)
  const fd = new FormData();
  fd.append('file', prepared, prepared.name || 'receipt.jpg');
  const up = await api(`/api/biz-trips/${E.tripId}/upload-receipt`, { method: 'POST', body: fd });

  // 2) Haiku 추출
  let ex;
  try {
    ex = await api(`/api/biz-trips/${E.tripId}/extract`, {
      method: 'POST', body: JSON.stringify({ filename: up.filename }),
    });
  } catch (e) {
    ex = { ok: false, message: '추출 요청 실패: ' + e.message };
  }

  const fields = (ex && ex.fields) || {};
  // 3) 깔끔하게 읽혔으면 바로 추가, 아니면 리뷰/재촬영
  if (ex && ex.ok && !ex.need_retake) {
    await createReceipt(up, {
      vendor: fields.vendor, occur_date: fields.occur_date,
      currency: fields.currency, amount: fields.amount,
    });
  } else {
    const reviewed = await openReview(up, ex);
    if (reviewed) {
      await createReceipt(up, reviewed);
    } else {
      // 재촬영/취소 → 업로드한 임시 이미지 삭제 시도
      // (서버에 receipt로 미등록 상태이므로 파일만 남음 — 다음 정리 대상)
    }
  }
}

async function createReceipt(up, fields) {
  const cards = E.trip.corp_cards || [];
  const body = {
    image_filename: up.filename,
    image_url: up.url,
    vendor: fields.vendor || null,
    occur_date: fields.occur_date || null,
    currency: fields.currency || null,
    amount: (fields.amount != null && fields.amount !== '') ? fields.amount : null,
    cost_type: '교통비',
    use_type: '법인카드',
    card_no: cards.length === 1 ? cards[0] : null,
    remark: null,
    extracted_raw: (fields.extracted_raw || null),
  };
  const res = await api(`/api/biz-trips/${E.tripId}/receipts`, {
    method: 'POST', body: JSON.stringify(body),
  });
  E.receipts.push(res.receipt);
  renderTable();
  recomputeTotals();
  renderGallery();
}

// 리뷰 모달 — Promise<fields|null>
let _reviewResolve = null;
function openReview(up, ex) {
  return new Promise((resolve) => {
    _reviewResolve = resolve;
    E.pending = up;
    $('#expd-review-img').src = up.url;
    const f = (ex && ex.fields) || {};
    $('#expd-rv-vendor').value   = f.vendor || '';
    $('#expd-rv-date').value     = f.occur_date || '';
    $('#expd-rv-currency').value = f.currency || '';
    $('#expd-rv-amount').value   = (f.amount != null ? f.amount : '');

    const warn = $('#expd-review-warn');
    const msgs = [];
    if (ex && ex.reason === 'no_api_key') msgs.push(ex.message);
    else if (ex && !ex.ok) msgs.push(ex.message || '자동 추출에 실패했습니다.');
    else {
      if (ex && ex.missing && ex.missing.length) {
        const ko = { occur_date: '일자', currency: '통화', amount: '금액' };
        msgs.push('필수 항목을 못 읽었습니다: ' + ex.missing.map(k => ko[k] || k).join(', '));
      }
      if (ex && ex.confidence === 'low') msgs.push('인식 신뢰도가 낮습니다.');
      if (ex && ex.issues && ex.issues.length) msgs.push('사진 문제: ' + ex.issues.join(', '));
    }
    if (msgs.length) {
      warn.innerHTML = '';
      warn.append(el('strong', {}, '⚠ 확인 필요'), el('br'),
        ...msgs.flatMap(m => [document.createTextNode(m), el('br')]));
      warn.append(el('span', { class: 'expd-warn-hint' }, '다시 촬영하거나, 값을 채운 뒤 추가하세요.'));
      warn.hidden = false;
    } else {
      warn.hidden = true;
    }
    $('#expd-review').hidden = false;
    document.body.classList.add('modal-open');
  });
}

function closeReview(result) {
  $('#expd-review').hidden = true;
  document.body.classList.remove('modal-open');
  const resolve = _reviewResolve;
  _reviewResolve = null;
  E.pending = null;
  if (resolve) resolve(result);
}

// ─── Events ──────────────────────────────────────────────────
function bindEvents() {
  const inpCam = $('#expd-inp-camera');
  const inpGal = $('#expd-inp-gallery');
  $('#expd-btn-camera').addEventListener('click', async () => { await handleFiles(await pickFiles(inpCam)); });
  $('#expd-btn-gallery').addEventListener('click', async () => { await handleFiles(await pickFiles(inpGal)); });

  $('#expd-btn-print').addEventListener('click', () => window.print());

  $('#expd-rv-add').addEventListener('click', () => {
    closeReview({
      vendor:     $('#expd-rv-vendor').value.trim() || null,
      occur_date: $('#expd-rv-date').value || null,
      currency:   ($('#expd-rv-currency').value.trim().toUpperCase()) || null,
      amount:     $('#expd-rv-amount').value !== '' ? $('#expd-rv-amount').value : null,
    });
  });
  $('#expd-rv-retake').addEventListener('click', () => closeReview(null));
  $('#expd-review-close').addEventListener('click', () => closeReview(null));
}

document.addEventListener('DOMContentLoaded', init);

// ════════════════════════════════════════════════════════════════
//  영수증 수동 크롭 + 원근보정 (Adobe 스캔 방식, 라이브러리 없음)
//  · 기본 사각형(가장자리 6% 안쪽) 제시 → 사용자가 네 모서리 조정
//  · "적용" 시 4점→사각형 호모그래피로 반듯하게 펴서 JPEG Blob 반환
// ════════════════════════════════════════════════════════════════
const CROP = {
  resolve: null,
  file: null,
  handles: [],   // [{fx,fy} ...]  TL,TR,BR,BL (0~1 비율)
  active: -1,
  WORK_MAX: 1800,  // 작업 캔버스 최대 장변
  OUT_MAX: 1500,   // 출력 최대 장변
};

function openCrop(file) {
  return new Promise((resolve) => {
    CROP.resolve = resolve;
    CROP.file = file;
    const modal = $('#expd-crop');
    const canvas = $('#expd-crop-canvas');
    const img = new Image();
    img.onload = () => {
      // 작업 해상도로 캔버스에 그림
      let w = img.naturalWidth, h = img.naturalHeight;
      const long = Math.max(w, h);
      if (long > CROP.WORK_MAX) { const r = CROP.WORK_MAX / long; w = Math.round(w * r); h = Math.round(h * r); }
      canvas.width = w; canvas.height = h;
      const ctx = canvas.getContext('2d', { willReadFrequently: true });
      ctx.drawImage(img, 0, 0, w, h);
      URL.revokeObjectURL(img.src);

      CROP.handles = [
        { fx: 0.06, fy: 0.06 }, { fx: 0.94, fy: 0.06 },
        { fx: 0.94, fy: 0.94 }, { fx: 0.06, fy: 0.94 },
      ];
      modal.hidden = false;
      document.body.classList.add('modal-open');
      buildHandles();
      requestAnimationFrame(renderQuad);
    };
    img.onerror = () => { resolve(file); };  // 디코드 실패 시 원본 그대로
    img.src = URL.createObjectURL(file);
  });
}

function buildHandles() {
  const overlay = $('#expd-crop-overlay');
  // 기존 핸들 제거 (폴리곤 svg는 유지)
  $$('.expd-crop-handle', overlay).forEach(n => n.remove());
  CROP.handles.forEach((h, i) => {
    const hd = el('div', { class: 'expd-crop-handle', 'data-i': i });
    hd.addEventListener('pointerdown', (e) => {
      e.preventDefault();
      CROP.active = i;
      try { hd.setPointerCapture(e.pointerId); } catch {}
    });
    hd.addEventListener('pointermove', (e) => {
      if (CROP.active !== i) return;
      const rect = overlay.getBoundingClientRect();
      const fx = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width));
      const fy = Math.min(1, Math.max(0, (e.clientY - rect.top) / rect.height));
      CROP.handles[i] = { fx, fy };
      renderQuad();
    });
    const end = (e) => { if (CROP.active === i) { CROP.active = -1; try { hd.releasePointerCapture(e.pointerId); } catch {} } };
    hd.addEventListener('pointerup', end);
    hd.addEventListener('pointercancel', end);
    overlay.append(hd);
  });
}

function renderQuad() {
  const overlay = $('#expd-crop-overlay');
  const canvas = $('#expd-crop-canvas');
  const rect = canvas.getBoundingClientRect();
  // 오버레이를 캔버스 표시 크기에 맞춤
  overlay.style.width = rect.width + 'px';
  overlay.style.height = rect.height + 'px';
  const pts = CROP.handles.map(h => [h.fx * rect.width, h.fy * rect.height]);
  const svg = $('#expd-crop-svg');
  svg.setAttribute('width', rect.width);
  svg.setAttribute('height', rect.height);
  $('#expd-crop-poly').setAttribute('points', pts.map(p => p.join(',')).join(' '));
  $$('.expd-crop-handle', overlay).forEach((hd, i) => {
    hd.style.left = pts[i][0] + 'px';
    hd.style.top = pts[i][1] + 'px';
  });
}

// 8x8 선형계 풀이 (부분 피벗 가우스 소거)
function _solve8(M, b) {
  const n = 8, A = M.map((r, i) => r.concat([b[i]]));
  for (let c = 0; c < n; c++) {
    let p = c;
    for (let r = c + 1; r < n; r++) if (Math.abs(A[r][c]) > Math.abs(A[p][c])) p = r;
    [A[c], A[p]] = [A[p], A[c]];
    const piv = A[c][c] || 1e-12;
    for (let r = 0; r < n; r++) {
      if (r === c) continue;
      const f = A[r][c] / piv;
      for (let k = c; k <= n; k++) A[r][k] -= f * A[c][k];
    }
  }
  const x = new Array(n);
  for (let i = 0; i < n; i++) x[i] = A[i][n] / (A[i][i] || 1e-12);
  return x;
}

// dst(사각형)→src(사용자 사각형) 호모그래피 [h0..h8]
function _homography(dst, src) {
  const M = [], r = [];
  for (let i = 0; i < 4; i++) {
    const x = dst[i].x, y = dst[i].y, u = src[i].x, v = src[i].y;
    M.push([x, y, 1, 0, 0, 0, -x * u, -y * u]); r.push(u);
    M.push([0, 0, 0, x, y, 1, -x * v, -y * v]); r.push(v);
  }
  const h = _solve8(M, r);
  return [h[0], h[1], h[2], h[3], h[4], h[5], h[6], h[7], 1];
}

function _dist(a, b) { return Math.hypot(a.x - b.x, a.y - b.y); }

function applyCrop() {
  const canvas = $('#expd-crop-canvas');
  const W = canvas.width, H = canvas.height;
  const q = CROP.handles.map(h => ({ x: h.fx * W, y: h.fy * H }));  // src 4점 (작업 px)
  const [tl, tr, br, bl] = q;
  let outW = Math.round((_dist(tl, tr) + _dist(bl, br)) / 2);
  let outH = Math.round((_dist(tl, bl) + _dist(tr, br)) / 2);
  const long = Math.max(outW, outH);
  if (long > CROP.OUT_MAX) { const r = CROP.OUT_MAX / long; outW = Math.round(outW * r); outH = Math.round(outH * r); }
  outW = Math.max(outW, 80); outH = Math.max(outH, 80);

  const dstC = [{ x: 0, y: 0 }, { x: outW, y: 0 }, { x: outW, y: outH }, { x: 0, y: outH }];
  const Hm = _homography(dstC, q);  // dst->src

  const sctx = canvas.getContext('2d', { willReadFrequently: true });
  const sImg = sctx.getImageData(0, 0, W, H), sData = sImg.data;
  const out = document.createElement('canvas'); out.width = outW; out.height = outH;
  const octx = out.getContext('2d');
  const oImg = octx.createImageData(outW, outH), oData = oImg.data;

  for (let y = 0; y < outH; y++) {
    for (let x = 0; x < outW; x++) {
      const dn = Hm[6] * x + Hm[7] * y + Hm[8];
      const sx = (Hm[0] * x + Hm[1] * y + Hm[2]) / dn;
      const sy = (Hm[3] * x + Hm[4] * y + Hm[5]) / dn;
      const ix = sx | 0, iy = sy | 0;
      const oi = (y * outW + x) * 4;
      if (ix >= 0 && ix < W && iy >= 0 && iy < H) {
        const si = (iy * W + ix) * 4;
        oData[oi] = sData[si]; oData[oi + 1] = sData[si + 1]; oData[oi + 2] = sData[si + 2]; oData[oi + 3] = 255;
      } else { oData[oi] = oData[oi + 1] = oData[oi + 2] = oData[oi + 3] = 255; }
    }
  }
  octx.putImageData(oImg, 0, 0);
  out.toBlob((blob) => { finishCrop(blob || CROP.file); }, 'image/jpeg', 0.9);
}

function finishCrop(result) {
  $('#expd-crop').hidden = true;
  document.body.classList.remove('modal-open');
  const resolve = CROP.resolve; CROP.resolve = null; CROP.active = -1;
  if (resolve) resolve(result);
}

function bindCropEvents() {
  $('#expd-crop-apply').addEventListener('click', applyCrop);
  $('#expd-crop-orig').addEventListener('click', () => finishCrop(CROP.file));   // 원본 사용
  $('#expd-crop-cancel').addEventListener('click', () => finishCrop(null));      // 취소(건너뜀)
  $('#expd-crop-reset').addEventListener('click', () => {
    CROP.handles = [
      { fx: 0.06, fy: 0.06 }, { fx: 0.94, fy: 0.06 },
      { fx: 0.94, fy: 0.94 }, { fx: 0.06, fy: 0.94 },
    ];
    renderQuad();
  });
  window.addEventListener('resize', () => { if (!$('#expd-crop').hidden) renderQuad(); });
}
