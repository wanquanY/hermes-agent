"""Shared primitives for L1 Repository layer (spec §4).

Repositories are the sole gateway into their owned tables. They receive a
typed ``RepositoryConnection`` via dependency injection — no shared
``_conn``/``_lock`` duck typing across mixins.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


# Direct type alias for now — Phase F introduces a richer connection wrapper
# (busy timeout enforcement, sharded lock coordination, telemetry hooks).
RepositoryConnection = sqlite3.Connection


@dataclass(frozen=True)
class RepositoryContext:
    """Injected context for repository instantiation.

    ``owner`` bridges to the legacy state facade during D2-D5
    migration and will be dropped in D5 (spec §12).
    """

    conn: RepositoryConnection
    owner: object | None = None
