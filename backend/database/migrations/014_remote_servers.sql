-- Migration 014: Remote write servers
-- Stores SSH-based remote exiftool server configurations.
-- One row per remote server.  For most users this will be 0 or 1 rows.

CREATE TABLE IF NOT EXISTS remote_servers (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    label                TEXT    NOT NULL DEFAULT 'Remote Server',
    host                 TEXT    NOT NULL,
    port                 INTEGER NOT NULL DEFAULT 22,
    user                 TEXT    NOT NULL,
    ssh_key_path         TEXT    NOT NULL,
    local_path_prefix    TEXT    NOT NULL,
    remote_path_prefix   TEXT    NOT NULL,
    writeback_concurrency INTEGER NOT NULL DEFAULT 4,
    enabled              INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at           TEXT    NOT NULL DEFAULT (datetime('now'))
);
