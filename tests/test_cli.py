"""CLI 端到端测试：一条命令能跑通，门禁失败要反映到退出码。"""
from __future__ import annotations

import json

from maldi_platform.cli import main

from helpers import make_agent_report, make_native


def run(argv, capsys):
    code = main(argv)
    out = capsys.readouterr()
    return code, out.out + out.err


def test_init_and_dataset_flow(tmp_path, data_dir, capsys):
    db = str(tmp_path / "cli.db")
    assert run(["init", "--db", db], capsys)[0] == 0

    code, out = run(
        [
            "dataset", "add", "--db", db,
            "--name", "synth-uti",
            "--source", str(data_dir),
            "--n-samples", "37",
            "--batch", "B1",
            "--modality", "maldi-spectrum",
        ],
        capsys,
    )
    assert code == 0
    assert "synth-uti" in out

    code, out = run(["dataset", "list", "--db", db], capsys)
    assert code == 0
    assert "synth-uti" in out
    assert "synthetic" in out


def test_dataset_show_json(tmp_path, data_dir, capsys):
    db = str(tmp_path / "cli.db")
    run(["dataset", "add", "--db", db, "--name", "d", "--source", str(data_dir)], capsys)
    code, out = run(["dataset", "show", "--db", db, "--version-id", "1", "--json"], capsys)
    assert code == 0
    payload = json.loads(out[out.index("{") :])
    assert payload["dataset"]["name"] == "d"
    assert len(payload["version"]["content_hash"]) == 64


def test_export_check_denies_real_payload(tmp_path, data_dir, capsys):
    db = str(tmp_path / "cli.db")
    run(
        ["dataset", "add", "--db", db, "--name", "real37", "--classification", "real",
         "--policy", "restricted", "--source", str(data_dir)],
        capsys,
    )
    assert run(["dataset", "export-check", "--db", db, "--version-id", "1", "--target", "metadata"], capsys)[0] == 0
    code, out = run(["dataset", "export-check", "--db", db, "--version-id", "1", "--target", "payload"], capsys)
    assert code == 1
    assert "拒绝" in out


def test_suite_and_run_and_compare(tmp_path, ids16, native_file, capsys):
    db = str(tmp_path / "cli.db")
    run(["suite", "add", "--db", db, "--name", "m9", "--task-type", "agent-perturbation"], capsys)

    a = native_file(make_native("A_full", ids16[:11], ids16), "a.json")
    c = native_file(make_native("C_no_tool", [], ids16), "c.json")
    assert run(["run", "ingest", "--db", db, "--suite", "1", "--file", str(a)], capsys)[0] == 0
    assert run(["run", "ingest", "--db", db, "--suite", "1", "--file", str(c)], capsys)[0] == 0

    code, out = run(["run", "list", "--db", db], capsys)
    assert code == 0 and "A_full" in out

    code, out = run(["compare", "--db", db, "--a", "1", "--b", "2"], capsys)
    assert code == 0
    assert "0.000977" in out
    assert "差异显著" in out


def test_ingest_agent_report_via_cli(tmp_path, ids16, native_file, capsys):
    db = str(tmp_path / "cli.db")
    run(["suite", "add", "--db", db, "--name", "m9"], capsys)
    p = native_file(make_agent_report({"A_full": ids16[:11], "C_no_tool": []}, ids16), "agent.json")
    code, out = run(["run", "ingest", "--db", db, "--suite", "1", "--file", str(p), "--label-prefix", "m9"], capsys)
    assert code == 0
    assert "m9:A_full" in out
    assert "m9:C_no_tool" in out


def test_gate_exit_code_reflects_failure(tmp_path, ids16, native_file, capsys):
    db = str(tmp_path / "cli.db")
    run(["suite", "add", "--db", db, "--name", "m9"], capsys)
    a = native_file(make_native("A_full", ids16[:11], ids16), "a.json")
    c = native_file(make_native("C_no_tool", [], ids16), "c.json")
    run(["run", "ingest", "--db", db, "--suite", "1", "--file", str(a)], capsys)
    run(["run", "ingest", "--db", db, "--suite", "1", "--file", str(c)], capsys)

    assert run(["gate", "--db", db, "--run", "1", "--min-pass", "0.6"], capsys)[0] == 0
    assert run(["gate", "--db", db, "--run", "1", "--min-pass", "0.9"], capsys)[0] == 1
    # 候选 0/16 相对基线 11/16 显著变差 → 回归门禁必须返回非零
    assert run(["gate", "--db", db, "--run", "2", "--baseline", "1"], capsys)[0] == 1


def test_gate_without_conditions_is_usage_error(tmp_path, capsys):
    db = str(tmp_path / "cli.db")
    code, _ = run(["gate", "--db", db, "--run", "1"], capsys)
    assert code == 2


def test_report_command(tmp_path, data_dir, ids16, native_file, capsys):
    db = str(tmp_path / "cli.db")
    out_path = tmp_path / "out" / "dashboard.html"
    run(["dataset", "add", "--db", db, "--name", "d", "--source", str(data_dir)], capsys)
    run(["suite", "add", "--db", db, "--name", "m9", "--dataset-version", "1"], capsys)
    a = native_file(make_native("A_full", ids16[:11], ids16), "a.json")
    run(["run", "ingest", "--db", db, "--suite", "1", "--file", str(a)], capsys)
    code, out = run(["report", "--db", db, "--out", str(out_path)], capsys)
    assert code == 0
    assert out_path.exists()


def test_error_message_on_missing_file(tmp_path, capsys):
    db = str(tmp_path / "cli.db")
    run(["suite", "add", "--db", db, "--name", "m9"], capsys)
    code, out = run(["run", "ingest", "--db", db, "--suite", "1", "--file", str(tmp_path / "nope.json")], capsys)
    assert code == 1
    assert "错误" in out
