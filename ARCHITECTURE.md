# TRMT runtime architecture

## Runtime and boundaries

`app.py` remains the Flask application and WSGI compatibility surface.  It owns
configuration, the Flask instance, database primitives, authentication hooks,
and the historical public helper names.  The implementation loaded into that
namespace is split into five focused boundaries:

- `routes_core.py`: authentication, dashboard, issues, vessels, and survey
  routes;
- `ai_gemini.py`: report extraction/translation and AI-adjacent pure helpers;
- `routes_calendar_dock.py`: calendar, survey/vetting, expenses, and dock
  operations;
- `routes_dock_submit.py`: dock procurement/inquiry submission workflows;
- `routes_tail.py`: ShipWiki, fleet map, Class Status, iOS, and push routes.

The loader executes each boundary in the application namespace.  This is
intentional: decorators still register on the one Flask app, `import app` and
`wsgi:application` remain valid, and existing imports do not silently change.
New non-trivial code goes directly into an extracted boundary; `app.py` is not
the destination for new features.

## Database and persistent state

SQLite is the system of record at `instance/trmt.db`; `app.db` and a repository
root `trmt.db` are not production databases.  `schema.sql`, `init_db`, and the
idempotent `_auto_migrate` path define schema evolution.  Uploaded evidence,
PDF previews, STT audio, and push/idempotency state are persistent operational
state and must not be treated as disposable test fixtures.  Tests use isolated
temporary databases and must not send mail, push notifications, call SVMS, or
mutate production files.

## Deployment and rollback

Web delivery is `automation/oneshot/ship.sh web "message" [paths]`, which
pushes, waits for the server artifact SHA, and performs live verification.
The server autodeploy timer consumes the committed archive; it is not a Git
checkout.  Rollback is performed by the repository rollback tooling and keeps
`deploy/` out of application rollback payloads so an old deployer cannot undo
the safety gate.  A deployment is not complete until the live SHA and health
response are observed.  This implementation task deliberately does not
commit, push, deploy, or send external messages.

## External imports and side effects

`import app` must remain safe for test discovery: route registration may happen,
but tests must isolate database paths and avoid external network/API calls.
Gemini, Outlook, SVMS, APNs, and worker callbacks are explicit integration
edges.  A GET smoke test may render/read data only; write methods, file uploads,
external dispatch, and money actions require their existing auth and safety
gates.

## Testing discipline

`tests/fixtures/url_map_snapshot.json` is the route contract: rule, method set,
endpoint name, `strict_slashes`, and defaults are reviewed together.  The
authenticated HTML smoke gate exercises every non-API HTML GET route and
requires an explicit safe fixture for parameterized pages.  iOS `TRMTTests`
contains pure model/normalization tests and is generated from `project.yml`;
the workspace CI workflow runs `xcodegen` followed by `xcodebuild test`.

## Money path last

Money and external-dispatch routes remain in their existing boundaries and are
not moved as part of a structural extraction.  Any future money-path change
requires its dedicated safety review and tests before shipment.  Structural
work must preserve URLs, endpoint names, auth decorators, persistent-state
semantics, and the existing deploy/rollback contract.
