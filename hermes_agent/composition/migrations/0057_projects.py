"""Create first-class projects inside the canonical state store."""

from __future__ import annotations

import sqlite3


version = 57
description = "persist first-class projects, folders, and discovered repositories"


def apply(cursor: sqlite3.Cursor) -> None:
    cursor.executescript(
        """
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            slug TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            description TEXT,
            icon TEXT,
            color TEXT,
            board_slug TEXT,
            primary_path TEXT,
            created_at REAL NOT NULL,
            archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1))
        );

        CREATE TABLE IF NOT EXISTS project_folders (
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            path TEXT NOT NULL,
            label TEXT,
            is_primary INTEGER NOT NULL DEFAULT 0 CHECK (is_primary IN (0, 1)),
            added_at REAL NOT NULL,
            PRIMARY KEY (project_id, path)
        );

        CREATE INDEX IF NOT EXISTS idx_project_folders_path
            ON project_folders(path);

        CREATE TABLE IF NOT EXISTS project_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS discovered_repos (
            root TEXT PRIMARY KEY,
            label TEXT,
            last_seen REAL NOT NULL
        );
        """
    )


__all__ = ["apply", "description", "version"]
