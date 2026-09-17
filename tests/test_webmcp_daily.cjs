'use strict';
const assert = require('node:assert/strict');
const { register, getModelContext } = require('../static/js/webmcp_daily.js');

async function main() {
  const docApi = { registerTool() {} };
  const navApi = { registerTool() {} };
  assert.equal(getModelContext({ document: { modelContext: docApi }, navigator: { modelContext: navApi } }), docApi);
  assert.equal(getModelContext({ document: {}, navigator: { modelContext: navApi } }), navApi);
  assert.equal(getModelContext({ document: { modelContext: {} }, navigator: { modelContext: navApi } }), navApi);
  assert.equal(getModelContext({ document: {}, navigator: {} }), null);
  assert.equal(getModelContext(null), null);
  assert.deepEqual(register(null, {}), { supported: false, registered: [] });
  assert.throws(() => register({ registerTool() {} }, {}), /incomplete/);

  const tools = [];
  const calls = [];
  const result = register({ registerTool: tool => tools.push(tool) }, {
    getPageContext: async () => ({ page: 'daily_issues' }),
    setIssueFilters: async input => { calls.push(['filters', input]); return { ok: true }; },
    openIssue: async id => { calls.push(['open', id]); return { opened: true }; },
  });
  assert.equal(result.supported, true);
  assert.deepEqual(result.registered, [
    'trmt_get_page_context', 'trmt_set_issue_filters', 'trmt_open_issue',
  ]);
  assert.equal(tools.length, 3);
  assert.equal(result.tools.length, 3);
  assert.equal(Object.isFrozen(result.tools), true);
  assert.deepEqual(result.tools, tools);
  assert.equal(tools[0].inputSchema.additionalProperties, false);
  assert.equal(tools[1].inputSchema.properties.query.maxLength, 120);
  assert.deepEqual(tools[1].inputSchema.properties.status.enum, ['', 'Open', 'InProgress', 'Closed']);
  assert.deepEqual(tools[2].inputSchema.required, ['issue_id']);
  assert.equal(tools.every(t => !/create|update|delete|save/i.test(t.description)), true);

  assert.deepEqual(await tools[0].execute({}), { page: 'daily_issues' });
  assert.deepEqual(await tools[1].execute({ status: 'Open' }), { ok: true });
  assert.deepEqual(await tools[2].execute({ issue_id: 7 }), { opened: true });
  assert.deepEqual(calls, [['filters', { status: 'Open' }], ['open', 7]]);

  const order = [];
  const serialTools = [];
  register({ registerTool: tool => serialTools.push(tool) }, {
    getPageContext: async () => { order.push('a-start'); await new Promise(r => setTimeout(r, 5)); order.push('a-end'); },
    setIssueFilters: async () => { order.push('b'); },
    openIssue: async () => {},
  });
  await Promise.all([serialTools[0].execute({}), serialTools[1].execute({})]);
  assert.deepEqual(order, ['a-start', 'a-end', 'b']);
  console.log('webmcp daily: 3 tools + unsupported-browser no-op PASS');
}

main().catch(err => { console.error(err); process.exitCode = 1; });
