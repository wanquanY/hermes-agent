# Hermes Workspace and Artifact Architecture

## Goals

Hermes embedded in Dovie must match the OpenClaw client contract without making the Hermes source directory the implicit user workspace.

The gateway owns two first-class concepts:

- **Workspace**: the user-visible project or folder that groups sessions, history, tool execution, and generated files.
- **Artifact**: a durable file produced or modified by an agent turn and safe for the client to surface in the "成果" view.

This design keeps those concepts explicit instead of deriving UI state from transient tool events only.

## Principles

- Session `cwd` is the execution directory for tools.
- Workspace `path` is the allowed artifact root and the grouping key for history.
- `cwd` must resolve inside the workspace root. If the client does not provide a workspace, the normalized `cwd` becomes the workspace root.
- Artifacts must be real files under the workspace root.
- Artifact creation is persisted before `artifact.created` is emitted.
- The client can recover workspace and artifact state after gateway restart.
- The Hermes source/runtime directory is never used as a user workspace unless the client explicitly passes it as `cwd`.

## Domain Model

### Workspace

| Field | Description |
| --- | --- |
| `id` | Stable workspace id. Client-provided id wins; otherwise it is derived from normalized local path. |
| `name` | Display name. Client-provided name wins; otherwise basename of path. |
| `path` | Normalized absolute workspace root. |
| `kind` | Client-defined workspace kind, usually `local`. |
| `created_at` | First seen timestamp. |
| `updated_at` | Last update timestamp. |

### Session Workspace Binding

| Field | Description |
| --- | --- |
| `session_id` | Stored Hermes session id, not the short gateway connection id. |
| `workspace_id` | Bound workspace id. |
| `cwd` | Normalized execution cwd for this session. |
| `created_at` | First bind timestamp. |
| `updated_at` | Last bind timestamp. |

### Artifact

| Field | Description |
| --- | --- |
| `id` | Stable id derived from workspace id and absolute path. |
| `workspace_id` | Workspace that owns the artifact. |
| `path` | Absolute file path. |
| `relative_path` | Path relative to workspace root. |
| `title` | Display title, defaulting to basename. |
| `mime_type` | Guessed MIME type. |
| `size_bytes` | File size when recorded. |
| `created_at` | First observed timestamp. |
| `updated_at` | Last observed timestamp. |
| `origin` | Structured source metadata, including tool id/name and event type. |

The `(workspace_id, path)` pair is unique. Repeated writes update the existing artifact and refresh the session-artifact binding instead of creating duplicates.

## Persistence

The gateway stores Dovie-facing workspace and artifact state in a dedicated SQLite database:

```text
$HERMES_HOME/tui-gateway/state.db
```

This keeps the Dovie gateway surface decoupled from the core `state.db` transcript schema and avoids growing `hermes_state.py`.

Tables:

- `gateway_workspaces`
- `gateway_session_workspaces`
- `gateway_artifacts`
- `gateway_session_artifacts`

The store is gateway-owned, uses WAL when available, and is safe to rebuild from future transcript/tool metadata if needed.

## Gateway Protocol

### Session Create / Resume / Branch

`session.create`, `session.resume`, and `session.branch` must:

1. Normalize `cwd`.
2. Resolve workspace from params or default to `cwd`.
3. Validate `cwd` is inside workspace root.
4. Persist the workspace.
5. Persist the session-workspace binding.
6. Return `info.cwd` and `info.workspace`.

### Tool Completion

Tool completion must:

1. Detect file mutation tools and extract target paths.
2. Resolve target paths against session `cwd`.
3. Reject files outside workspace root.
4. Reject missing/non-file targets.
5. Persist artifact metadata and the session-artifact binding.
6. Emit `artifact.created` with the persisted artifact payload.

### Query APIs

The client must be able to recover state without relying on transient event history:

- `workspace.current`
- `workspace.list`
- `artifacts.list`

These APIs return stored data and are safe to call after a gateway restart.

## Non-Goals

- This layer does not replace Hermes transcript history in `state.db`.
- This layer does not implement client-side rendering of the "成果" view.
- This layer does not sandbox tool execution. It defines the workspace root used for artifact eligibility and grouping.

## Validation

Required tests:

- Reject missing `cwd`.
- Reject `cwd` outside explicit workspace path.
- Default workspace to `cwd` when omitted.
- Persist session-workspace binding on create and resume.
- Persist artifacts before event emission.
- Deduplicate repeated writes to the same workspace path.
- Reject artifacts outside workspace root.
- List artifacts after the transient gateway session is removed.
