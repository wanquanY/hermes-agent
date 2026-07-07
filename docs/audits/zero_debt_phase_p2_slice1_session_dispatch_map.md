# P2 Slice 1 Session Dispatch Map

Status: `preflight_only`

P1 human sign-off is still pending. This map documents the production entry
points for P2 Slice 1 but does not start the migration.

## Target Repo-Backed Path

The v3 gateway stack already has repo-backed session handlers:

| Component | Evidence | Role |
|---|---|---|
| `hermes_agent/gateway/pipeline.py` | `dispatch(registry, frame, resolver=...)` | Normalizes params, folds inbound session-id aliases, dispatches to registry, folds response aliases. |
| `hermes_agent/gateway/registry.py` | `MethodRegistry.register/get/validate` | Single method table for v3 handlers. |
| `hermes_agent/gateway/methods/session_methods.py` | `register(registry, repo)` | Registers `session.create/get/list/close/branch/update_index`. |
| `hermes_agent/repositories/session_repo.py` | `SessionRepoImpl` | Owns `sessions` and `session_index`. |
| `hermes_agent/transport/stdio_daemon.py` | `session_methods.register(registry, session_repo)` | Wires repo-backed session methods in the v3 stdio daemon path. |

Target Slice 1 chain:

```text
frame -> hermes_agent.gateway.pipeline.dispatch
      -> MethodRegistry["session.create/get/list"]
      -> hermes_agent.gateway.methods.session_methods
      -> SessionRepoImpl
      -> SQLite sessions/session_index
      -> canonical wire response with only session_id
```

## Current DoXie/TUI Production Path

The active DoXie sidecar method exposure still points at legacy method modules:

| Component | Evidence | Current behavior |
|---|---|---|
| `dovie_extension/manifest.py` | `METHOD_MODULES["session.create"] = "tui_gateway.methods.session"` and `METHOD_MODULES["session.list"] = "tui_gateway.methods.session"` | Exposes legacy TUI method module for DoXie. |
| `dovie_extension/gateway_methods.py` | allowlist contains `session.create`, `session.list`, `workspace.session.list` | Allows DoXie to call legacy session methods. |
| `tui_gateway/methods/session.py` | `@method("session.create")` | Creates control-plane sessions via `_db_for_session_request` and direct DB calls. |
| `tui_gateway/methods/session.py` | `@method("session.list")` | Lists sessions via `db.list_sessions_rich(...)` and live-session overlays. |
| `tui_gateway/ws.py` / `tui_gateway/server.py` | read/control executor allowlists include `session.list`; comments reference `session.create` route | Keeps the legacy sidecar control path active. |

Legacy Slice 1 chain:

```text
DoXie request
  -> dovie_extension METHOD_MODULES
  -> tui_gateway.methods.session @method("session.create/list")
  -> request-scoped SessionDB / db shim
  -> sessions/session_index + live in-memory _sessions overlay
  -> legacy response carrying stored_session_id/session_key semantics
```

## Critical Migration Finding

P2 Slice 1 cannot be satisfied by only testing `hermes_agent/transport/stdio_daemon.py`.
That path is already repo-backed, but it is not the DoXie/TUI production
sidecar path. The slice must migrate or replace the `dovie_extension` /
`tui_gateway.methods.session` production route for `session.create/get/list`.

## Required Production Wiring Decision

The implementation should choose one production owner for `session.create/get/list`:

1. Preferred final direction: expose the v3 `MethodRegistry`/`pipeline` from the
   DoXie sidecar path and register `session_methods` against `SessionRepoImpl`.
2. Temporary within-slice option: route only `session.create/get/list` through a
   dedicated bridge that constructs `SessionRepoImpl` from the request-scoped
   SQLite connection.

Either option must remove the old same-responsibility storage behavior before
the slice closes. A bridge is acceptable only inside the slice; it cannot remain
as a compatibility layer at phase close.

## Slice 1 Test Surface

Tests must cover the production path, not just the v3 stdio daemon:

- DoXie/TUI sidecar dispatch for `session.create`
- DoXie/TUI sidecar dispatch for `session.list`
- repo-backed `session.get` if exposed through the same path
- SQLite assertions on `sessions` and `session_index`
- response alias check: no `stored_session_id`, `stable_session_id`, or
  `runtime_session_id` in the returned wire payload

Existing target-path coverage already proves the repo-backed v3 handlers:

| Test | Coverage |
|---|---|
| `tests/gateway_v3/test_session_create_method.py` | `session.create`, `session.get`, `session.list`, branch/close basics through `pipeline.dispatch` and `SessionRepoImpl`. |
| `tests/gateway_v3/test_session_methods_more.py` | `session.list` filters, close, branch behavior through v3 registry. |
| `tests/gateway_v3/test_domain_methods_e2e.py` | Cross-domain v3 method registration with `SessionRepoImpl`, alias folding, and session reads. |
| `tests/gateway_v3/test_response_alias_fold.py` | Wire response alias retirement. |

Missing coverage is the DoXie/TUI sidecar production route:

```text
dovie_extension METHOD_MODULES -> tui_gateway.methods.session -> SessionRepoImpl
```

P2 Slice 1 must add or update tests on that route before deleting the old
`SessionDB` storage owner.

## Removal Targets

The slice should remove or behavior-empty these same-responsibility paths:

- direct `db.create_session(...)` ownership inside `tui_gateway/methods/session.py`
  for `session.create`
- direct `db.list_sessions_rich(...)` ownership inside `tui_gateway/methods/session.py`
  for `session.list`
- `METHOD_MODULES` registration for `session.create/list` once the v3 registry
  owns those methods in the sidecar path

If live-session overlays are still required for UI state, they must become a
separate projection/bridge after the repo-backed storage response, not the
storage owner.
