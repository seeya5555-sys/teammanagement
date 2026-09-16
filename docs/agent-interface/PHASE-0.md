# TRMT Agent Interface — Phase 0 measurement gate

Phase 0 prevents protocol enthusiasm from being mistaken for measured value.
It makes no network calls and never reads production data or credentials.

## Representative read-only tasks

1. List active issues for a vessel with only summary fields.
2. Retrieve one issue by its explicit ID.
3. Produce a vessel overview from issue counts and nearest due date.

Later phases may add deployment status and page collaboration, but the same
three tasks are the cross-method comparison set.

## Commands

```bash
python3 scripts/trmt_agent_phase0.py routes --output /tmp/trmt-routes.json
python3 scripts/trmt_agent_phase0.py deep-links --output /tmp/trmt-links.json
python3 scripts/trmt_agent_phase0.py fixture-baseline
python3 scripts/trmt_agent_phase0.py compare /path/to/real-runs.jsonl
```

The fixture baseline reports serialized **bytes**, never claimed model tokens.
The compare command accepts token counts captured by the actual model client:

```json
{"case":"list_issues","variant":"current_ui","iteration":1,"input_tokens":1200,"output_tokens":90,"schema_tokens":0,"image_tokens":800,"duration_ms":4200,"success":true,"scope_violation":false,"side_effect":false}
```

Each task needs at least five uniquely numbered runs for all three variants: `current_ui`,
`task_tool`, and `webmcp`. Run them with the same account, model, data snapshot,
and cold/warm policy. Do not mix a live changing dataset into the comparison.
Token component fields must be mutually exclusive values reported by the client:
`input_tokens` excludes tool schema and image tokens, which belong in their own
fields. Image tokens are intentionally counted because avoiding screenshots is
one of the product goals.

## Adoption gate

A candidate is adopted for a task only when all are true:

- median total model tokens decrease by at least 30%;
- median duration does not increase;
- success rate does not fall;
- scope violations and side effects are both zero.

The aggregate decision is fail-closed: every representative task must pass.

## Security and deep-link gate

- The frozen fixture contains synthetic vessels and issues only.
- Phase 0 performs no writes and exposes no API key.
- Route guards are inventoried so `/api/ext/` API-key identity is not confused
  with login/Bearer user scope.
- The inventory is static: it reports decorators below `@route` (the wrappers
  Flask actually registers) but does not infer `before_request`, `add_url_rule`,
  or runtime wrappers. It is a navigation aid, not an authorization audit.
- Deep-link output lists static candidates only. A candidate is not considered
  working until a behavioral test proves navigation and selected record state.
- If an existing deep link completes a UI task with the same measurements,
  do not create an equivalent WebMCP tool.
