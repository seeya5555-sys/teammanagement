# TRMT runtime architecture

## Runtime and boundaries

`app_core.py` owns configuration, the Flask instance and database primitives.
`app.py` owns authentication/request hooks, migration orchestration, Blueprint
registration and historical helper names (re-exported for compatibility).
Route and helper boundaries are **real imported modules**, not an exec-loaded
shared namespace. Production enters through `wsgi:application`.

- `helpers_shared.py`: shared auth decorators, Gemini, vetting, dock procurement,
  SOA, fleet/push and automation helpers. No routes.
- `routes_core.py`: login/logout, dashboard, issues, vessels, supervisors/users,
  widget, condition-survey CRUD, calendar/survey/report pages and Dock Manager shell.
- `ai_gemini.py`: vetting CRUD, findings/attachments and report extraction.
- `routes_calendar_dock.py`: the large worker/finance/report boundary:
  `/api/ext/*`, calendar, reports, expenses/trips, STT, invoice, fundreq, AOR,
  reqgen and remittance. `calendar_service.py`, `dock_report_projection.py`,
  `boarding_report_projection.py` and `report_export_service.py` already extract
  request-independent logic while preserving HTTP endpoint ownership.
- `routes_dock_submit.py`: dock procurement/inquiry/submit/yard workflows and
  the ShipWiki card surface.
- `routes_tail.py`: Class Status, fleet map, iOS/APNs push, ShipWiki callbacks
  and classic dashboard.
- `routes_dock_daily.py`: Dock Daily Report blocks, revisions, document
  import/export and SVMS synchronization.
- `routes_repair_request.py`: Repair Request lifecycle and vessel-name normalization.
- `routes_liscr.py`: LISCR jobs, profiles and runner claim/result operations.
- `routes_family_assets.py`: households, assets/evidence, salary/cash-flow closing,
  reconciliation and allowances; household membership and optimistic revisions
  are separate from business supervisor scoping.

Do not infer locations from names: vetting APIs live in `ai_gemini.py`; the
calendar **page** lives in `routes_core.py` while calendar **APIs** live in
`routes_calendar_dock.py`. ShipWiki cards and callbacks have different owners.

Route counts are intentionally not hard-coded here. Compare `app.url_map` with
`tests/fixtures/url_map_snapshot.json`; a failing snapshot is contract drift
to inspect, not permission to regenerate it blindly.

## Dependency contract

The current dependency direction is downward (arrows mean "imports"):

```text
Blueprints → app → helpers_shared → app_core
     └──────────────→ support/services → app_core
```

Modules may import lower layers directly. `app.py` imports Blueprint modules
only for registration; this bootstrap exception is not a business-logic edge.
Support libraries must not depend on application/route layers, and their own
subgraph must be acyclic. There is no longer an `app` ↔ `helpers_shared` cycle:
the shared foundation has already moved to `app_core`.

`tests/test_boundary_dependency_graph.py` enforces downward layering,
self-contained name resolution with `symtable`, no exec loading, no imported-name
redefinition, unique endpoint short names and acyclic support libraries.
`tests/fixtures/boundary_dependency_graph.json` freezes the import graph.
Definition conflicts are checked within the shared spine, not between unrelated
Blueprints (each legitimately defines its own `bp`). Dynamic `getattr` access
remains outside the static graph's coverage.

New logic should first become a request-independent service/projection under
the existing HTTP adapter. Preserve URLs, endpoint names, auth decorators and
JSON shapes. Moving endpoint ownership to another Blueprint is a separate
compatibility change even if the URL stays identical.

Load-bearing details:

- Endpoint names are data: `base.html` navigation and help-manual lookup depend
  on them. A Blueprint rename can break HTML without changing any API URL.
- Mutable cross-module state must be updated on its owner. In particular,
  migration code updates `routes_calendar_dock.SOA_REVIEW_SCHEMA_DEGRADED`
  explicitly; rebinding an imported copy does not update the route's state.
- Patching `app.query` does not patch a previously imported `query` binding in
  a route. Patch the consuming module, or use the compatibility proxy in
  `tests/source_bundle.py` where legacy tests require shared-namespace semantics.

## Database and persistent state

SQLite is the system of record at `instance/trmt.db`; repository-root
`trmt.db` and `app.db` are not production databases. Uploaded evidence, PDF
previews, STT audio and push/idempotency state are persistent operational data.

`app_core.get_db()` holds one connection per Flask application context, enables
foreign keys/WAL and a busy timeout, and closes it at teardown.
`query(one=True)` returns one `sqlite3.Row` or `None`; normal `query()`
returns a list. Both paths close the cursor on success and row-decoding errors.
`execute()` generally commits immediately, with explicit transaction exceptions:
several helper calls do not automatically form one transaction.

`schema.sql`, `init_db` and idempotent `_auto_migrate` define schema evolution.
Extracted slices in `migration_steps.py` preserve ordered additive repairs and
independent failure boundaries. Foundation and management-metadata tests prove
legacy-row preservation, repeat-run equivalence and failure isolation. Remaining
migrations stay in `app.py` until they have equivalent domain-specific gates.

`wsgi.py` also mounts an independent Dock Manager Flask application at
`/drydock` using `DispatcherMiddleware`. Its `fleet.db` remains separate;
`drydock_integration.py` provides the SSO/admin/SQLite integration boundary.
A cross-database workflow is not implicitly atomic.

## Client and worker data flow

```text
Browser → Jinja + scoped JS → session/CSRF → Blueprint → service/SQL → TRMT DB
iOS View → MainActor ViewModel → Repository or feature API → APIClient actor
         → Bearer/request guards → same Blueprint/service/DB → Codable → UI
iOS Dock API → Bearer mobile-entry bootstrap → isolated cookie session → /drydock
Mac runners → API-key /api/ext/* → queue/claim/result state → external systems
```

The native app is a client, not a second business database. `AuthStore` owns
identity and Keychain-backed session lifecycle. `APIClient` rejects responses
from an old account generation. `DockAPIClient` separately owns Dock cookie
bootstrap and its single-flight/epoch checks.

`OfflineStore` is account-scoped. Allowlisted GET caches are fallbacks only for
transport failures, not HTTP/decoding errors. Opt-in writes keep their original
idempotency key in `Outbox`; financial/approval writes are not automatically
replayed. Logout purges readable caches while preserving the same user's unsent
outbox for their next login. `AttachmentCache` uses invalidation tickets to
reject late cache writes after logout/deletion.

`TRMTWidget` is a separate extension target with a deliberately small shared
source set and Keychain access group. Main-app foreground/mutations invalidate
relevant widget sources; iOS owns timeline execution. Swift models, JavaScript
and external workers share backend contracts, so payload changes require
cross-surface fixtures even when only one screen is being changed.

## Authentication and side effects

Cookie-based browser writes use CSRF protection. Native API requests use Bearer
authentication; workers use API keys. Credential type, not a broad path exemption,
determines the CSRF policy. Live account/role checks remain owned by backend auth.

`import app` must remain safe for discovery. Runtime directory/key initialization
is explicit through `init_runtime()`; an uninitialized entry point fails closed.
Tests must isolate database and file paths and must not send mail, push
notifications, call SVMS or mutate production data.

Gemini, Outlook, SVMS, APNs and runner callbacks are explicit integration edges.
Money and external-dispatch paths retain their auth, confirmation, claim/result
and reconciliation contracts during structural work.

## Testing discipline

- `tests/fixtures/url_map_snapshot.json`: rule, methods, endpoint, strict slashes
  and defaults. Changes require inspection of the exact additions/removals.
- `test_productization_gates.py`: authenticated HTML GET smoke and error pages;
  parameterized pages need explicit safe fixtures.
- `test_boundary_dependency_graph.py`: import graph and layering.
- `test_runtime_hardening.py`: side-effect-free import, key persistence and
  request/runtime guards.
- `test_db_query_contract.py`: row shape/order, bounded single-row materialization
  and cursor release on errors.
- `test_money_path_guard_contract.py`: static decorator order plus anonymous/
  non-admin rejection. URL snapshots alone do not catch a removed auth guard.
- `test_money_bulk_contract.py`: final database state for bulk approval and purge.
- Feature tests cover projections, migrations, idempotency and browser pure logic.
- iOS `TRMTTests` contains model/normalization and URLProtocol-backed network
  contracts. `project.yml` is the source for XcodeGen; workspace CI generates the
  project and runs `xcodebuild test`. Build-only success is not executed tests.

Some historical Python tests initialize fixtures at import and modify module
globals; prefer isolated per-file processes when combining unrelated suites.
Never print fixture-generated passwords or include runtime logs in review packets.

## Deployment and rollback

`deploy/autodeploy.sh`, invoked by `deploy/trmt-autodeploy.timer`, installs a
committed archive (the server is not a Git checkout). `deploy/install.sh`
bootstraps service/timers. `deploy/rollback.sh` excludes deployer files from the
application rollback payload. `deploy/backup.sh` and `deploy/restore-check.sh`
own backup/restore verification.

The service file configures one Gunicorn worker with eight gthread threads.
Request-bound document conversion and network integrations consume this shared
thread budget. A larger worker count is not a substitute for measuring memory,
SQLite writer contention and external-task semantics.

Delivery is complete only after observing live web SHA/health and, for iOS,
the OTA manifest/build and artifact hash. A local build, commit or push alone
does not prove live application.

Money-path structural changes require their dedicated safety review and guard,
state-transition and final-state tests. URL snapshots alone do not prove that
authorization or side-effect controls survived a refactor.
