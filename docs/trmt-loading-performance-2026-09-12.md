# TRMT modal/save loading performance — 2026-09-12

## Scope and evidence

Canonical web and iOS sources reviewed for modal preparation, save follow-up requests,
and dependent loading waterfalls. Existing rendering, server-confirmed saves, scopes,
error handling, permissions, and report full-detail contracts remain authoritative.

### Changes

1. **Daily web refresh:** supervisor counts still refresh first so the active scope is
   resolved exactly as before; vessel and issue reads now run concurrently, followed by
   one render. Three dependent network waves become two; request count is unchanged.
2. **Daily web bootstrap:** supervisor, summary counts, and vessel order start together;
   scoped vessels and issues start together after scope resolution. Five dependent
   waves become two, with the same requests and failure policies.
3. **Daily modal roster:** reuse the freshly loaded scoped vessel options, normalizing
   numeric/string supervisor keys. A warmed modal no longer repeats a vessel GET.
   Explicit reload invalidates old roster caches before refreshing them. Issue detail
   still gets a fresh server read before full editing, preserving concurrent-edit safety.
4. **Dock/Boarding report metadata modals:** their existing GET now requests
   `metadata_only=1`. The server omits section/block reads and decoding. Default full
   detail, 404, login gate, and `can_edit` are unchanged. iOS report metadata sheets
   already use their loaded metadata and do not issue this expensive detail GET, so
   no additional iOS metadata request was introduced.
5. **iOS Daily action append/edit:** return actions from the successful POST/PATCH
   response immediately. Only older responses without actions use the existing
   best-effort detail fallback. Normal save path changes from two requests to one;
   CAS conflict and offline handling are unchanged. A test-only APIClient initializer
   allows transport-level request-count tests without altering the shared default.

## Validation

- Node `tests/loading_performance_runtime.cjs`: passed. Executes shipped functions
  with controlled promises; verifies concurrent dependent-stage dispatch, a single
  render after both reads, warm modal GET elimination, and supervisor cache separation.
- Python targeted report projections/API/common-modal regressions: **14/14 passed**
  using `.venv-test/bin/python`. Includes full/metadata equality except sections,
  admin/viewer permission parity, missing reports, anonymous access, and one-query
  metadata projection.
- Changed JavaScript syntax and whitespace checks passed.
- All Might's cache-alias concern was resolved by returning a shallow copy to modal consumers. Summary counts and vessel-order are authenticated current-user reads with no supervisor parameter; they are scope-independent. Both action write endpoints return the complete committed actions array, so an empty array is not a legacy placeholder. Metadata responses are consumed only by the two modal-local variables and never enter a shared report store.
- iOS app, widget, and complete XCTest target **build-for-testing exit 0**.
- Three new URLProtocol XCTest cases cover committed response request counts,
  legacy fallback, and failed CAS writes. **Compiled, not executed:** the bounded
  test-without-building attempt returned exit 70 because Xcode did not expose the
  installed simulator as a supported destination.

### Synthetic payload benchmark (not live latency)

200 paragraph blocks with 8192 ASCII bytes each, mocked SQLite result rows, actual
projection and JSON serialization, mean of 20 local iterations:

| Metric | Full report | Metadata only |
| --- | ---: | ---: |
| Projection SQL reads | 3 | 1 |
| JSON bytes | 1,653,720 | 67 |
| Local projection + serialization | 3.166 ms | 0.002 ms |

Fixture size is deliberately synthetic. These numbers are not production payloads,
network timings, or a claim about the user's observed end-to-end latency.

## Boundaries / main handoff

No commit, push, deployment, All Might review, live writes, or production data edits
were performed by this worker. Main owns the single review and release gates.
Full issue/list refreshes after mutations remain in place where responses contain
only IDs; removing them would risk stale ordering/filter counts or replacing rows
with incomplete data. iPhone physical-device latency and actual XCTest execution
remain unverified. Network RTT, backend write latency, and runtime service contention
must be measured separately before attributing all perceived lag to these paths.
