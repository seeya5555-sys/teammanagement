/* 브레인 — 데쿠 중심 세컨브레인(기억·자동화·업데이트 이력). 2D(force-graph) / 3D(3d-force-graph) 전환. */
(function () {
  'use strict';
  const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
  const CATS = [
    { key: 'hub', label: '데쿠·허브', color: css('--accent') || '#1D5B94' },
    { key: 'automation', label: '자동화', color: css('--green-border') || '#97C459' },
    { key: 'update', label: '업데이트 이력', color: css('--amber-dot') || '#EF9F27' },
    { key: 'vessel', label: '선박', color: css('--blue') || '#185FA5' },
    { key: 'issue', label: 'TRMT 현안', color: css('--coral-dot') || '#D85A30' },
    { key: 'closed', label: '완료 현안(이력)', color: css('--border-strong') || '#B4B2A9' },
    { key: 'mail', label: '메일', color: css('--purple') || '#6B4E71' },
    { key: 'knowledge', label: '지식자료(위키·도면·스킬)', color: css('--teal') || '#0F6E56' },
    { key: 'project', label: 'Projects · 진행', color: css('--coral-dot') || '#D85A30' },
    { key: 'area', label: 'Areas · 영역', color: css('--blue-dot') || '#378ADD' },
    { key: 'resource', label: 'Resources · 자료', color: css('--teal-dot') || '#1D9E75' },
    { key: 'archive', label: 'Archive · 완료', color: css('--gray-dot') || '#888780' },
    { key: 'personal', label: '개인 기록', color: css('--purple-dot') || '#8E6F92' },
  ];
  const CAT = Object.fromEntries(CATS.map((c) => [c.key, c]));
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  let all = { nodes: [], links: [] };
  let byId = new Map();
  let outs = new Map(), ins = new Map();
  let mode = '2d', graph = null, selected = null, query = '';
  const enabled = new Set(CATS.map((c) => c.key));
  enabled.delete('closed');            // 완료 현안은 기본 숨김 — 분류에서 켜면 보임
  let showSup = false;
  let focusMode = true;            // 데쿠 중심: 데쿠에서 2단계 + 펼친 노드의 이웃만
  const expanded = new Set();
  const STATUS = { ok: css('--green-border') || '#97C459', running: css('--teal-dot') || '#1D9E75',
                   fail: css('--red-strong') || '#E24B4A', disabled: css('--border-strong') || '#B4B2A9' };
  function nbrs(id) { return (outs.get(id) || []).concat(ins.get(id) || []); }
  function focusSet() {
    const keep = new Set();
    if (!byId.has('deku')) return null;
    let frontier = ['deku']; keep.add('deku');
    for (let d = 0; d < 2; d++) {
      const nx = [];
      frontier.forEach((id) => (outs.get(id) || []).forEach((t) => { if (!keep.has(t)) { keep.add(t); nx.push(t); } }));
      frontier = nx;
    }
    expanded.forEach((id) => { keep.add(id); nbrs(id).forEach((t) => keep.add(t)); });
    if (selected) keep.add(selected);
    return keep;
  }

  function visible(n) {
    if (!enabled.has(n.cat)) return false;
    if (n.superseded && !showSup) return false;
    return true;
  }
  function matches(n) {
    if (!query) return true;
    const q = query.toLowerCase();
    return (n.title || '').toLowerCase().includes(q) || (n.id || '').toLowerCase().includes(q)
      || (n.summary || '').toLowerCase().includes(q);
  }
  let todayPick = null;               // Today 칩 선택 시 해당 노드 묶음만
  function viewData() {
    const fs = focusMode ? focusSet() : null;
    let nodes = all.nodes.filter((n) => visible(n) && (!fs || fs.has(n.id) || (query && matches(n))));
    if (todayPick) {
      const keep = new Set(todayPick);
      todayPick.forEach((id) => nbrs(id).forEach((t) => { const x = byId.get(t); if (x && x.cat === 'vessel') keep.add(t); }));
      keep.add('deku');
      nodes = all.nodes.filter((n) => keep.has(n.id) && (visible(n) || n.id === 'deku'));  // Today=데쿠 중심 대신, 분류·폐기 필터는 유지
    }
    const ids = new Set(nodes.map((n) => n.id));
    const links = all.links.filter((l) => ids.has(l._s) && ids.has(l._t)).map((l) => ({ source: l._s, target: l._t }));
    return { nodes, links };
  }
  function color(n) {
    const base = ((n.cat === 'automation' || n.cat === 'mail' || (n.cat === 'issue' && n.status === 'fail')) && STATUS[n.status]) || (CAT[n.cat] || CAT.archive).color;
    if (query && !matches(n)) return base + '33';
    if (n.superseded) return base + '66';
    return base;
  }
  function size(n) {
    if (n.id === 'deku') return 14;
    if (n.cat === 'hub') return 8;
    return 2 + Math.min(10, Math.sqrt((outs.get(n.id) || []).length + (ins.get(n.id) || []).length) * 1.6);
  }

  function build() {
    const el = $('br-canvas');
    el.innerHTML = '';
    const w = el.clientWidth, h = el.clientHeight;
    if (mode === '3d') {
      graph = ForceGraph3D()(el).width(w).height(h)
        .backgroundColor(css('--nav-bg') || '#26241E')
        .nodeLabel((n) => esc(n.title))
        .nodeColor(color).nodeVal(size).nodeOpacity(0.92)
        .linkColor(() => 'rgba(201,198,188,0.28)').linkWidth(0.4)
        .onNodeClick((n) => { expand(n); focus3d(n); select(n.id); });
    } else {
      graph = ForceGraph()(el).width(w).height(h)
        .backgroundColor(css('--surface') || '#FFFFFF')
        .nodeLabel((n) => esc(n.title))
        .nodeVal(size)
        .linkColor(() => css('--border') || '#D3D1C7').linkWidth(0.6)
        .nodeCanvasObject(draw2d).nodePointerAreaPaint((n, c, ctx) => {
          ctx.fillStyle = c; ctx.beginPath(); ctx.arc(n.x, n.y, size(n) + 2, 0, 2 * Math.PI); ctx.fill();
        })
        .onNodeClick((n) => { expand(n); graph.centerAt(n.x, n.y, 600); select(n.id); });
    }
    graph.graphData(pinCenter(viewData()));
    graph.cooldownTicks(200);
  }
  function draw2d(n, ctx, scale) {
    const r = size(n);
    ctx.beginPath(); ctx.arc(n.x, n.y, r, 0, 2 * Math.PI);
    ctx.fillStyle = color(n); ctx.fill();
    if (selected === n.id) { ctx.lineWidth = 2 / scale; ctx.strokeStyle = css('--text-primary'); ctx.stroke(); }
    const big = r >= 6 || selected === n.id || (query && matches(n));
    if (big || scale > 2.2) {
      const fs = Math.max(10 / scale, 2.4);
      ctx.font = `${fs}px -apple-system, "Pretendard", sans-serif`;
      ctx.fillStyle = (query && !matches(n)) ? (css('--text-tertiary') + '66') : css('--text-secondary');
      ctx.textAlign = 'center'; ctx.textBaseline = 'top';
      ctx.fillText(n.title || n.id, n.x, n.y + r + 1.5 / scale);
    }
  }
  function expand(n) {
    if (!focusMode || expanded.has(n.id)) return;
    expanded.add(n.id); refresh();
  }
  function pinCenter(d) { const c = d.nodes.find((n) => n.id === 'deku'); if (c) { c.fx = 0; c.fy = 0; if (mode === '3d') c.fz = 0; } return d; }
  function focus3d(n) {
    const x = n.x || 0, y = n.y || 0, z = n.z || 0, r = Math.hypot(x, y, z);
    const d = 120, u = r < 1 ? [0, 0, 1] : [x / r, y / r, z / r];
    graph.cameraPosition({ x: x + u[0] * d, y: y + u[1] * d, z: z + u[2] * d }, { x, y, z }, 900);
  }

  function refresh() {
    if (!graph) return;
    graph.graphData(pinCenter(viewData()));
    if (mode === '3d') graph.nodeColor(color); // 3D 는 재적용해야 반영됨
  }

  function li(id) {
    const n = byId.get(id); if (!n) return '';
    const c = (CAT[n.cat] || CAT.archive).color;
    return `<li data-id="${esc(id)}"><span class="br-dot" style="background:${c}"></span>${esc(n.title)}${n.superseded ? ' ⛔' : ''}</li>`;
  }
  function select(id) {
    selected = id;
    const n = byId.get(id); const box = $('br-detail');
    if (!n) { box.innerHTML = '<div class="ph">노드를 찾을 수 없어.</div>'; return; }
    const c = CAT[n.cat] || CAT.archive;
    const o = (outs.get(id) || []), i = (ins.get(id) || []);
    let h = `<span class="br-badge" style="background:${c.color}22;color:${c.color}">${esc(c.label)}</span>`;
    if (n.status) h += `<span class="br-badge" style="background:${STATUS[n.status] || '#ccc'}33;color:var(--text-primary)">${esc((n.cat === 'issue' ? { ok: 'Open', running: '진행 중', fail: '마감 지남' } : n.cat === 'mail' ? { ok: '회신 받음', running: '일부 회신', fail: '발송 실패', disabled: '회신 대기' } : { ok: '정상', running: '실행 중', fail: '실패', disabled: '꺼짐' })[n.status] || n.status)}</span>`;
    if (n.superseded) h += '<span class="br-badge" style="background:var(--red-bg);color:var(--red)">⛔ 폐기</span>';
    h += `<h2>${esc(n.title)}</h2>`;
    const sub = [n.type, n.section, n.date, n.updated && ('수정 ' + n.updated)].filter(Boolean).map(esc).join(' · ');
    if (sub) h += `<div class="sub">${sub}</div>`;
    if (n.summary) h += `<div class="sum">${esc(n.summary)}</div>`;
    if (n.cat === 'personal' && !n.hub) h += '<div class="br-note">본문은 TRMT 앱 \'마음 기록\'에서만 열람해.</div>';
    const go = openTarget(n);
    if (go) h += `<button type="button" class="br-open" data-open="${esc(n.id)}">${esc(go.label)}</button>`;
    if (n.body) h += `<h4>본문</h4><div class="br-text">${esc(n.body)}</div>`;
    h += `<h4>연결된 노트 ${o.length}</h4><ul class="br-links">${o.map(li).join('') || '<li style="color:var(--text-tertiary);cursor:default">없음</li>'}</ul>`;
    h += `<h4>백링크 ${i.length}</h4><ul class="br-links">${i.map(li).join('') || '<li style="color:var(--text-tertiary);cursor:default">없음</li>'}</ul>`;
    box.innerHTML = h;
    box.scrollTop = 0;
    if (mode === '2d' && graph) graph.nodeCanvasObject(draw2d); // 선택 테두리 다시 그림
  }
  const MAIL_PAGE = { daily: '/daily-mail', vetting: '/vetting-mail', class: '/class-mail', cs: '/cs-mail' };
  const inApp = !!(window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.trmtBrain);
  function openTarget(n) {
    if ((n.cat === 'issue' || n.cat === 'closed') && n.ref) return { label: 'Daily에서 이 현안 열기', web: '/?issue=' + encodeURIComponent(n.ref), app: { kind: 'issue', id: Number(n.ref) } };
    if (n.cat === 'vessel') return { label: 'Daily에서 이 선박 현안 보기', web: '/?vessel_id=' + encodeURIComponent(n.ref || ''), app: { kind: 'vessel', name: n.title } };
    if (n.cat === 'mail' && MAIL_PAGE[n.type]) return { label: '메일 자동화 화면 열기', web: MAIL_PAGE[n.type], app: { kind: 'mail', type: n.type } };
    return null;
  }
  $('br-detail').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-open]');
    if (b) {
      const t = openTarget(byId.get(b.dataset.open) || {}); if (!t) return;
      if (inApp) window.webkit.messageHandlers.trmtBrain.postMessage(t.app);
      else location.href = t.web;
      return;
    }
    const t = e.target.closest('li[data-id]'); if (!t) return;
    const n = byId.get(t.dataset.id); if (!n) return;
    if (!visible(n)) { enabled.add(n.cat); if (n.superseded) { showSup = true; $('br-sup').checked = true; } renderCats(); refresh(); }
    if (focusMode && !expanded.has(n.id)) { expanded.add(n.id); refresh(); }
    setTimeout(() => { if (mode === '2d' && n.x != null) graph.centerAt(n.x, n.y, 600); else if (n.x != null) focus3d(n); }, 50);
    select(n.id);
  });

  function renderToday() {
    const today = new Date(Date.now() + 9 * 3600e3).toISOString().slice(0, 10);
    const in7 = new Date(Date.now() + (9 + 24 * 7) * 3600e3).toISOString().slice(0, 10);
    const iss = all.nodes.filter((n) => n.cat === 'issue');
    const groups = [
      { k: 'overdue', label: '마감 지남', cls: 'fail', ids: iss.filter((n) => n.due && n.due < today) },
      { k: 'soon', label: '7일 내 마감', cls: 'warn', ids: iss.filter((n) => n.due && n.due >= today && n.due <= in7) },
      { k: 'mail', label: '회신 대기 메일', cls: '', ids: all.nodes.filter((n) => n.cat === 'mail' && n.status === 'disabled') },
      { k: 'auto', label: '실패 자동화', cls: 'fail', ids: all.nodes.filter((n) => n.cat === 'automation' && n.status === 'fail') },
    ];
    const box = $('br-today');
    box.innerHTML = '<span class="br-meta" style="align-self:center">Today</span>' + groups.map((g) =>
      `<button type="button" class="br-chip ${g.ids.length ? g.cls : ''}" data-k="${g.k}"><b>${g.ids.length}</b>${esc(g.label)}</button>`).join('');
    box.hidden = false;
    box.onclick = (e) => {
      const b = e.target.closest('.br-chip'); if (!b) return;
      const g = groups.find((x) => x.k === b.dataset.k);
      const same = b.classList.contains('on');
      box.querySelectorAll('.br-chip').forEach((x) => x.classList.remove('on'));
      todayPick = (same || !g.ids.length) ? null : g.ids.map((n) => n.id);
      if (todayPick) { b.classList.add('on'); g.ids.forEach((n) => enabled.add(n.cat)); renderCats(); }
      refresh();
      if (todayPick) select(todayPick[0]);
    };
  }
  function renderCats() {
    const counts = {};
    all.nodes.forEach((n) => { counts[n.cat] = (counts[n.cat] || 0) + 1; });
    $('br-cats').innerHTML = CATS.map((c) => `<li><label><input type="checkbox" data-cat="${c.key}" ${enabled.has(c.key) ? 'checked' : ''}>`
      + `<span class="br-dot" style="background:${c.color}"></span>${esc(c.label)}<span class="n">${counts[c.key] || 0}</span></label></li>`).join('');
  }
  $('br-cats').addEventListener('change', (e) => {
    const k = e.target.dataset.cat; if (!k) return;
    e.target.checked ? enabled.add(k) : enabled.delete(k); refresh();
  });
  $('br-sup').addEventListener('change', (e) => { showSup = e.target.checked; refresh(); });
  let qt;
  $('br-search').addEventListener('input', (e) => {
    clearTimeout(qt);
    qt = setTimeout(() => {
      query = e.target.value.trim(); refresh();
      if (query) { const hit = viewData().nodes.find(matches); if (hit) select(hit.id); }
    }, 200);
  });
  document.querySelectorAll('.br-view button').forEach((b) => b.addEventListener('click', () => {
    document.querySelectorAll('.br-view button').forEach((x) => x.classList.toggle('on', x === b));
    focusMode = b.dataset.view === 'focus'; if (focusMode) expanded.clear(); refresh();
  }));
  document.querySelectorAll('.br-seg:not(.br-view) button').forEach((b) => b.addEventListener('click', () => {
    if (b.dataset.mode === mode) return;
    document.querySelectorAll('.br-seg:not(.br-view) button').forEach((x) => x.classList.toggle('on', x === b));
    if (graph && graph._destructor) graph._destructor();
    mode = b.dataset.mode; build();
  }));
  let rt;
  window.addEventListener('resize', () => {
    clearTimeout(rt);
    rt = setTimeout(() => { const el = $('br-canvas'); if (graph) graph.width(el.clientWidth).height(el.clientHeight); }, 150);
  });

  fetch('/api/brain/graph', { credentials: 'same-origin' }).then((r) => {
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }).then((d) => {
    d.links.forEach((l) => { l._s = l.source; l._t = l.target; });
    all = d; byId = new Map(d.nodes.map((n) => [n.id, n]));
    d.links.forEach((l) => {
      if (!outs.has(l._s)) outs.set(l._s, []); outs.get(l._s).push(l._t);
      if (!ins.has(l._t)) ins.set(l._t, []); ins.get(l._t).push(l._s);
    });
    const real = d.nodes.filter((n) => !n.superseded).length;
    $('br-meta').textContent = `노트 ${d.nodes.length} (현행 ${real}) · 연결 ${d.links.length}` + (d.updated_at ? ` · 동기화 ${d.updated_at}` : '');
    renderCats();
    if (!d.nodes.length) {
      const e = $('br-empty'); e.hidden = false; e.textContent = '아직 동기화된 데이터가 없어. 맥 데쿠가 첫 동기화를 하면 그래프가 그려져.';
      return;
    }
    renderToday();
    build();
    if (byId.has('deku')) select('deku');
  }).catch((err) => {
    const e = $('br-empty'); e.hidden = false; e.textContent = '브레인 데이터를 못 불러왔어 (' + err.message + ').';
    $('br-meta').textContent = '';
  });
})();
