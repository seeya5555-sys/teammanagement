'use strict';
// Exercise the shipped functions with controlled promises: assertions measure
// dependency waves and request counts, not machine-speed-dependent thresholds.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../static/js/app.js'), 'utf8');
function extract(name, endMarker) {
  const start = source.indexOf(`async function ${name}(`);
  assert.ok(start >= 0, name);
  const end = source.indexOf(endMarker, start);
  assert.ok(end > start, endMarker);
  return source.slice(start, end);
}
function deferred() {
  let resolve;
  const promise = new Promise(r => { resolve = r; });
  return { promise, resolve };
}
(async () => {
  const events = [];
  const vessels = deferred(), issues = deferred();
  const context = vm.createContext({
    document: { getElementById: () => ({}) },
    S: { activeTab: 'all' },
    _vesselCache: new Map([['old', []]]),
    onlySupId: () => 7,
    loadSupervisors: async () => { events.push('supervisors'); },
    loadVessels: async id => { assert.equal(id, 7); events.push('vessels'); await vessels.promise; },
    loadIssues: async () => { events.push('issues'); await issues.promise; },
    ...Object.fromEntries(['renderTabs', 'renderVesselFilter', 'renderTabContext', 'render'].map(
      name => [name, () => events.push(name)])),
  });
  vm.runInContext(extract('reloadAll', '// ───────────── Event wiring'), context);
  const reload = context.reloadAll();
  await new Promise(setImmediate);
  assert.deepEqual(events, ['supervisors', 'vessels', 'issues']);
  assert.equal(context._vesselCache.size, 0);
  vessels.resolve();
  await new Promise(setImmediate);
  assert.ok(!events.includes('render'));
  issues.resolve();
  await reload;
  assert.equal(events.filter(x => x === 'render').length, 1);

  let requests = 0;
  const rows = [{ id: 2, name: 'Test vessel' }];
  const cacheContext = vm.createContext({
    S: {}, _vesselCache: new Map(),
    api: async () => { requests++; return rows; },
  });
  vm.runInContext(extract('loadVessels', '// Daily 사이드바'), cacheContext);
  vm.runInContext(extract('loadVesselsForSupervisor', '// ═════════'), cacheContext);
  await cacheContext.loadVessels(7);
  assert.equal(requests, 1);
  const warmRows = await cacheContext.loadVesselsForSupervisor('7');
  assert.deepEqual(warmRows, rows);
  assert.notEqual(warmRows, rows, 'modal consumer must not alias mutable page state');
  assert.equal(requests, 1, 'opening editor must reuse the current scoped roster');
  await cacheContext.loadVesselsForSupervisor(8);
  assert.equal(requests, 2, 'a different supervisor must not reuse the wrong scope');
  for (const [file, endpoint] of [['dd', 'dock'], ['brep', 'boarding']]) {
    const js = fs.readFileSync(path.join(__dirname, `../static/js/${file}.js`), 'utf8');
    assert.ok(js.includes(`/api/${endpoint}-reports/\${id}?metadata_only=1`));
  }
  console.log('PASS: reload dependency waves 3 -> 2; warm modal roster GET 1 -> 0; render once after both reads; report metadata callers wired.');
})().catch(error => { console.error(error); process.exitCode = 1; });
