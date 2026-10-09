// Behavior-level tests of the shipped editor functions, without a browser/pixel harness.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.env.TRMT_INLINE_SOURCE || require('node:path').join(__dirname, '../static/js/app.js'), 'utf8');

function functionSource(name) {
  const match = new RegExp(`^(?:async )?function ${name}\\(`, 'm').exec(source);
  assert.ok(match, name);
  const rest = source.slice(match.index);
  const next = /\n(?:async )?function \w+\(/.exec(rest);
  return next ? rest.slice(0, next.index) : rest;
}
class Element {
  constructor(tag = 'div', visible = true) {
    this.tag = tag; this.visible = visible; this.isConnected = true;
    this.children = []; this.handlers = {}; this.value = ''; this.innerHTML = '';
    this.classList = {add() {}, remove() {}, toggle() {}};
  }
  getClientRects() { return this.visible ? [{}] : []; }
  append(...nodes) { this.children.push(...nodes.filter(Boolean)); }
  addEventListener(name, fn) { (this.handlers[name] ||= []).push(fn); }
  focus() { this.focused = true; }
  select() {}
  querySelector(selector) {
    const tags = selector.split(',');
    return this.children.find(c => tags.includes(c.tag))
      || this.children.map(c => c.querySelector?.(selector)).find(Boolean) || null;
  }
  querySelectorAll(selector) { return this.children.filter(c => c.tag === 'div'); }
  remove() { this.removed = true; this.isConnected = false; }
  async fire(name, extra = {}) {
    for (const fn of this.handlers[name] || []) await fn({stopPropagation() {}, preventDefault() {}, ...extra});
  }
}
function context() {
  const queued = [], notices = [];
  const c = {
    S: {_editing: null, expandedActions: new Set()}, window: {},
    document: {createElement: tag => new Element(tag), querySelectorAll: () => []},
    InlineEdit: {toast: msg => notices.push(msg)},
    setTimeout: fn => queued.push(fn), renderTable() {}, renderCards() {},
    todayISO: () => '2026-10-09', alert() {}, confirm: () => false,
    api: async () => ({}), reloadAll: async () => {},
    el: (tag, attrs = {}, ...children) => {
      const e = new Element(tag); Object.assign(e, attrs);
      for (const [k,v] of Object.entries(attrs)) if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
      e.append(...children); return e;
    },
  };
  c.window.InlineEdit = c.InlineEdit;
  vm.createContext(c);
  for (const name of ['dailyInlineEditBlocked', 'releaseDailyInlineEditor', 'startEditInline',
    'startEditSelect', 'startEditActionEntry', 'removeDailyActionDraft', 'renderActionCell', 'addActionInline']) vm.runInContext(functionSource(name), c);
  return {c, queued, notices};
}
const issue = () => ({id: 10, description: 'original', status: 'Open', actions: [{date: '2026-10-09', progress: 'existing'}]});

test('removed textarea lock no longer blocks detail editing after collapse/rerender', async () => {
  const {c} = context();
  c.S._editing = new Element(); c.S._editing.isConnected = false;
  const target = new Element();
  await c.startEditInline(target, issue(), 'description', 'textarea');
  assert.equal(c.S._editing, target);
  assert.equal(target.querySelector('textarea').value, 'original');
});
test('hidden desktop lock no longer blocks mobile select or progress editor', async () => {
  for (const kind of ['select', 'progress']) {
    const {c} = context(); c.S._editing = new Element('div', false);
    const target = new Element();
    if (kind === 'select') await c.startEditSelect(target, issue(), 'status', [['Open','Open']]);
    else c.startEditActionEntry(target, issue(), 0);
    assert.equal(c.S._editing, target);
  }
});
test('a connected visible draft is protected and an explicit explanation is given', async () => {
  const {c, notices} = context(); const active = new Element(); const input = new Element('textarea');
  active.append(input); c.S._editing = active;
  await c.startEditInline(new Element(), issue(), 'description', 'textarea');
  assert.equal(c.S._editing, active);
  assert.equal(input.focused, true);
  assert.equal(notices.length, 1);
});
test('old escaped editor callback cannot unlock a newer editing session', async () => {
  const {c} = context(); const old = new Element();
  await c.startEditInline(old, issue(), 'description', 'textarea');
  const oldInput = old.querySelector('textarea'); old.isConnected = false;
  const current = new Element(); await c.startEditInline(current, issue(), 'description', 'textarea');
  await oldInput.fire('keydown', {key: 'Escape'});
  assert.equal(c.S._editing, current);
});
test('mobile add selects visible card instead of hidden desktop representation', async () => {
  const {c, queued} = context(); const hidden = new Element('div', false), mobile = new Element();
  const mobileEntry = new Element(); mobile.querySelectorAll = () => [mobileEntry];
  c.document.querySelectorAll = () => [hidden, mobile];
  const calls = []; c.startEditActionEntry = (...args) => calls.push(args);
  const i = issue(); await c.addActionInline(i); mobileEntry._dailyAction = i.actions[1]; queued.shift()();
  assert.equal(calls.length, 1); assert.equal(calls[0][0], mobileEntry); assert.equal(calls[0][2], 1);
});
test('navigation before delayed add does not leave an orphan empty action', async () => {
  const {c, queued} = context(); const i = issue(); await c.addActionInline(i);
  assert.equal(i.actions.length, 2); queued.shift()(); assert.equal(i.actions.length, 1);
});
test('delayed add selects its own action object, not a later action', async () => {
  const {c, queued} = context(); const i = issue(); const visible = new Element();
  let selected; c.startEditActionEntry = (node, obj, idx) => { selected = idx; };
  c.document.querySelectorAll = () => [visible];
  await c.addActionInline(i);
  visible.children = [Object.assign(new Element(), {_dailyAction: i.actions[1]})];
  i.actions.push({progress:'another action'}); queued.shift()();
  assert.equal(selected, 1);
});
test('renderer assigns original action identity and index to each entry', () => {
  const {c} = context(); const i = issue(); i.actions.push({_new:true, progress:''});
  const wrap = c.renderActionCell(i);
  const entries = wrap.children[0].children;
  assert.equal(entries[0]._dailyAction, i.actions[0]);
  assert.equal(entries[1]._dailyAction, i.actions[1]);
  assert.equal(entries[1]['data-idx'], 1);
});
test('hidden textarea session is canceled; queued stale saves send no request', async () => {
  const {c} = context(); const old = new Element(); let requests = 0;
  c.api = async () => { requests++; };
  await c.startEditInline(old, issue(), 'description', 'textarea');
  const oldInput = old.querySelector('textarea'); oldInput.value = 'stale draft'; old.visible = false;
  const current = new Element(); await c.startEditInline(current, issue(), 'description', 'textarea');
  await oldInput.fire('keydown', {key: 'Enter', ctrlKey:true});
  assert.equal(requests, 0); assert.equal(c.S._editing, current);
});
test('hidden select scheduled blur cannot overwrite a newly opened editor', async () => {
  const {c, queued} = context(); const old = new Element(); let requests = 0;
  c.api = async () => { requests++; };
  await c.startEditSelect(old, issue(), 'status', [['Open','Open'],['Closed','Closed']]);
  const select = old.querySelector('select'); select.value = 'Closed'; await select.fire('blur');
  old.visible = false;
  const current = new Element(); await c.startEditInline(current, issue(), 'description', 'textarea');
  queued.shift()(); await Promise.resolve();
  assert.equal(requests, 0); assert.equal(c.S._editing, current);
});
test('detached select blur is canceled even before another edit starts', async () => {
  const {c, queued} = context(); const old = new Element(); let requests = 0;
  c.api = async () => { requests++; };
  await c.startEditSelect(old, issue(), 'status', [['Open','Open'],['Closed','Closed']]);
  const select = old.querySelector('select'); select.value = 'Closed'; await select.fire('blur');
  old.isConnected = false; queued.shift()(); await Promise.resolve();
  assert.equal(requests, 0); assert.equal(c.S._editing, null);
});
test('hidden action session is canceled without a late PATCH', async () => {
  const {c} = context(); const old = new Element(); let requests = 0;
  c.api = async () => { requests++; };
  c.startEditActionEntry(old, issue(), 0);
  const input = old.querySelector('textarea'); input.value = 'stale progress'; old.visible = false;
  const current = new Element(); await c.startEditInline(current, issue(), 'description', 'textarea');
  await input.fire('keydown', {key:'Enter', ctrlKey:true});
  assert.equal(requests, 0); assert.equal(c.S._editing, current);
});
test('blocked delayed add removes only own draft DOM in both representations', async () => {
  const {c, queued} = context(); const i = issue(); const desktop = new Element(), mobile = new Element();
  c.document.querySelectorAll = () => [desktop,mobile];
  await c.addActionInline(i);
  const draft = i.actions[1];
  const first = Object.assign(new Element(), {_dailyAction: draft});
  const second = Object.assign(new Element(), {_dailyAction: draft});
  const stored = Object.assign(new Element(), {_dailyAction: i.actions[0]});
  desktop.children = [stored,first]; mobile.children = [second];
  c.S._editing = new Element();
  queued.shift()();
  assert.equal(i.actions.length, 1); assert.equal(first.removed, true); assert.equal(second.removed, true);
  assert.equal(stored.removed, undefined); assert.ok(c.S._editing);
});

test('clicking a rendered existing progress entry opens the correct editor', async () => {
  const {c} = context(); const i = issue(); const wrap = c.renderActionCell(i);
  const entry = wrap.children[0].children[0];
  await entry.fire('click', {target:{closest:()=>null}});
  assert.equal(c.S._editing, entry);
  assert.ok(entry.querySelector('textarea'));
});
test('a visible detail editor still saves only its edited field', async () => {
  const {c} = context(); const target = new Element(); let request;
  c.api = async (url, options) => { request = {url,options}; };
  await c.startEditInline(target, issue(), 'description', 'textarea');
  const input = target.querySelector('textarea'); input.value = 'updated detail';
  await input.fire('keydown', {key:'Enter',ctrlKey:true});
  assert.equal(request.url, '/api/issues/10');
  assert.equal(request.options.method, 'PUT');
  assert.deepEqual(JSON.parse(request.options.body), {description:'updated detail'});
  assert.equal(c.S._editing, null);
});
