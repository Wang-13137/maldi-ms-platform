"""SQLite 存储层。

中台不引入任何外部依赖：一个 SQLite 文件就是全部状态，便于备份、审计与离线复现。
所有时间戳统一用 UTC ISO8601 字符串，避免跨机器时区漂移。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2

PathLike = str | Path


class SchemaError(RuntimeError):
    """库的 schema 版本与本程序不兼容（通常是用旧程序打开了新库）。"""


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

# 迁移脚本：键是"升到哪一版"，值是从上一版升到该版要执行的 SQL。
# 只增不改——已发布的版本号对应的迁移脚本一旦进入使用者手里就不能再动，
# 否则同一个库在不同机器上会演化成两种结构。
_MIGRATIONS: dict[int, str] = {
    2: """
-- v2：审计日志。谁在什么时候把哪份数据导出去了，必须能事后查。
CREATE TABLE IF NOT EXISTS audit_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    actor   TEXT NOT NULL,
    action  TEXT NOT NULL,
    target  TEXT,
    outcome TEXT NOT NULL CHECK (outcome IN ('ok', 'denied', 'failed')),
    detail  TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);

-- 用触发器把"只追加"钉在库层面，而不是靠调用方自觉。
CREATE TRIGGER IF NOT EXISTS audit_log_no_update
BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log 只允许追加，不允许 UPDATE');
END;

CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log 只允许追加，不允许 DELETE');
END;
""",
}


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
    """建库 + 按需迁移到 ``SCHEMA_VERSION``（幂等，已存在的库不会被清空）。

    只做"往前升"：库比程序新时直接报错，不做静默降级——用旧程序写新库是最容易
    产生"看不见的数据损坏"的场景，宁可让人先升级程序。
    """
    conn = connect(path)
    conn.executescript(_SCHEMA)  # v1 基线，全部 CREATE IF NOT EXISTS

    current = current_version(conn)
    if current > SCHEMA_VERSION:
        raise SchemaError(
            f"库的 schema 是 v{current}，本程序只支持到 v{SCHEMA_VERSION}：请升级 maldi-platform 后再操作。"
        )
    for target in range(current + 1, SCHEMA_VERSION + 1):
        conn.executescript(_MIGRATIONS[target])

    _set_meta(conn, "schema_version", str(SCHEMA_VERSION))
    conn.commit()
    return conn


def current_version(conn: sqlite3.Connection) -> int:
    """读库里的 schema 版本。老库没有 meta 记录时按 v1 处理（v1 时代还没有版本字段）。"""
    raw = get_meta(conn, "schema_version")
    if raw is None:
        return 1
    try:
        return int(raw)
    except ValueError as exc:
        raise SchemaError(f"meta.schema_version 不是整数：{raw!r}") from exc


def check_schema(conn: sqlite3.Connection) -> dict[str, Any]:
    """给 ``maldi-platform db check`` 用的体检结果，可直接作为 CI 步骤的输出。"""
    try:
        db_version = current_version(conn)
    except sqlite3.OperationalError:
        # 文件在但表还没建：不是"版本不对"，是"还没初始化"
        return {
            "status": "empty",
            "db_version": 0,
            "code_version": SCHEMA_VERSION,
            "pending_migrations": list(range(1, SCHEMA_VERSION + 1)),
        }
    if db_version > SCHEMA_VERSION:
        status = "future"
    elif db_version < SCHEMA_VERSION:
        status = "outdated"
    else:
        status = "ok"
    return {
        "status": status,
        "db_version": db_version,
        "code_version": SCHEMA_VERSION,
        "pending_migrations": list(range(db_version + 1, SCHEMA_VERSION + 1)),
    }


def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None
