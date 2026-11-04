"""schema 版本与迁移：只往前升、拒绝降级、迁移幂等。"""

from __future__ import annotations

import sqlite3

import pytest

from maldi_platform.cli import main
from maldi_platform.db import (
    SCHEMA_VERSION,
    SchemaError,
    check_schema,
    connect,
    current_version,
    init_db,
)


def run(argv, capsys):
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def _downgrade_to_v1(path) -> None:
    """把新库退化成 v1 的样子：没有 audit_log、版本号写 1。"""
    conn = init_db(path)
    conn.executescript(
        "DROP TRIGGER IF EXISTS audit_log_no_update;"
        "DROP TRIGGER IF EXISTS audit_log_no_delete;"
        "DROP TABLE IF EXISTS audit_log;"
    )
    conn.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()


def _has_table(path, name: str) -> bool:
    conn = connect(path)
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone()
    conn.close()
    return row is not None


def test_fresh_db_is_at_current_version(conn):
    assert SCHEMA_VERSION == 2
    assert current_version(conn) == SCHEMA_VERSION
    info = check_schema(conn)
    assert info["status"] == "ok"
    assert info["pending_migrations"] == []


def test_fresh_db_has_audit_table(tmp_path):
    db = tmp_path / "fresh.db"
    init_db(db).close()
    assert _has_table(db, "audit_log") is True


def test_v1_db_is_migrated_on_init(tmp_path):
    db = tmp_path / "old.db"
    _downgrade_to_v1(db)
    assert _has_table(db, "audit_log") is False

    conn = init_db(db)
    assert current_version(conn) == SCHEMA_VERSION
    assert _has_table(db, "audit_log") is True
    conn.close()


def test_outdated_db_is_reported_before_migration(tmp_path):
    db = tmp_path / "old.db"
    _downgrade_to_v1(db)
    conn = connect(db)
    info = check_schema(conn)
    assert info["status"] == "outdated"
    assert info["db_version"] == 1
    assert info["pending_migrations"] == [2]
    conn.close()


def test_init_is_idempotent(tmp_path):
    db = tmp_path / "twice.db"
    init_db(db).close()
    conn = init_db(db)
    assert current_version(conn) == SCHEMA_VERSION
    # 重复 init 不该把审计记录清掉
    conn.execute(
        "INSERT INTO audit_log(ts, actor, action, outcome) VALUES('2026-01-01T00:00:00+00:00', 't', 'x', 'ok')"
    )
    conn.commit()
    conn.close()
    conn = init_db(db)
    assert conn.execute("SELECT COUNT(*) AS n FROM audit_log").fetchone()["n"] == 1
    conn.close()


def test_future_db_is_refused(tmp_path):
    """库比程序新时必须报错——用旧程序写新库是最隐蔽的数据损坏。"""
    db = tmp_path / "future.db"
    conn = init_db(db)
    conn.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()

    with pytest.raises(SchemaError) as exc:
        init_db(db)
    assert "99" in str(exc.value)


def test_non_integer_version_is_refused(tmp_path):
    db = tmp_path / "bad.db"
    conn = init_db(db)
    conn.execute("UPDATE meta SET value = 'v2' WHERE key = 'schema_version'")
    conn.commit()
    with pytest.raises(SchemaError):
        current_version(conn)
    conn.close()


def test_empty_file_is_reported_as_empty(tmp_path):
    """文件存在但还没有表，不能报"版本不一致"，要报"还没初始化"。"""
    db = tmp_path / "empty.db"
    db.write_bytes(b"")
    conn = sqlite3.connect(str(db))
    info = check_schema(conn)
    assert info["status"] == "empty"
    assert info["db_version"] == 0
    conn.close()


def test_cli_db_check_ok(tmp_path, capsys):
    db = str(tmp_path / "cli.db")
    init_db(db).close()
    code, out = run(["db", "check", "--db", db], capsys)
    assert code == 0
    assert "一致" in out


def test_cli_db_check_missing(tmp_path, capsys):
    code, out = run(["db", "check", "--db", str(tmp_path / "nope.db")], capsys)
    assert code == 1
    assert "不存在" in out


def test_cli_db_check_future(tmp_path, capsys):
    db = str(tmp_path / "future.db")
    conn = init_db(db)
    conn.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    code, out = run(["db", "check", "--db", db], capsys)
    assert code == 1
    assert "升级程序" in out
