# Hermes Storage Migrations

This directory is the executable migration chain for `hermes_state.SessionDB`.
`SCHEMA_SQL` and `_reconcile_columns()` remain the declarative baseline; files
here hold data migrations and version-specific rebuilds that cannot be handled
by column reconciliation alone.

## Naming

- Python migrations use `NNNN_slug.py`.
- `NNNN` is the integer `version` declared by the module, zero-padded to four
  digits.
- The loader rejects duplicate versions and any file whose declared `version`
  does not match the filename prefix.
- Slugs should come from the original migration comment or the migrated method
  name, for example `0010_fts_trigram_backfill.py`.

## Baseline

`0001_declarative_baseline.py` is special. It runs only when no
`schema_version` row exists, and it applies `SCHEMA_SQL` plus
`_reconcile_columns()` through the runner owner. Existing databases skip it.

## Declaration Forms

A migration module may expose one of three forms:

- `create_migration(context)`: use this when the migration needs the runner
  owner, e.g. a `SessionDB` helper method. Use `require_owner(context, name)`.
- `migration`: an object with `version`, `description`, and `apply(cursor)`.
- `apply(cursor)`: a module-level function. The loader wraps it in a migration
  object using the module `version` and `description`.

## Checklist

- Pick the next reserved version number and name the file `NNNN_slug.py`.
- Declare integer `version` and non-empty `description`.
- Preserve original SQL, try/except fallback behavior, and backfill semantics.
- Keep schema-level startup routines in their owner modules unless they are
  guarded by a migration version.
- Add or update a focused smoke test for the migrated behavior.
- Do not edit `_reconcile_columns()` table definitions in this directory pass.
