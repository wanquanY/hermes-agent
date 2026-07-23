"""SQLite schema migrations for Hermes Agent storage."""

from .base import Migration
from .base import MigrationLoadError
from .base import MigrationRecord
from .base import MigrationRunner
from .base import load_migrations

CURRENT_SCHEMA_VERSION = 59

__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "Migration",
    "MigrationLoadError",
    "MigrationRecord",
    "MigrationRunner",
    "load_migrations",
]
