'use strict';

// Thin WebMCP adapter for the Daily page. Business logic and authorization stay
// in the page/API; unsupported browsers get no globals, network calls or UI.
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.TRMTDailyWebMCP = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  const STATUS = ['', 'Open', 'InProgress', 'Closed'];
  const PRIORITY = ['', 'Normal', 'Urgent', 'Next DD', 'COC & Flag'];

  function getModelContext(scope) {
    if (!scope) return null;
    // Current Chrome exposes WebMCP on document; retain navigator fallback for
    // earlier origin-trial builds during the standards transition.
    const current = scope.document?.modelContext;
    if (typeof current?.registerTool === 'function') return current;
    const legacy = scope.navigator?.modelContext;
    return typeof legacy?.registerTool === 'function' ? legacy : null;
  }

  function register(modelContext, handlers) {
    if (!modelContext || typeof modelContext.registerTool !== 'function') {
      return { supported: false, registered: [] };
    }
    if (!handlers || ['getPageContext', 'setIssueFilters', 'openIssue']
      .some(name => typeof handlers[name] !== 'function')) {
      throw new TypeError('WebMCP handlers are incomplete');
    }

    let execution = Promise.resolve();
    const serial = fn => input => {
      const next = execution.then(() => fn(input));
      execution = next.catch(() => {});
      return next;
    };
    const tools = [
      {
        name: 'trmt_get_page_context',
        description: 'Read a bounded summary of the current TRMT Daily page, filters, and visible issues. Makes no changes.',
        inputSchema: { type: 'object', properties: {}, additionalProperties: false },
        execute: serial(() => handlers.getPageContext()),
      },
      {
        name: 'trmt_set_issue_filters',
        description: 'Set Daily-page issue filters and refresh the visible list. This only changes the current UI; it does not modify server data.',
        inputSchema: {
          type: 'object',
          properties: {
            query: { type: 'string', maxLength: 120, description: 'Title/detail/action search text; empty clears it.' },
            vessel_name: { type: 'string', maxLength: 80, description: 'Exact assigned vessel name; empty clears it.' },
            status: { type: 'string', enum: STATUS, description: 'Empty means the current sub-tab default.' },
            priority: { type: 'string', enum: PRIORITY, description: 'Empty clears the priority filter.' },
          },
          additionalProperties: false,
        },
        execute: serial(input => handlers.setIssueFilters(input || {})),
      },
      {
        name: 'trmt_open_issue',
        description: 'Reveal and highlight an existing issue on the Daily page by numeric ID. Read/UI-only; it never opens an editor or writes data.',
        inputSchema: {
          type: 'object',
          properties: { issue_id: { type: 'integer', minimum: 1 } },
          required: ['issue_id'],
          additionalProperties: false,
        },
        execute: serial(input => handlers.openIssue(input && input.issue_id)),
      },
    ];

    for (const tool of tools) modelContext.registerTool(tool);
    return { supported: true, registered: tools.map(tool => tool.name), tools: Object.freeze(tools) };
  }

  return { register, getModelContext, STATUS, PRIORITY };
});
