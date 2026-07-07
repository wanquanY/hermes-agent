"""Migration loading and execution for Hermes Agent SQLite storage."""

from __future__ import annotations

import importlib.util
import logging
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Protocol


_logger = logging.getLogger(__name__)


def _log_frozen_skip(version: int, exc: BaseException) -> None:
    _logger.warning(
        "migration %d frozen (%s); skipping until unpark", version, exc
    )

_MIGRATION_PREFIX_WIDTH = 4


class MigrationLoadError(RuntimeError):
    """Raised when migration discovery finds an invalid migration module."""


class Migration(Protocol):
    """Executable storage migration."""

    version: int
    description: str

    def apply(self, cursor: sqlite3.Cursor) -> None:
        """Apply the migration with the supplied cursor."""


@dataclass(frozen=True)
class MigrationContext:
    """Context available to migration factories."""

    owner: Any | None = None


@dataclass(frozen=True)
class MigrationRecord:
    """Loaded migration metadata."""

    version: int
    description: str
    path: Path
    migration: Migration


@dataclass(frozen=True)
class _FunctionMigration:
    version: int
    description: str
    _apply: Callable[[sqlite3.Cursor], None]

    def apply(self, cursor: sqlite3.Cursor) -> None:
        self._apply(cursor)


def require_owner(context: MigrationContext, migration_name: str) -> Any:
    """Return the runner owner or raise a migration-specific error."""

    if context.owner is None:
        raise MigrationLoadError(f"{migration_name} requires a migration owner")
    return context.owner


def _default_migrations_dir() -> Path:
    return Path(__file__).resolve().parent


def _filename_version(path: Path) -> int:
    prefix = path.name.split("_", 1)[0]
    if len(prefix) != _MIGRATION_PREFIX_WIDTH or not prefix.isdigit():
        raise MigrationLoadError(
            f"migration filename must start with NNNN_: {path.name}"
        )
    return int(prefix)


def _migration_files(directory: Path) -> list[Path]:
    if not directory.exists():
        raise MigrationLoadError(f"migration directory does not exist: {directory}")
    files = sorted(
        path
        for path in directory.iterdir()
        if path.is_file()
        and path.suffix == ".py"
        and path.name != "__init__.py"
        and path.name != "base.py"
    )
    if not files:
        raise MigrationLoadError(f"no migration files found in {directory}")
    return files


def _load_module(path: Path) -> ModuleType:
    module_name = (
        f"hermes_agent.storage.migrations._loaded_"
        f"{path.stem}_{abs(hash(path.resolve()))}"
    )
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise MigrationLoadError(f"could not load migration module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


def _declared_version(module: ModuleType, path: Path) -> int:
    declared = getattr(module, "version", None)
    if not isinstance(declared, int):
        raise MigrationLoadError(f"{path.name} must declare integer version")
    filename_version = _filename_version(path)
    if declared != filename_version:
        raise MigrationLoadError(
            f"{path.name} declares version {declared}, expected {filename_version}"
        )
    return declared


def _description(module: ModuleType, path: Path) -> str:
    value = getattr(module, "description", "")
    if not isinstance(value, str) or not value.strip():
        raise MigrationLoadError(f"{path.name} must declare description")
    return value.strip()


def _coerce_migration(
    module: ModuleType,
    path: Path,
    context: MigrationContext,
) -> Migration:
    factory = getattr(module, "create_migration", None)
    if callable(factory):
        migration = factory(context)
    else:
        migration = getattr(module, "migration", None)
        if migration is None:
            apply_fn = getattr(module, "apply", None)
            if callable(apply_fn):
                migration = _FunctionMigration(
                    version=getattr(module, "version"),
                    description=getattr(module, "description"),
                    _apply=apply_fn,
                )

    if migration is None:
        raise MigrationLoadError(
            f"{path.name} must expose create_migration(), migration, or apply()"
        )
    if getattr(migration, "version", None) != getattr(module, "version"):
        raise MigrationLoadError(f"{path.name} migration.version mismatch")
    if getattr(migration, "description", None) != getattr(module, "description"):
        raise MigrationLoadError(f"{path.name} migration.description mismatch")
    if not callable(getattr(migration, "apply", None)):
        raise MigrationLoadError(f"{path.name} migration must define apply(cursor)")
    return migration


def load_migrations(
    directory: Path | str | None = None,
    *,
    context: MigrationContext | None = None,
) -> list[MigrationRecord]:
    """Load migration modules from a directory sorted by filename prefix."""

    migrations_dir = Path(directory) if directory is not None else _default_migrations_dir()
    migration_context = context or MigrationContext()
    records: list[MigrationRecord] = []
    seen_versions: dict[int, Path] = {}

    for path in _migration_files(migrations_dir):
        version = _filename_version(path)
        if version in seen_versions:
            raise MigrationLoadError(
                f"duplicate migration version {version}: "
                f"{seen_versions[version].name}, {path.name}"
            )
        seen_versions[version] = path

        module = _load_module(path)
        declared = _declared_version(module, path)
        description = _description(module, path)
        migration = _coerce_migration(module, path, migration_context)
        records.append(
            MigrationRecord(
                version=declared,
                description=description,
                path=path,
                migration=migration,
            )
        )

    return records


class MigrationRunner:
    """Run all storage migrations newer than the stored schema version."""

    def __init__(
        self,
        cursor: sqlite3.Cursor,
        owner: Any | None = None,
        *,
        migrations_dir: Path | str | None = None,
    ) -> None:
        self._cursor = cursor
        self._owner = owner
        self._migrations_dir = (
            Path(migrations_dir) if migrations_dir is not None else _default_migrations_dir()
        )

    def run_all(self) -> None:
        """Apply pending migrations in ascending order and bump schema_version.

        Migrations that raise an exception whose class name is
        ``FrozenMigrationError`` are treated as intentionally parked (spec
        §12 Phase M pattern) — skipped, logged, and excluded from the
        target ``schema_version`` bump. Real errors continue to abort.
        """

        context = MigrationContext(owner=self._owner)
        migrations = load_migrations(self._migrations_dir, context=context)
        current_version = self._read_schema_version()
        applied_versions: list[int] = []

        for record in migrations:
            if record.version == 1:
                if current_version is None:
                    try:
                        record.migration.apply(self._cursor)
                        applied_versions.append(record.version)
                    except Exception as exc:
                        if type(exc).__name__ == "FrozenMigrationError":
                            _log_frozen_skip(record.version, exc)
                            continue
                        raise
                continue
            if current_version is None or record.version > current_version:
                try:
                    record.migration.apply(self._cursor)
                    applied_versions.append(record.version)
                except Exception as exc:
                    if type(exc).__name__ == "FrozenMigrationError":
                        _log_frozen_skip(record.version, exc)
                        continue
                    raise

        if applied_versions:
            bump_to = max(applied_versions)
            if current_version is None or bump_to > current_version:
                self._write_schema_version(bump_to)

    def _read_schema_version(self) -> int | None:
        try:
            row = self._cursor.execute(
                "SELECT version FROM schema_version LIMIT 1"
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return None
            raise
        if row is None:
            return None
        value = row["version"] if isinstance(row, sqlite3.Row) else row[0]
        return int(value)

    def _write_schema_version(self, version: int) -> None:
        row = self._cursor.execute(
            "SELECT version FROM schema_version LIMIT 1"
        ).fetchone()
        if row is None:
            self._cursor.execute(
                "INSERT INTO schema_version (version) VALUES (?)",
                (version,),
            )
        else:
            self._cursor.execute(
                "UPDATE schema_version SET version = ?",
                (version,),
            )

