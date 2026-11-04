"""SQLite 存储层。

中台不引入任何外部依赖：一个 SQLite 文件就是全部状态，便于备份、审计与离线复现。
所有时间戳统一用 UTC ISO8601 字符串，避免跨机器时区漂移。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union

SCHEMA_VERSION = 1

PathLike = Union[str, Path]

_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ---------- 数据中台 ----------

CREATE TABLE IF NOT EXISTS datasets (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL UNIQUE,
    modality       TEXT NOT NULL DEFAULT 'unknown',
    -- synthetic: 合成/扰动数据，可自由分发；real: 真实受试者数据，只允许登记元数据
    classification TEXT NOT NULL CHECK (classification IN ('synthetic', 'real')),
    export_policy  TEXT NOT NULL CHECK (export_policy IN ('open', 'internal', 'restricted')),
    owner          TEXT,
    description    TEXT,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dataset_versions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_id        INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    version           TEXT NOT NULL,
    content_hash      TEXT NOT NULL,
    n_files           INTEGER NOT NULL DEFAULT 0,
    n_samples         INTEGER,
    acquisition_batch TEXT,
    -- 只存指针，不复制数据本体；真实数据的路径永远不进版本库
    storage_path      TEXT,
    notes             TEXT,
    created_at        TEXT NOT NULL,
    UNIQUE (dataset_id, version)
);

CREATE TABLE IF NOT EXISTS dataset_fields (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_version_id INTEGER NOT NULL REFERENCES dataset_versions(id) ON DELETE CASCADE,
    name               TEXT NOT NULL,
    dtype              TEXT,
    role               TEXT CHECK (role IN ('feature', 'label', 'id', 'time', 'meta')),
    unit               TEXT,
    UNIQUE (dataset_version_id, name)
);

CREATE TABLE IF NOT EXISTS dataset_splits (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_version_id INTEGER NOT NULL REFERENCES dataset_versions(id) ON DELETE CASCADE,
    name               TEXT NOT NULL,
    n_samples          INTEGER NOT NULL,
    seed               INTEGER,
    split_hash         TEXT,
    UNIQUE (dataset_version_id, name)
);

-- ---------- 评测中台 ----------

CREATE TABLE IF NOT EXISTS suites (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    name               TEXT NOT NULL UNIQUE,
    dataset_version_id INTEGER REFERENCES dataset_versions(id) ON DELETE SET NULL,
    task_type          TEXT,
    metric_spec        TEXT,
    created_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    suite_id    INTEGER NOT NULL REFERENCES suites(id) ON DELETE CASCADE,
    label       TEXT NOT NULL,
    arm         TEXT,
    model       TEXT,
    git_commit  TEXT,
    config      TEXT,
    source_file TEXT,
    n_cases     INTEGER NOT NULL DEFAULT 0,
    n_passed    INTEGER NOT NULL DEFAULT 0,
    started_at  TEXT,
    created_at  TEXT NOT NULL,
    UNIQUE (suite_id, label)
);

CREATE TABLE IF NOT EXISTS cases (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    case_id    TEXT NOT NULL,
    task_class TEXT,
    passed     INTEGER NOT NULL CHECK (passed IN (0, 1)),
    detail     TEXT,
    UNIQUE (run_id, case_id)
);

CREATE TABLE IF NOT EXISTS metrics (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id  INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    name    TEXT NOT NULL,
    value   REAL NOT NULL,
    n       INTEGER,
    ci_low  REAL,
    ci_high REAL,
    UNIQUE (run_id, name)
);

CREATE TABLE IF NOT EXISTS gates (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id  INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    name    TEXT NOT NULL,
    passed  INTEGER NOT NULL CHECK (passed IN (0, 1)),
    detail  TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (run_id, name)
);

-- 血缘：谁产出了谁。数据版本 → 评测运行 → 报告
CREATE TABLE IF NOT EXISTS lineage (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    src_kind  TEXT NOT NULL,
    src_id    INTEGER NOT NULL,
    dst_kind  TEXT NOT NULL,
    dst_id    INTEGER NOT NULL,
    relation  TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cases_run ON cases(run_id);
CREATE INDEX IF NOT EXISTS idx_runs_suite ON runs(suite_id);
CREATE INDEX IF NOT EXISTS idx_lineage_dst ON lineage(dst_kind, dst_id);
"""


def now_iso() -> str:
    """统一的 UTC 时间戳（秒级即可，报告中按本地时区展示由前端决定）。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(path: PathLike) -> sqlite3.Connection:
    """打开（不自动建表）连接。不存在的文件会被 SQLite 创建为空库。"""
    p = Path(path)
    if p.parent and not p.parent.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(path: PathLike) -> sqlite3.Connection:
    """建库建表（幂等）。已存在的库不会被清空。"""
    conn = connect(path)
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
    return conn


def get_meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None
