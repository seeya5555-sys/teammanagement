/* ═══════════════════════════════════════════════════════════════
   TRMT3 — Condition Survey
   ═══════════════════════════════════════════════════════════════ */

// localStorage에서 펼친 상태 복구
function loadExpandedSet() {
  try {
    const raw = localStorage.getItem('trmt_cs_expanded');
    if (!raw) return new Set();
    return new Set(JSON.parse(raw));
  } catch (_) { return new Set(); }
}
function saveExpandedSet() {
  try {
    localStorage.setItem('trmt_cs_expanded', JSON.stringify([...S.expandedSurveys]));
  } catch (_) {}
}
// 선박 카드 펼침 — 매 진입 시 항상 모두 접힘 상태로 시작
// (localStorage 사용 안 함. 같은 페이지 안에서 펼친 건 유지되지만,
//  페이지 다시 진입하면 다시 모두 접힘)

// CS에서 숨길 감독 이름 (대소문자 무시) — Daily 업무관리는 영향 없음
const HIDDEN_SUPERVISOR_NAMES_CS = ['FLEET AGENDA'];
function isHiddenSupervisor(sup) {
  return HIDDEN_SUPERVISOR_NAMES_CS.includes((sup.name || '').toUpperCase());
}

// ───────────── State ─────────────
const S = {
  user:        window.TRMT?.user || {},
  supervisors: [],
  data:        [],
  activeTab:   'all',
  year:        new Date().getFullYear(),
  search:      '',
  expandedSurveys: loadExpandedSet(),
  expandedVessels: new Set(),
  hiddenVesselIds: new Set(),    // CS 전체 탭에서 제외할 vessel id
};

// ───────────── Helpers ─────────────
function $(s, r=document) { return r.querySelector(s); }
function escHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]
  ));
}
function el(tag, attrs={}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class')      e.className = v;
    else if (k === 'html')  e.innerHTML = v;
    else if (k.startsWith('on') && typeof v === 'function')
                             e.addEventListener(k.slice(2).toLowerCase(), v);
    else if (v === true)    e.setAttribute(k, '');
    else                    e.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    e.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
  return e;
}

// ───────────── API ─────────────
async function api(url, opts={}) {
  const isForm = opts.body instanceof FormData;
  const headers = isForm ? {} : {'Content-Type':'application/json'};
  const res = await fetch(url, { headers, ...opts });
  const ct = res.headers.get('content-type') || '';
  const data = ct.includes('json') ? await res.json() : await res.text();
  if (!res.ok) {
    const msg = (typeof data === 'object' && data.error) ? data.error : `HTTP ${res.status}`;
    throw new Error(msg);
  }
  return data;
}

// ───────────── Tabs ─────────────
function renderTabs() {
  const bar = $('#cs-tab-bar');
  bar.innerHTML = '';
  bar.append(tabEl('all', '전체', 'gray', null, S.activeTab === 'all'));
  for (const s of S.supervisors) {
    if (isHiddenSupervisor(s)) continue;   // CS 숨김
    bar.append(tabEl(s.id, s.name, s.color, null, S.activeTab == s.id));
  }
}
function tabEl(id, name, color, _count, active) {
  const t = el('div', { class: 'tab' + (active ? ' active' : ''), 'data-id': id },
    el('span', { class: `tab-dot dot-${color}` }),
    name);
  t.addEventListener('click', () => switchTab(id));
  return t;
}
async function switchTab(id) {
  S.activeTab = id;
  renderTabs();
  await reloadData();
}

function renderContext() {
  const c = $('#cs-context');
  const q = (S.search || '').trim().toLowerCase();
  let totalCount = S.data.length;
  let filteredCount = totalCount;
  if (q) {
    filteredCount = S.data.filter(item => {
      const v = item.vessel;
      return (v.name && v.name.toLowerCase().includes(q))
          || (v.short_name && v.short_name.toLowerCase().includes(q));
    }).length;
  }
  const tabName = S.activeTab === 'all'
    ? '전체'
    : (S.supervisors.find(x => x.id == S.activeTab)?.name + ' 담당' || '');

  if (q) {
    c.textContent = `${S.year}년 · ${tabName} 선박 ${filteredCount}/${totalCount}척  (검색: "${S.search}")`;
  } else {
    c.textContent = `${S.year}년 · ${tabName} 선박 ${totalCount}척`;
  }
}

// ───────────── Render ─────────────
const QUARTERS = [1, 2, 3, 4];

function render() {
  const list = $('#cs-vessel-list');
  list.innerHTML = '';
  if (!S.data.length) {
    list.append(el('div', { class: 'cs-empty' },
      '담당 선박이 없습니다. Daily 업무관리에서 선박을 추가하세요.'));
    return;
  }

  // 검색어로 선박명 필터링 (대소문자 무시 · 부분 일치 · 약칭도 검색 대상)
  const q = (S.search || '').trim().toLowerCase();
  const filtered = q ? S.data.filter(item => {
    const v = item.vessel;
    return (v.name && v.name.toLowerCase().includes(q))
        || (v.short_name && v.short_name.toLowerCase().includes(q));
  }) : S.data;

  if (q && !filtered.length) {
    list.append(el('div', { class: 'cs-empty' },
      `"${S.search}" 와(과) 일치하는 선박이 없습니다.`));
    return;
  }

  // 선종별 그룹화
  const TYPE_ORDER = ['VLCC', 'AFRAMAX', 'MR', 'LR', 'CNTR', '기타'];
  const TYPE_LABEL = {
    VLCC: 'VLCC', AFRAMAX: 'AFRAMAX', MR: 'MR', LR: 'LR',
    CNTR: 'CNTR (Container)', '기타': '기타',
  };
  const byType = {};
  for (const item of filtered) {
    const type = item.vessel.vessel_type || '기타';
    if (!byType[type]) byType[type] = [];
    byType[type].push(item);
  }
  const types = Object.keys(byType).sort((a, b) => {
    const ai = TYPE_ORDER.indexOf(a);
    const bi = TYPE_ORDER.indexOf(b);
    return (ai < 0 ? 99 : ai) - (bi < 0 ? 99 : bi);
  });

  for (const type of types) {
    const group = byType[type];
    const groupBlock = el('div', { class: 'cs-type-group' });
    groupBlock.append(el('div', { class: `cs-type-header cs-type-${type.toLowerCase()}` },
      el('span', { class: 'cs-type-badge' }, TYPE_LABEL[type] || type),
      el('span', { class: 'cs-type-count' }, `${group.length}척`),
    ));
    for (const item of group) {
      groupBlock.append(vesselBlock(item));
    }
    list.append(groupBlock);
  }
}

function vesselSummary(item) {
  // 접힌 상태에서도 보이는 요약: 1Q~4Q 분기별 Open 카운트 (미등록도 격자로 표시)
  const surveys = item.surveys || {};
  const wrap = el('span', { class: 'cs-vessel-summary' });

  // 1Q ~ 4Q 격자 (미등록 분기는 회색 빈 셀)
  for (const q of [1, 2, 3, 4]) {
    const s = surveys[q];
    const cell = el('span', { class: 'cs-q-summary' });
    cell.append(el('span', { class: 'cs-q-label' }, `${q}Q`));
    if (s) {
      const op = s.open_count || 0;
      const cl = s.close_count || 0;
      const num = el('span', { class: 'cs-q-num' });
      // Open이 1+ 이면 빨간 강조, 모두 Closed면 초록 체크, 항목 0이면 –
      if (op > 0) {
        num.append(el('strong', { class: 'cs-q-open-on' }, String(op)));
      } else if (cl > 0) {
        num.append(el('strong', { class: 'cs-q-all-closed' }, '✓'));
      } else {
        num.append(el('span', { class: 'cs-q-empty-data' }, '–'));
      }
      cell.append(num);
      cell.classList.add('has-data');
    } else {
      cell.append(el('span', { class: 'cs-q-num cs-q-blank' }, '–'));
    }
    wrap.append(cell);
  }
  return wrap;
}

function toggleVessel(vid) {
  if (S.expandedVessels.has(vid)) S.expandedVessels.delete(vid);
  else S.expandedVessels.add(vid);
  render();
}

function vesselBlock(item) {
  const v = item.vessel;
  const block = el('div', { class: 'cs-vessel-block' });
  const isExpanded = S.expandedVessels.has(v.id);

  // 선박 헤더 (클릭으로 토글)
  const head = el('div', {
    class: 'cs-vessel-head' + (isExpanded ? ' expanded' : ''),
    onclick: () => toggleVessel(v.id),
    title: '클릭으로 분기 표 펼치기/접기',
  },
    el('span', { class: 'cs-vessel-caret' }, isExpanded ? '▼' : '▶'),
    el('span', { class: 'cs-vessel-icon' }, '🚢'),
    el('strong', { class: 'cs-vessel-name' }, v.name),
    el('span', { class: 'cs-vessel-type' }, v.vessel_type || ''),
    // 요약 카운트 (접힌 상태에서도 보이도록)
    vesselSummary(item),
  );
  block.append(head);

  if (!isExpanded) return block;

  // 분기 표
  const table = el('table', { class: 'cs-quarter-table' });
  const thead = el('thead', {},
    el('tr', {},
      el('th', { style: 'width:60px' }, 'Quarter'),
      el('th', { style: 'width:110px' }, '시행사'),
      el('th', { style: 'width:140px' }, 'Management'),
      el('th', { style: 'width:140px' }, 'Inspection Date'),
      el('th', { style: 'width:60px; text-align:center' }, 'Def'),
      el('th', { style: 'width:60px; text-align:center' }, 'Obs'),
      el('th', { style: 'width:70px; text-align:center' }, '합계'),
      el('th', { style: 'width:60px; text-align:center' }, 'Open'),
      el('th', { style: 'width:60px; text-align:center' }, 'Close'),
      el('th', { class: 'cs-th-actions' }, ''),
    )
  );
  table.append(thead);

  const tbody = el('tbody');
  for (const q of QUARTERS) {
    const survey = item.surveys[q];   // undefined = 빈 분기
    tbody.append(quarterRow(v.id, q, survey));
    // 펼친 상태면 세부 행 추가
    if (survey && S.expandedSurveys.has(survey.id)) {
      tbody.append(detailRow(survey));
    }
  }
  table.append(tbody);
  block.append(table);

  return block;
}

function quarterRow(vesselId, quarter, survey) {
  const tr = el('tr', { class: 'cs-quarter-row' + (survey ? ' has-data' : ' empty') });

  // Quarter 셀 — 클릭 시 펼치기/모달
  const qCell = el('td', { class: 'cs-q-label' });
  if (survey) {
    const expanded = S.expandedSurveys.has(survey.id);
    qCell.append(
      el('span', { class: 'cs-caret' }, expanded ? '▼' : '▶'),
      ` ${quarter}Q`,
    );
    qCell.style.cursor = 'pointer';
    qCell.addEventListener('click', () => toggleExpand(survey.id));
  } else {
    qCell.textContent = `${quarter}Q`;
    qCell.classList.add('disabled');
  }
  tr.append(qCell);

  // 시행사
  tr.append(editableCell(
    survey, vesselId, quarter, 'vendor',
    survey?.vendor || '',
    'select', ['', 'AALMAR', 'IDWAL'],
  ));
  // Management
  tr.append(editableCell(
    survey, vesselId, quarter, 'management',
    survey?.management || '',
    'text',
  ));
  // Inspection Date
  tr.append(editableCell(
    survey, vesselId, quarter, 'inspection_date',
    survey?.inspection_date || '',
    'date',
  ));

  // 카운트 5개 (Def/Obs/합계/Open/Close, 중앙정렬)
  if (survey) {
    tr.append(countEditableCell(survey, 'manual_defect_count',      survey.defect_count,      survey.defect_manual));
    tr.append(countEditableCell(survey, 'manual_observation_count', survey.observation_count, survey.observation_manual));
    // 합계는 자동 (Def + Obs)
    tr.append(el('td', { class: 'cs-cnt cs-cnt-total', style: 'text-align:center' },
      String(survey.total_count)));
    // Open 카운트 — 자동 (총 - Close), 별도 셀
    tr.append(el('td', { class: 'cs-cnt cs-cnt-open', style: 'text-align:center' },
      String(survey.open_count)));
    tr.append(countEditableCell(survey, 'manual_close_count',       survey.close_count,       survey.close_manual, true));
  } else {
    tr.append(el('td', { class: 'cs-cnt', style: 'text-align:center' }, '–'));
    tr.append(el('td', { class: 'cs-cnt', style: 'text-align:center' }, '–'));
    tr.append(el('td', { class: 'cs-cnt cs-cnt-total', style: 'text-align:center' }, '–'));
    tr.append(el('td', { class: 'cs-cnt cs-cnt-open',  style: 'text-align:center' }, '–'));
    tr.append(el('td', { class: 'cs-cnt cs-cnt-close', style: 'text-align:center' }, '–'));
  }

  // 액션 (첨부 + 메모 + 삭제)
  const actions = el('td', { class: 'cs-actions' });
  if (survey) {
    // 📎 첨부
    const attBtn = el('button', {
      class: 'icon-btn',
      title: `첨부파일${survey.attach_count ? ` (${survey.attach_count})` : ''}`,
      onclick: (e) => { e.stopPropagation(); openAttachModal(survey); },
    });
    attBtn.innerHTML = `<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2">
      <path d="M21.44 11.05l-9.19 9.19a6 6 0 01-8.49-8.49l9.19-9.19a4 4 0 015.66 5.66l-9.2 9.19a2 2 0 01-2.83-2.83l8.49-8.48"/></svg>`;
    if (survey.attach_count > 0) {
      attBtn.classList.add('has-attach');
      const badge = el('span', { class: 'attach-badge' }, String(survey.attach_count));
      attBtn.append(badge);
    }
    actions.append(attBtn);

    // 📝 메모
    const memoBtn = el('button', {
      class: 'icon-btn',
      title: 'Overall Remark / 상세 편집',
      onclick: (e) => { e.stopPropagation(); openSurveyModal(vesselId, quarter, survey); },
    });
    memoBtn.innerHTML = `<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2">
      <path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/>
      <polyline points="14 2 14 8 20 8"/>
      <line x1="9" y1="13" x2="15" y2="13"/>
      <line x1="9" y1="17" x2="13" y2="17"/></svg>`;
    if (survey.overall_remark) memoBtn.classList.add('has-memo');
    actions.append(memoBtn);

    // 🗑 삭제
    const rm = el('button', {
      class: 'icon-btn danger',
      title: '이 분기 서베이 삭제',
      onclick: (e) => { e.stopPropagation(); deleteSurvey(survey); },
    });
    rm.innerHTML = `<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2">
      <path d="M3 6h18"/><path d="M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2"/>
      <path d="M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6"/></svg>`;
    actions.append(rm);
  }
  tr.append(actions);

  return tr;
}

// 카운트 셀 (클릭 → 숫자 입력, 빈값 저장 시 자동 카운트로 복귀)
function countEditableCell(survey, field, value, isManual, isClose = false) {
  const td = el('td', {
    class: 'cs-cnt cs-edit-cell' + (isClose ? ' cs-cnt-close' : '') + (isManual ? ' is-manual' : ''),
    style: 'text-align:center',
    title: isManual ? '수동 입력값 (클릭으로 수정, 빈칸 저장 시 자동값 복귀)' : '자동 카운트 (클릭으로 직접 입력)',
  });
  const display = el('div', { class: 'cs-cell-display' }, String(value));
  td.append(display);

  td.addEventListener('click', (e) => {
    e.stopPropagation();
    if (td._editing) return;
    td._editing = true;
    td.innerHTML = '';
    const input = document.createElement('input');
    input.type = 'number';
    input.min = '0';
    input.value = isManual ? String(value) : '';
    input.placeholder = String(value);
    input.className = 'cs-inline-input';
    input.style.textAlign = 'center';
    td.append(input);
    input.focus();
    input.select();

    let done = false;
    const save = async () => {
      if (done) return; done = true;
      td._editing = false;
      const raw = input.value.trim();
      // 빈값 = 자동 카운트로 복귀 (NULL)
      const newVal = raw === '' ? null : Number(raw);
      if (newVal !== null && (isNaN(newVal) || newVal < 0)) {
        td.innerHTML = ''; td.append(display);
        return;
      }
      try {
        await api(`/api/cs/surveys/${survey.id}`, {
          method: 'PUT',
          body: JSON.stringify({ [field]: newVal === null ? '' : newVal }),
        });
        await reloadData();
      } catch (err) {
        alert('저장 실패: ' + err.message);
        td.innerHTML = ''; td.append(display);
      }
    };
    input.addEventListener('blur', save);
    input.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter') save();
      if (ev.key === 'Escape') {
        done = true; td._editing = false;
        td.innerHTML = ''; td.append(display);
      }
    });
  });

  return td;
}

function editableCell(survey, vesselId, quarter, field, value, kind, options) {
  const td = el('td', { class: 'cs-edit-cell' });
  const display = el('div', { class: 'cs-cell-display' });

  if (kind === 'select') {
    display.textContent = value || '–';
    if (!value) display.classList.add('placeholder');
  } else {
    display.textContent = value || '–';
    if (!value) display.classList.add('placeholder');
  }
  td.append(display);

  td.addEventListener('click', (e) => {
    e.stopPropagation();
    if (td._editing) return;
    td._editing = true;
    td.innerHTML = '';
    let input;
    if (kind === 'select') {
      input = document.createElement('select');
      for (const opt of options) {
        const o = document.createElement('option');
        o.value = opt; o.textContent = opt || '(미선택)';
        if (opt === value) o.selected = true;
        input.append(o);
      }
    } else if (kind === 'date') {
      input = document.createElement('input');
      input.type = 'date'; input.value = value || '';
    } else {
      input = document.createElement('input');
      input.type = 'text'; input.value = value || '';
    }
    input.className = 'cs-inline-input';
    td.append(input);
    input.focus();
    if (input.select) input.select();

    let done = false;
    const save = async () => {
      if (done) return; done = true;
      const newVal = input.value;
      td._editing = false;
      if (newVal === value) {
        td.innerHTML = '';
        td.append(display);
        return;
      }
      try {
        if (survey) {
          await api(`/api/cs/surveys/${survey.id}`, {
            method: 'PUT',
            body: JSON.stringify({ [field]: newVal }),
          });
        } else {
          // 신규 생성 (해당 분기 셀에 처음 값이 들어감)
          const r = await api('/api/cs/surveys', {
            method: 'POST',
            body: JSON.stringify({
              vessel_id: vesselId,
              year: S.year,
              quarter,
              [field]: newVal,
            }),
          });
        }
        await reloadData();
      } catch (err) {
        alert('저장 실패: ' + err.message);
        td.innerHTML = '';
        td.append(display);
      }
    };
    input.addEventListener('blur', save);
    input.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter') save();
      if (ev.key === 'Escape') {
        done = true; td._editing = false;
        td.innerHTML = '';
        td.append(display);
      }
    });
  });

  return td;
}

// ───────────── 펼치기 / 세부 행 (Findings) ─────────────
function toggleExpand(surveyId) {
  if (S.expandedSurveys.has(surveyId)) S.expandedSurveys.delete(surveyId);
  else S.expandedSurveys.add(surveyId);
  saveExpandedSet();
  render();
}

function detailRow(survey) {
  const tr = el('tr', { class: 'cs-detail-row' });
  const td = el('td', { colspan: 10, class: 'cs-detail-cell' });

  const defects      = (survey.findings || []).filter(f => f.category === 'Defect');
  const observations = (survey.findings || []).filter(f => f.category === 'Observation');

  // 1) Overall Remark — 맨 위, Defect/Observation 섹션과 같은 스타일
  if (survey.overall_remark) {
    const sec = el('div', { class: 'cs-finding-section' });
    sec.append(el('div', { class: 'cs-finding-header cs-cat-overall' },
      el('span', { class: 'cs-cat-dot' }),
      el('strong', {}, 'Overall Remark'),
    ));
    sec.append(el('div', { class: 'cs-overall-body' }, survey.overall_remark));
    td.append(sec);
  }

  // 2) Defect 섹션
  if (defects.length || (survey._inlineAdd && survey._inlineAdd.category === 'Defect')) {
    td.append(findingsSection(survey, 'Defect', defects));
  } else {
    td.append(addOnlyBtn(survey, 'Defect'));
  }

  // 3) Observation 섹션
  if (observations.length || (survey._inlineAdd && survey._inlineAdd.category === 'Observation')) {
    td.append(findingsSection(survey, 'Observation', observations));
  } else {
    td.append(addOnlyBtn(survey, 'Observation'));
  }

  tr.append(td);
  return tr;
}

function emptySection(survey) {
  const wrap = el('div', { class: 'cs-finding-empty' });
  wrap.append(el('div', { style: 'color:var(--text-tertiary); font-size:12px; margin-bottom:8px' },
    '아직 등록된 항목이 없습니다. Defect 또는 Observation을 추가하세요.'));
  wrap.append(addOnlyBtn(survey, 'Defect'));
  wrap.append(addOnlyBtn(survey, 'Observation'));
  return wrap;
}

function addOnlyBtn(survey, category) {
  const btn = el('button', {
    class: 'btn btn-outline btn-sm',
    style: 'margin: 4px 6px 4px 0',
    onclick: () => addBlankRow(survey, category),
  }, `+ ${category} 추가`);
  return btn;
}

function findingsSection(survey, category, findings) {
  const sec = el('div', { class: 'cs-finding-section' });
  const header = el('div', { class: `cs-finding-header cs-cat-${category.toLowerCase()}` },
    el('span', { class: 'cs-cat-dot' }),
    el('strong', {}, category),
    el('span', { class: 'cs-cat-count' }, `(${findings.length}건)`),
  );
  sec.append(header);

  const table = el('table', { class: 'cs-findings-table', 'data-survey': survey.id, 'data-category': category });
  const thead = el('thead', {}, el('tr', {},
    el('th', { style: 'width:50px' }, 'No'),
    el('th', { class: 'cs-th-item' }, 'Item'),
    el('th', { class: 'cs-th-desc' }, 'Description'),
    el('th', { class: 'cs-th-remark' }, 'Remark'),
    el('th', { style: 'width:90px; text-align:center' }, 'Status'),
    el('th', { style: 'width:48px' }, ''),
  ));
  table.append(thead);

  const tbody = el('tbody');
  for (const f of findings) tbody.append(findingRow(survey, f));

  // 인라인 추가 행들 (배열 형태) — 여러 빈 행 누적 가능
  const isAdding = survey._inlineAdd && survey._inlineAdd.category === category;
  if (isAdding) {
    survey._inlineAdd.rows.forEach((row, idx) => {
      tbody.append(inlineAddRow(survey, category, row, idx, findings.length));
    });
  }

  table.append(tbody);
  sec.append(table);

  // 버튼 영역 — + 추가 / 💾 저장 / 취소
  const btnRow = el('div', { class: 'cs-add-btn-row' });
  btnRow.append(el('button', {
    class: 'btn btn-outline btn-sm',
    onclick: () => addBlankRow(survey, category),
  }, isAdding
      ? `+ 빈 줄 추가  (엑셀 표 붙여넣기 가능)`
      : `+ ${category} 항목 추가  (엑셀 표 붙여넣기 가능)`));

  if (isAdding) {
    btnRow.append(el('button', {
      class: 'btn btn-primary btn-sm',
      onclick: () => saveAllInlineRows(survey, category),
    }, `💾 전체 저장`));
    btnRow.append(el('button', {
      class: 'btn btn-outline btn-sm',
      onclick: () => { survey._inlineAdd = null; render(); },
    }, `취소`));
  }
  sec.append(btnRow);

  return sec;
}

function findingRow(survey, f) {
  const tr = el('tr');
  tr.append(el('td', { class: 'cs-no' }, String(f.no)));
  tr.append(findingEditableCell(f, 'item', 'text'));
  tr.append(findingEditableCell(f, 'description', 'text'));
  tr.append(findingEditableCell(f, 'remark', 'text'));

  // Status — 클릭 토글
  const stTd = el('td', { class: 'cs-status', style: 'text-align:center' });
  const stPill = el('span', {
    class: 'bd ' + (f.status === 'Closed' ? 'status-done' : 'status-open'),
    style: 'cursor:pointer',
    title: '클릭으로 Open/Closed 토글',
    onclick: () => toggleFindingStatus(f),
  }, f.status);
  stTd.append(stPill);
  tr.append(stTd);

  // 삭제
  const acts = el('td', { class: 'cs-actions' });
  const rm = el('button', {
    class: 'icon-btn danger',
    title: '항목 삭제',
    onclick: () => deleteFinding(f),
  });
  rm.innerHTML = `<svg viewBox="0 0 24 24" width="11" height="11" fill="none" stroke="currentColor" stroke-width="2">
    <path d="M3 6h18"/><path d="M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2"/>
    <path d="M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6"/></svg>`;
  acts.append(rm);
  tr.append(acts);
  return tr;
}

function findingEditableCell(f, field, kind) {
  const td = el('td', { class: 'cs-edit-cell' });
  const display = el('div', { class: 'cs-cell-display' }, f[field] || '–');
  if (!f[field]) display.classList.add('placeholder');
  td.append(display);

  td.addEventListener('click', (e) => {
    if (td._editing) return;
    td._editing = true;
    td.innerHTML = '';
    const input = document.createElement(kind === 'textarea' ? 'textarea' : 'input');
    if (kind !== 'textarea') input.type = 'text';
    input.value = f[field] || '';
    input.className = 'cs-inline-input';
    td.append(input);
    input.focus();
    if (input.select) input.select();

    let done = false;
    const save = async () => {
      if (done) return; done = true;
      const newVal = input.value;
      td._editing = false;
      if (newVal === (f[field] || '')) {
        td.innerHTML = ''; td.append(display);
        return;
      }
      try {
        await api(`/api/cs/findings/${f.id}`, {
          method: 'PUT',
          body: JSON.stringify({ [field]: newVal }),
        });
        f[field] = newVal;
        await reloadData();
      } catch (err) {
        alert('저장 실패: ' + err.message);
        td.innerHTML = ''; td.append(display);
      }
    };
    input.addEventListener('blur', save);
    input.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter' && !ev.shiftKey) { ev.preventDefault(); save(); }
      if (ev.key === 'Escape') {
        done = true; td._editing = false;
        td.innerHTML = ''; td.append(display);
      }
    });
  });
  return td;
}

async function toggleFindingStatus(f) {
  const newSt = f.status === 'Closed' ? 'Open' : 'Closed';
  try {
    await api(`/api/cs/findings/${f.id}`, {
      method: 'PUT',
      body: JSON.stringify({ status: newSt }),
    });
    await reloadData();
  } catch (err) { alert('상태 변경 실패: ' + err.message); }
}

async function deleteFinding(f) {
  if (!confirm(`${f.category} #${f.no} 을(를) 삭제하시겠습니까?`)) return;
  try {
    await api(`/api/cs/findings/${f.id}`, { method: 'DELETE' });
    await reloadData();
  } catch (err) { alert('삭제 실패: ' + err.message); }
}

// ───────────── 인라인 추가 + 엑셀 붙여넣기 ─────────────
// 빈 행 하나 추가 (이미 있으면 행 배열에 push, 없으면 새로 시작)
function addBlankRow(survey, category) {
  // 다른 survey의 inlineAdd는 닫기 (한 번에 하나만)
  for (const item of S.data) {
    for (const q of QUARTERS) {
      const s = item.surveys[q];
      if (s && s !== survey && s._inlineAdd) s._inlineAdd = null;
    }
  }
  // 현재 survey가 다른 카테고리에서 추가 중이면 그것도 닫기
  if (survey._inlineAdd && survey._inlineAdd.category !== category) {
    survey._inlineAdd = null;
  }
  if (!survey._inlineAdd) {
    survey._inlineAdd = { category, rows: [] };
  }
  survey._inlineAdd.rows.push({ item: '', description: '', remark: '', status: 'Open' });
  render();

  // 첫 빈 행의 첫 입력에 focus
  setTimeout(() => {
    const inputs = document.querySelectorAll('.cs-inline-add-row .cs-inline-input');
    // 마지막에 추가된 행의 Item 입력칸에 focus (4 inputs/row)
    const targetIdx = (survey._inlineAdd.rows.length - 1) * 4;
    if (inputs[targetIdx]) inputs[targetIdx].focus();
  }, 50);
}

// 빈 인라인 행 한 개 (행 배열의 idx 위치)
function inlineAddRow(survey, category, row, idx, baseNo) {
  const tr = el('tr', { class: 'cs-inline-add-row' });
  tr.append(el('td', { class: 'cs-no' }, String(baseNo + idx + 1)));

  const itemInput = el('input', {
    type: 'text', class: 'cs-inline-input',
    placeholder: 'Item', value: row.item || '',
  });
  const descInput = el('input', {
    type: 'text', class: 'cs-inline-input',
    placeholder: idx === 0 ? 'Description (엑셀 표 복사 후 Ctrl+V)' : 'Description',
    value: row.description,
  });
  const remarkInput = el('input', {
    type: 'text', class: 'cs-inline-input', placeholder: 'Remark', value: row.remark,
  });
  const statusSel = document.createElement('select');
  statusSel.className = 'cs-inline-input';
  for (const v of ['Open','Closed']) {
    const o = document.createElement('option'); o.value = v; o.textContent = v;
    if (v === row.status) o.selected = true;
    statusSel.append(o);
  }

  // 상태 변경 → row 객체에 반영
  itemInput.addEventListener('input',   () => { row.item        = itemInput.value; });
  descInput.addEventListener('input',   () => { row.description = descInput.value; });
  remarkInput.addEventListener('input', () => { row.remark      = remarkInput.value; });
  statusSel.addEventListener('change',  () => { row.status      = statusSel.value; });

  // 엑셀 붙여넣기 — 4컬럼 매핑 (Item/Desc/Remark/Status)
  const onPaste = (ev) => {
    const text = (ev.clipboardData || window.clipboardData).getData('text');
    if (!text) return;
    const isTabular = text.includes('\t') || /\r?\n/.test(text.trim());
    if (!isTabular) return;

    ev.preventDefault();
    const rows = text.split(/\r?\n/).filter(r => r.length > 0 || r === '');
    while (rows.length && rows[rows.length - 1].trim() === '') rows.pop();

    rows.forEach((rline, k) => {
      const cols = rline.split('\t');
      const targetIdx = idx + k;
      while (survey._inlineAdd.rows.length <= targetIdx) {
        survey._inlineAdd.rows.push({ item: '', description: '', remark: '', status: 'Open' });
      }
      const target = survey._inlineAdd.rows[targetIdx];
      if (cols[0] !== undefined && cols[0] !== '') target.item        = cols[0].trim();
      if (cols[1] !== undefined && cols[1] !== '') target.description = cols[1].trim();
      if (cols[2] !== undefined && cols[2] !== '') target.remark      = cols[2].trim();
      if (cols[3] !== undefined && cols[3] !== '') {
        const st = cols[3].trim();
        target.status = (st === 'Closed' || st.toLowerCase() === 'closed') ? 'Closed' : 'Open';
      }
    });
    render();
  };
  itemInput.addEventListener('paste',   onPaste);
  descInput.addEventListener('paste',   onPaste);
  remarkInput.addEventListener('paste', onPaste);

  // Enter는 다음 행 추가/이동, Escape는 취소
  for (const inp of [itemInput, descInput, remarkInput]) {
    inp.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter' && !ev.shiftKey) {
        ev.preventDefault();
        if (idx === survey._inlineAdd.rows.length - 1) {
          addBlankRow(survey, category);
        } else {
          const inputs = document.querySelectorAll('.cs-inline-add-row .cs-inline-input');
          // 4 inputs/row → 다음 행의 첫 input
          const nextIdx = (idx + 1) * 4;
          if (inputs[nextIdx]) inputs[nextIdx].focus();
        }
      }
      if (ev.key === 'Escape') {
        survey._inlineAdd = null;
        render();
      }
    });
  }

  const td0 = el('td'); td0.append(itemInput);
  const td1 = el('td'); td1.append(descInput);
  const td2 = el('td'); td2.append(remarkInput);
  const td3 = el('td', { style: 'text-align:center' }); td3.append(statusSel);
  tr.append(td0, td1, td2, td3);

  // 행 삭제 버튼
  const acts = el('td', { class: 'cs-actions' });
  const rm = el('button', {
    class: 'icon-btn danger', title: '이 빈 줄 삭제',
    onclick: () => {
      survey._inlineAdd.rows.splice(idx, 1);
      if (!survey._inlineAdd.rows.length) survey._inlineAdd = null;
      render();
    },
  });
  rm.innerHTML = `<svg viewBox="0 0 24 24" width="11" height="11" fill="none" stroke="currentColor" stroke-width="2">
    <path d="M3 6h18"/><path d="M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2"/>
    <path d="M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6"/></svg>`;
  acts.append(rm);
  tr.append(acts);
  return tr;
}

// 채워진 행만 골라서 한 번에 저장
async function saveAllInlineRows(survey, category) {
  if (!survey._inlineAdd) return;
  const valid = survey._inlineAdd.rows
    .filter(r => (r.item || '').trim() !== '' || (r.description || '').trim() !== '')
    .map(r => ({
      item:        (r.item || '').trim(),
      description: (r.description || '').trim(),
      remark:      (r.remark || '').trim(),
      status:      r.status || 'Open',
    }));
  if (!valid.length) {
    alert('저장할 항목이 없습니다. Item 또는 Description을 입력하세요.');
    return;
  }
  try {
    await api(`/api/cs/surveys/${survey.id}/findings`, {
      method: 'POST',
      body: JSON.stringify({ category, items: valid }),
    });
    survey._inlineAdd = null;
    await reloadData();
  } catch (err) { alert('저장 실패: ' + err.message); }
}

// ───────────── Survey 모달 (Overall Remark) ─────────────
let _modalCtx = null;  // { vesselId, quarter, surveyId? }

function openSurveyModal(vesselId, quarter, survey) {
  _modalCtx = { vesselId, quarter, surveyId: survey?.id };
  $('#cs-modal-title').textContent =
    survey ? `분기 수검 정보 — ${quarter}Q (${S.year})` : `${quarter}Q 신규 수검`;
  $('#cs-f-vendor').value = survey?.vendor || '';
  $('#cs-f-mgmt').value   = survey?.management || '';
  $('#cs-f-date').value   = survey?.inspection_date || '';
  $('#cs-f-remark').value = survey?.overall_remark || '';
  $('#cs-survey-modal').hidden = false;
  document.body.style.overflow = 'hidden';
}

function closeSurveyModal() {
  $('#cs-survey-modal').hidden = true;
  document.body.style.overflow = '';
  _modalCtx = null;
}

async function saveSurveyModal() {
  if (!_modalCtx) return;
  const payload = {
    vendor:          $('#cs-f-vendor').value || null,
    management:      $('#cs-f-mgmt').value.trim() || null,
    inspection_date: $('#cs-f-date').value || null,
    overall_remark:  $('#cs-f-remark').value.trim() || null,
  };
  try {
    if (_modalCtx.surveyId) {
      await api(`/api/cs/surveys/${_modalCtx.surveyId}`, {
        method: 'PUT', body: JSON.stringify(payload),
      });
    } else {
      await api('/api/cs/surveys', {
        method: 'POST',
        body: JSON.stringify({
          vessel_id: _modalCtx.vesselId,
          year: S.year,
          quarter: _modalCtx.quarter,
          ...payload,
        }),
      });
    }
    closeSurveyModal();
    await reloadData();
  } catch (err) { alert('저장 실패: ' + err.message); }
}

async function deleteSurvey(survey) {
  if (!confirm(`${survey.quarter}Q 수검 데이터를 삭제하시겠습니까?\n세부 항목 ${survey.total_count}건도 함께 삭제됩니다.`)) return;
  try {
    await api(`/api/cs/surveys/${survey.id}`, { method: 'DELETE' });
    S.expandedSurveys.delete(survey.id);
    await reloadData();
  } catch (err) { alert('삭제 실패: ' + err.message); }
}

// ───────────── Data Reload ─────────────
async function reloadData() {
  const url = `/api/cs/surveys?year=${S.year}` +
              (S.activeTab !== 'all' ? `&supervisor_id=${S.activeTab}` : '');
  let data = await api(url);
  // 전체 탭에서는 숨김 감독 담당 vessel 제외
  if (S.activeTab === 'all' && S.hiddenVesselIds.size) {
    data = data.filter(item => !S.hiddenVesselIds.has(item.vessel.id));
  }
  S.data = data;
  renderContext();
  render();
}

// ───────────── 첨부 모달 (Daily 업무관리와 동일 패턴) ─────────────
let _csAttachSurvey = null;

async function openAttachModal(survey) {
  _csAttachSurvey = survey;
  $('#cs-attach-subtitle').textContent =
    `· ${survey.year} ${survey.quarter}Q ${survey.vendor || ''}`;
  await renderCsAttachGrid();
  $('#cs-attach-modal').hidden = false;
  document.body.style.overflow = 'hidden';
}

async function closeAttachModal() {
  $('#cs-attach-modal').hidden = true;
  document.body.style.overflow = '';
  _csAttachSurvey = null;
  await reloadData();   // 카운트 뱃지 갱신
}

async function renderCsAttachGrid() {
  const grid = $('#cs-attach-grid');
  grid.innerHTML = '';
  if (!_csAttachSurvey) return;
  let items = [];
  try {
    items = await api(`/api/cs/surveys/${_csAttachSurvey.id}/attachments`);
  } catch (_) {}
  if (!items.length) {
    grid.append(el('div', { class: 'attach-empty' },
      '첨부 파일이 없습니다. 위 영역으로 파일을 드래그하거나 클릭해 업로드하세요.'));
    return;
  }
  for (const a of items) grid.append(csAttachItemEl(a));
}

function csAttachItemEl(a) {
  const item = el('div', { class: 'attach-item' });

  const thumb = el('div', { class: 'attach-thumb' });
  const isImg = /\.(jpe?g|png|gif|webp|bmp|heic|heif)$/i.test(a.filename);
  const isPdf = /\.pdf$/i.test(a.filename);
  if (isImg) {
    thumb.append(el('img', {
      src: `/api/cs/attachments/${a.id}?inline=1`,
      alt: a.filename, loading: 'lazy',
    }));
  } else {
    thumb.append(el('div', { class: 'attach-file-icon' },
      isPdf ? 'PDF' : (a.filename.split('.').pop() || 'FILE').toUpperCase().slice(0, 4)));
  }
  item.append(thumb);

  const meta = el('div', { class: 'attach-meta' },
    el('a', {
      href: `/api/cs/attachments/${a.id}` + (isImg || isPdf ? '?inline=1' : ''),
      target: (isImg || isPdf) ? '_blank' : '_self',
      class: 'attach-name',
    }, a.filename),
    el('span', { class: 'attach-size' }, formatFileSize(a.file_size)),
  );
  item.append(meta);

  const rm = el('button', {
    class: 'icon-btn danger attach-rm',
    title: '삭제',
    onclick: () => deleteCsAttach(a.id),
  });
  rm.innerHTML = `<svg viewBox="0 0 24 24" width="11" height="11" fill="none" stroke="currentColor" stroke-width="2">
    <path d="M3 6h18"/><path d="M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2"/>
    <path d="M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6"/></svg>`;
  item.append(rm);
  return item;
}

function formatFileSize(b) {
  if (!b) return '';
  if (b < 1024) return b + ' B';
  if (b < 1048576) return (b / 1024).toFixed(1) + ' KB';
  return (b / 1048576).toFixed(1) + ' MB';
}

async function uploadCsFiles(files) {
  if (!_csAttachSurvey || !files || !files.length) return;
  const sid = _csAttachSurvey.id;
  for (const f of files) {
    if (f.size > 20 * 1024 * 1024) {
      alert(`"${f.name}" 은 20MB를 초과합니다.`);
      continue;
    }
    const fd = new FormData();
    fd.append('file', f);
    try {
      await api(`/api/cs/surveys/${sid}/attachments`, { method: 'POST', body: fd });
    } catch (err) {
      alert(`"${f.name}" 업로드 실패: ${err.message}`);
    }
  }
  await renderCsAttachGrid();
}

async function deleteCsAttach(aid) {
  if (!confirm('이 첨부파일을 삭제하시겠습니까?')) return;
  try {
    await api(`/api/cs/attachments/${aid}`, { method: 'DELETE' });
    await renderCsAttachGrid();
  } catch (err) { alert('삭제 실패: ' + err.message); }
}

// ───────────── Init ─────────────
async function loadSupervisors() {
  S.supervisors = await api('/api/supervisors');
  // 숨김 감독이 담당하는 vessel ID 캐싱 (전체 탭에서 제외용)
  S.hiddenVesselIds = new Set();
  const hiddenSups = S.supervisors.filter(isHiddenSupervisor);
  for (const sup of hiddenSups) {
    try {
      const vessels = await api(`/api/vessels?supervisor_id=${sup.id}`);
      for (const v of vessels) S.hiddenVesselIds.add(v.id);
    } catch (_) {}
  }
}

(async function init() {
  try {
    await loadSupervisors();
    // 본인 감독 탭 자동 선택 (단, 숨김 감독이면 전체로)
    if (S.user.supervisor_id) {
      const sup = S.supervisors.find(s => s.id === S.user.supervisor_id);
      if (sup && !isHiddenSupervisor(sup)) {
        S.activeTab = S.user.supervisor_id;
      }
    }
    // 활성 탭이 우연히 숨김 감독이면 전체로 복귀
    const activeSup = S.supervisors.find(s => s.id == S.activeTab);
    if (activeSup && isHiddenSupervisor(activeSup)) S.activeTab = 'all';
    renderTabs();
    $('#cs-year-label').textContent = S.year;
    await reloadData();

    // 이벤트
    $('#cs-year-prev').addEventListener('click', async () => {
      S.year--;
      $('#cs-year-label').textContent = S.year;
      S.expandedSurveys.clear();
      await reloadData();
    });
    $('#cs-year-next').addEventListener('click', async () => {
      S.year++;
      $('#cs-year-label').textContent = S.year;
      S.expandedSurveys.clear();
      await reloadData();
    });

    // 검색 — 실시간 필터링 (재요청 없이 클라에서)
    const searchInput = $('#cs-search');
    const clearBtn = $('#cs-search-clear');
    searchInput.addEventListener('input', (e) => {
      S.search = e.target.value;
      clearBtn.hidden = !S.search;
      renderContext();
      render();
    });
    searchInput.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && S.search) {
        S.search = '';
        searchInput.value = '';
        clearBtn.hidden = true;
        renderContext();
        render();
      }
    });
    clearBtn.addEventListener('click', () => {
      S.search = '';
      searchInput.value = '';
      clearBtn.hidden = true;
      searchInput.focus();
      renderContext();
      render();
    });

    // 모달
    $('#cs-survey-modal').addEventListener('click', (ev) => {
      if (ev.target.dataset.closeCs === '1') closeSurveyModal();
    });
    $('#cs-btn-save').addEventListener('click', saveSurveyModal);

    // 첨부 모달
    $('#cs-attach-modal').addEventListener('click', (ev) => {
      if (ev.target.dataset.closeCsa === '1') closeAttachModal();
    });
    // dropzone — 클릭으로 파일 선택
    const dz = $('#cs-attach-dropzone');
    const fi = $('#cs-attach-file-input');
    dz.addEventListener('click', () => fi.click());
    fi.addEventListener('change', () => {
      uploadCsFiles(fi.files);
      fi.value = '';
    });
    // 드래그 앤 드롭
    dz.addEventListener('dragover', (e) => { e.preventDefault(); dz.classList.add('dragover'); });
    dz.addEventListener('dragleave', () => dz.classList.remove('dragover'));
    dz.addEventListener('drop', (e) => {
      e.preventDefault();
      dz.classList.remove('dragover');
      uploadCsFiles(e.dataTransfer.files);
    });

    document.addEventListener('keydown', (ev) => {
      if (ev.key === 'Escape') {
        if (!$('#cs-attach-modal').hidden) closeAttachModal();
        else if (!$('#cs-survey-modal').hidden) closeSurveyModal();
      }
    });
  } catch (err) {
    console.error(err);
    alert('초기 로드 실패: ' + err.message);
  }
})();
