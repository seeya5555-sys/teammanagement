/* 브레인 — 데쿠 memory 지식 그래프. 2D(force-graph) / 3D(3d-force-graph) 전환. */
(function () {
  'use strict';
  const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
  const CATS = [
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
  let showSup = false;

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
  function viewData() {
    const nodes = all.nodes.filter(visible);
    const ids = new Set(nodes.map((n) => n.id));
    const links = all.links.filter((l) => ids.has(l._s) && ids.has(l._t)).map((l) => ({ source: l._s, target: l._t }));
    return { nodes, links };
  }
  function color(n) {
    const base = (CAT[n.cat] || CAT.archive).color;
    if (query && !matches(n)) return base + '33';
    if (n.superseded) return base + '66';
    return base;
  }
  function size(n) { return 2 + Math.min(10, Math.sqrt((outs.get(n.id) || []).length + (ins.get(n.id) || []).length) * 1.6); }

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
        .onNodeClick((n) => { focus3d(n); select(n.id); });
    } else {
      graph = ForceGraph()(el).width(w).height(h)
        .backgroundColor(css('--surface') || '#FFFFFF')
        .nodeLabel((n) => esc(n.title))
        .nodeVal(size)
        .linkColor(() => css('--border') || '#D3D1C7').linkWidth(0.6)
        .nodeCanvasObject(draw2d).nodePointerAreaPaint((n, c, ctx) => {
          ctx.fillStyle = c; ctx.beginPath(); ctx.arc(n.x, n.y, size(n) + 2, 0, 2 * Math.PI); ctx.fill();
        })
        .onNodeClick((n) => { graph.centerAt(n.x, n.y, 600); select(n.id); });
    }
    graph.graphData(viewData());
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
  function focus3d(n) {
    const d = 90, k = 1 + d / Math.hypot(n.x || 1, n.y || 1, n.z || 1);
    graph.cameraPosition({ x: n.x * k, y: n.y * k, z: n.z * k }, n, 900);
  }
  function refresh() {
    if (!graph) return;
    graph.graphData(viewData());
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
    if (n.superseded) h += '<span class="br-badge" style="background:var(--red-bg);color:var(--red)">⛔ 폐기</span>';
    h += `<h2>${esc(n.title)}</h2>`;
    const sub = [n.type, n.section, n.date, n.updated && ('수정 ' + n.updated)].filter(Boolean).map(esc).join(' · ');
    if (sub) h += `<div class="sub">${sub}</div>`;
    if (n.summary) h += `<div class="sum">${esc(n.summary)}</div>`;
    if (n.cat === 'personal' && !n.hub) h += '<div class="br-note">본문은 TRMT 앱 \'마음 기록\'에서만 열람해.</div>';
    if (n.body) h += `<h4>본문</h4><div class="br-text">${esc(n.body)}</div>`;
    h += `<h4>연결된 노트 ${o.length}</h4><ul class="br-links">${o.map(li).join('') || '<li style="color:var(--text-tertiary);cursor:default">없음</li>'}</ul>`;
    h += `<h4>백링크 ${i.length}</h4><ul class="br-links">${i.map(li).join('') || '<li style="color:var(--text-tertiary);cursor:default">없음</li>'}</ul>`;
    box.innerHTML = h;
    box.scrollTop = 0;
    if (mode === '2d' && graph) graph.nodeCanvasObject(draw2d); // 선택 테두리 다시 그림
  }
  $('br-detail').addEventListener('click', (e) => {
    const t = e.target.closest('li[data-id]'); if (!t) return;
    const n = byId.get(t.dataset.id); if (!n) return;
    if (!visible(n)) { enabled.add(n.cat); if (n.superseded) { showSup = true; $('br-sup').checked = true; } renderCats(); refresh(); }
    setTimeout(() => { if (mode === '2d' && n.x != null) graph.centerAt(n.x, n.y, 600); else if (n.x != null) focus3d(n); }, 50);
    select(n.id);
  });

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
  document.querySelectorAll('.br-seg button').forEach((b) => b.addEventListener('click', () => {
    if (b.dataset.mode === mode) return;
    document.querySelectorAll('.br-seg button').forEach((x) => x.classList.toggle('on', x === b));
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
    build();
  }).catch((err) => {
    const e = $('br-empty'); e.hidden = false; e.textContent = '브레인 데이터를 못 불러왔어 (' + err.message + ').';
    $('br-meta').textContent = '';
  });
})();
