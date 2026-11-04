"""审计日志：被拒也要留痕、只追加、门禁结论可追溯。"""

from __future__ import annotations

import sqlite3

import pytest

from helpers import make_native
from maldi_platform import audit
from maldi_platform.cli import main


def run(argv, capsys):
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out + captured.err


# --------------------------------------------------------------------------- #
# 记录与查询
# --------------------------------------------------------------------------- #
def test_record_and_list_newest_first(conn):
    audit.record(conn, "dataset.add", target="a@v1")
    audit.record(conn, "dataset.export", target="version:1", outcome="denied", detail="payload")

    rows = audit.list_entries(conn)
    assert [r["action"] for r in rows] == ["dataset.export", "dataset.add"]
    assert rows[0]["outcome"] == "denied"
    assert rows[0]["target"] == "version:1"
    assert audit.count(conn) == 2


def test_list_filters(conn):
    audit.record(conn, "dataset.add", target="a")
    audit.record(conn, "dataset.export", target="b", outcome="denied")
    audit.record(conn, "dataset.export", target="c", outcome="ok")

    assert len(audit.list_entries(conn, action="dataset.export")) == 2
    assert len(audit.list_entries(conn, outcome="denied")) == 1
    assert len(audit.list_entries(conn, limit=1)) == 1


def test_unknown_outcome_is_refused(conn):
    with pytest.raises(audit.AuditError):
        audit.record(conn, "dataset.add", outcome="maybe")


def test_audit_is_append_only_at_db_level(conn):
    audit.record(conn, "dataset.add", target="a")
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE audit_log SET actor = 'someone-else'")
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM audit_log")
    conn.rollback()
    assert audit.count(conn) == 1


def test_actor_priority(monkeypatch):
    assert audit.resolve_actor("explicit") == "explicit"
    monkeypatch.setenv(audit.ACTOR_ENV, "ci-bot")
    assert audit.resolve_actor() == "ci-bot"
    monkeypatch.delenv(audit.ACTOR_ENV)
    assert audit.resolve_actor()  # 退回到系统用户，至少不为空


# --------------------------------------------------------------------------- #
# 业务入口真的写了审计
# --------------------------------------------------------------------------- #
def test_denied_export_is_audited(tmp_path, data_dir, capsys):
    db = str(tmp_path / "audit.db")
    run(["init", "--db", db], capsys)
    code, _ = run(
        [
            "dataset",
            "add",
            "--db",
            db,
            "--name",
            "real-uti",
            "--classification",
            "real",
            "--policy",
            "restricted",
            "--source",
            str(data_dir),
        ],
        capsys,
    )
    assert code == 0

    code, out = run(
        ["dataset", "export-check", "--db", db, "--version-id", "1", "--target", "payload"], capsys
    )
    assert code == 1
    assert "拒绝" in out

    code, out = run(["audit", "list", "--db", db], capsys)
    assert code == 0
    assert "dataset.export" in out
    assert "denied" in out


def test_allowed_export_is_audited(tmp_path, data_dir, capsys):
    db = str(tmp_path / "audit2.db")
    run(
        [
            "dataset",
            "add",
            "--db",
            db,
            "--name",
            "synth",
            "--classification",
            "synthetic",
            "--policy",
            "open",
            "--source",
            str(data_dir),
        ],
        capsys,
    )
    code, _ = run(
        ["dataset", "export-check", "--db", db, "--version-id", "1", "--target", "metadata"], capsys
    )
    assert code == 0
    _, out = run(["audit", "list", "--db", db, "--json"], capsys)
    assert '"outcome": "ok"' in out


def test_gate_failure_is_audited(tmp_path, ids16, native_file, capsys):
    db = str(tmp_path / "gate.db")
    run(["suite", "add", "--db", db, "--name", "s1"], capsys)
    payload = make_native("weak", ids16[:4], ids16)
    path = native_file(payload, "weak.json")
    run(["run", "ingest", "--db", db, "--suite", "1", "--file", str(path)], capsys)

    code, out = run(["gate", "--db", db, "--run", "1", "--min-pass", "0.9"], capsys)
    assert code == 1
    assert "未通过" in out

    _, listed = run(["audit", "list", "--db", db, "--action", "gate.evaluate"], capsys)
    assert "failed" in listed


def test_ingest_is_audited(tmp_path, ids16, native_file, capsys):
    db = str(tmp_path / "ingest.db")
    run(["suite", "add", "--db", db, "--name", "s1"], capsys)
    path = native_file(make_native("a", ids16[:5], ids16), "a.json")
    run(["run", "ingest", "--db", db, "--suite", "1", "--file", str(path)], capsys)

    _, listed = run(["audit", "list", "--db", db, "--action", "run.ingest"], capsys)
    assert "run.ingest" in listed
    assert "ok" in listed


def test_actor_is_recorded_from_cli(tmp_path, data_dir, capsys):
    db = str(tmp_path / "actor.db")
    run(
        [
            "--actor",
            "wang",
            "dataset",
            "add",
            "--db",
            db,
            "--name",
            "synth",
            "--source",
            str(data_dir),
        ],
        capsys,
    )
    _, out = run(["audit", "list", "--db", db, "--json"], capsys)
    assert '"actor": "wang"' in out
