# TRMT runtime architecture

## Runtime and boundaries

`app.py` remains the Flask application and WSGI compatibility surface.  It owns
configuration, the Flask instance, database primitives, authentication hooks,
and the historical public helper names.  The implementation loaded into that
namespace is split into five focused boundaries:

Boundary contents below are measured from the route decorators actually present
in each file, not from intent.  Counts are `@app.route` declarations.

- `helpers_shared.py` (0 routes, loaded first): every helper or constant that
  two or more boundaries consume, or that `app.py` itself consumes — auth
  decorators (`login_required`, `admin_required`, `api_key_required`), Gemini
  call/config, dock-procure and SOA helpers, fleet/push helpers, automation
  task metadata.  Nothing route-shaped lives here;
- `routes_core.py` (59 routes): login/logout/auth, dashboard, issues, vessels,
  users/supervisors, widget, condition-survey CRUD (`/api/cs/surveys`), and the
  `/calendar`, `/condition-survey`, `/vetting-status`, `/dry-dock` **pages**;
- `ai_gemini.py` (21 routes): vetting CRUD (`/api/vettings`), findings and
  attachments, plus report extraction/translation and AI-adjacent pure helpers;
- `routes_calendar_dock.py` (204 routes): the `/api/ext/*` worker surface (74),
  calendar/report/expense/business-trip APIs, STT, and the money APIs
  (`/api/invoice`, `/api/fundreq`, `/api/aor`, `/api/reqgen`);
- `routes_dock_submit.py` (73 routes): dock procurement/inquiry/submit/yard
  workflows and the ShipWiki card surface (`/shipwiki`, `/api/shipwiki/*`);
- `routes_tail.py` (38 routes): Class Status, fleet map, iOS and `/api/ext/push`
  delivery, ShipWiki push callbacks, and `/dashboard/classic`.

Two naming traps follow from the measurement and are load-bearing when locating
code: **vetting APIs live in `ai_gemini.py`, not `routes_calendar_dock.py`**, and
the **`/calendar` page lives in `routes_core.py`** while only calendar *APIs* are
in `routes_calendar_dock.py`.  ShipWiki is split by direction: the card surface
is in `routes_dock_submit.py`, the push callbacks in `routes_tail.py`.

The loader executes each boundary in the application namespace.  This is
intentional: decorators still register on the one Flask app, `import app` and
`wsgi:application` remain valid, and existing imports do not silently change.
New non-trivial code goes directly into an extracted boundary; `app.py` is not
the destination for new features.

The development server does not watch these files by default: they never enter
`sys.modules`, and the reloader watches imported modules only.  `app.py` records
each loaded path in `EXTRACTED_BOUNDARY_PATHS` and passes it to `app.run` as
`extra_files`, so an edit to a boundary restarts the dev server instead of
silently doing nothing.  Production runs under gunicorn and is unaffected.

## Boundary coupling (read before any Blueprint work)

Because the boundaries share one namespace, cross-file dependencies are real but
undeclared — no `import` line records them.  Two consequences are handled by
`tests/test_boundary_dependency_graph.py`:

- a misspelled helper name is not a startup error but a **request-time
  `NameError`**, so `test_no_unresolved_names` performs the undefined-name check
  that a normal module structure would delegate to static analysis.  Name
  resolution uses `symtable`, the same scope analysis CPython compiles with, so
  function locals, arguments, comprehension variables, closure free variables and
  `global` declarations are distinguished correctly.  An earlier `ast.walk`
  version pooled every binding per file and therefore treated one function's
  local as a module-level provider, silently accepting a typo elsewhere in the
  same file; that under-detection is what the scope-aware version fixes.  This is
  the deliberate substitute for a linter here: `flake8`/`ruff` would report every
  cross-boundary free variable as F821 and require a blanket suppression, which
  detects nothing.  The residual, accepted cost is that IDE go-to-definition
  still does not cross a boundary; that is resolved only when boundaries become
  real modules with explicit imports;
- no top-level name may be provided by two boundaries.  If it were, the effective
  binding would be whichever file loads last, and "which boundary does this
  depend on" would stop being well defined;
- the dependency graph is frozen in
  `tests/fixtures/boundary_dependency_graph.json` (12 edges, 305 symbol uses)
  and is the concrete import list a Blueprint conversion has to satisfy.
  Load-time and request-time references are not distinguished, and dynamic
  access through `globals()` or `getattr` is invisible to it.

The graph used to be **cyclic** (every boundary depended on a sibling;
`routes_dock_submit.py` consumed 43 symbols from `routes_calendar_dock.py`).
The `helpers_shared.py` extraction resolved that: the transitive closure of
every cross-consumed symbol (146 module-level definitions) moved into one
shared file that loads before the other boundaries.  The resulting contract is
**sibling-free layering**, enforced by `test_layering_no_sibling_dependencies`:

- `helpers_shared.py` may depend only on `app.py`;
- every other boundary may depend only on `app.py` and `helpers_shared.py`;
- `app.py` may depend only on `helpers_shared.py`.

This is deliberately not called a DAG: `app.py` and `helpers_shared.py`
reference each other (shared helpers use `app.py` primitives; `init_db` and
`_auto_migrate` in `app.py` call five AOR helpers).  Both directions are
call-time-only and every module-level statement in `helpers_shared.py` is a
pure binding (constants, env reads, `threading.Lock()` — measured, no I/O), so
the pair behaves as one foundation layer.  Splitting a true foundation module
out of `app.py` would remove the mutual edge but is not required for the
per-boundary Blueprint move.

A boundary that starts referencing a sibling again fails the gate — the fix is
to move the shared helper into `helpers_shared.py` (if it is genuinely shared)
or keep it private to the consuming file.  Without this test, refreshing the
frozen fixture with `--update` would let a new cycle through as an apparently
reviewed change.  With the cycle gone, each boundary is independently movable:
a Blueprint conversion can now proceed one boundary at a time, importing only
`app.py` primitives and `helpers_shared` — the endpoint-rename churn (`bp.name`
prefixes, `url_for` call sites, the 396-entry contract snapshot) remains the
open cost and is a per-boundary, reviewable change.

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
requires an explicit safe fixture for parameterized pages.  `404.html` is
reached by no route, so it is exercised through the error handler instead; it
renders `url_for('dashboard')` and would otherwise be the one template an
endpoint rename could break unobserved.

Note what the route contract does **not** cover: it compares rule, method, and
endpoint name, so a route that keeps its URL while losing `@admin_required`
passes it.  `tests/test_money_path_guard_contract.py` closes that specific gap
for the money path with a static decorator check (including decorator order —
a guard placed above `@app.route` is discarded by Flask) and a runtime check
that all 43 money rule/method pairs reject anonymous and non-admin callers.
`tests/test_money_bulk_contract.py` covers the two highest-consequence money
routes that no test previously named, asserting final database state rather than
response counts: bulk approval must not re-approve already decided or in-flight
rows (double execution), and the decided-row purge must not delete live rows.
iOS `TRMTTests`
contains pure model/normalization tests and is generated from `project.yml`;
the workspace CI workflow runs `xcodegen` followed by `xcodebuild test`.

## Money path last

Money and external-dispatch routes remain in their existing boundaries and are
not moved as part of a structural extraction.  Any future money-path change
requires its dedicated safety review and tests before shipment.  Structural
work must preserve URLs, endpoint names, auth decorators, persistent-state
semantics, and the existing deploy/rollback contract.
