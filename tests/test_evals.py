"""评测中台测试：入库、配对比较、回归门禁。

重点盯三件事：
1. 一个 agent 报告文件拆成多个臂，且臂内 case 一一对应（否则配对比较全部失效）；
2. 门禁在"候选显著变差"时必须拦下来，在"变好"时必须放行；
3. 跨套件比较必须报错（不同批样本不能配对）。
"""
from __future__ import annotations

import json

import pytest

from maldi_platform import datasets, evals
from maldi_platform.evals import EvalError

from helpers import make_agent_report, make_native


# --------------------------------------------------------------------------- #
# 套件
# --------------------------------------------------------------------------- #
def test_create_suite_and_list(conn):
    sid = evals.create_suite(conn, "m9-agent", task_type="agent-perturbation")
    suites = evals.list_suites(conn)
    assert suites[0]["id"] == sid
    assert suites[0]["n_runs"] == 0


def test_duplicate_suite_rejected(conn):
    evals.create_suite(conn, "dup")
    with pytest.raises(EvalError):
        evals.create_suite(conn, "dup")


# --------------------------------------------------------------------------- #
# 入库：原生格式
# --------------------------------------------------------------------------- #
def test_ingest_native_run(conn, ids16):
    sid = evals.create_suite(conn, "s")
    payload = make_native("A-full", passed_ids=ids16[:11], all_ids=ids16)
    (rid,) = evals.ingest_native(conn, sid, payload)
    run = evals.get_run(conn, rid)
    assert run["label"] == "A-full"
    assert run["n_cases"] == 16
    assert run["n_passed"] == 11
    assert run["model"] == "glm-4-flash"


def test_ingest_computes_pass_rate_with_ci(conn, ids16):
    sid = evals.create_suite(conn, "s")
    (rid,) = evals.ingest_native(conn, sid, make_native("A", ids16[:11], ids16))
    metrics = {m["name"]: m for m in evals.run_metrics(conn, rid)}
    assert metrics["pass_rate"]["value"] == pytest.approx(0.6875)
    assert metrics["pass_rate"]["ci_low"] < 0.6875 < metrics["pass_rate"]["ci_high"]
    assert metrics["perm_called_rate"]["value"] == 1.0


def test_ingest_rejects_missing_cases(conn):
    sid = evals.create_suite(conn, "s")
    with pytest.raises(EvalError):
        evals.ingest_native(conn, sid, {"label": "x", "cases": []})


def test_ingest_rejects_missing_label(conn, ids16):
    sid = evals.create_suite(conn, "s")
    payload = make_native("A", ids16[:1], ids16)
    payload.pop("label")
    with pytest.raises(EvalError):
        evals.ingest_native(conn, sid, payload)


def test_ingest_rejects_case_without_id(conn):
    sid = evals.create_suite(conn, "s")
    with pytest.raises(EvalError):
        evals.ingest_native(conn, sid, {"label": "x", "cases": [{"passed": True}]})


def test_ingest_rejects_duplicate_label(conn, ids16):
    sid = evals.create_suite(conn, "s")
    evals.ingest_native(conn, sid, make_native("A", ids16[:11], ids16))
    with pytest.raises(EvalError):
        evals.ingest_native(conn, sid, make_native("A", ids16[:5], ids16))


def test_ingest_rejects_duplicate_case_id(conn, ids16):
    sid = evals.create_suite(conn, "s")
    payload = {
        "label": "A",
        "cases": [
            {"case_id": "T0", "passed": True},
            {"case_id": "T0", "passed": False},
        ],
    }
    with pytest.raises(EvalError):
        evals.ingest_native(conn, sid, payload)


# --------------------------------------------------------------------------- #
# 入库：agent 报告格式
# --------------------------------------------------------------------------- #
def test_ingest_agent_report_splits_arms(conn, ids16):
    sid = evals.create_suite(conn, "m9", task_type="agent-perturbation")
    payload = make_agent_report(
        {"A_full": ids16[:11], "C_no_tool": []},
        ids16,
    )
    ids = evals.ingest_maldi_agent(conn, sid, payload, label_prefix="m9")
    assert len(ids) == 2
    runs = evals.list_runs(conn, sid)
    assert [r["label"] for r in runs] == ["m9:A_full", "m9:C_no_tool"]
    assert runs[0]["n_passed"] == 11
    assert runs[1]["n_passed"] == 0
    assert runs[0]["model"] == "glm-4-flash"


def test_agent_ingest_records_config(conn, ids16):
    sid = evals.create_suite(conn, "m9")
    payload = make_agent_report({"A_full": ids16[:11]}, ids16)
    (rid,) = evals.ingest_maldi_agent(conn, sid, payload, label_prefix="m9")
    cfg = json.loads(evals.get_run(conn, rid)["config"])
    assert cfg["n_perm"] == 30
    assert cfg["seeds"] == [0, 1, 2, 3]


def test_agent_ingest_requires_runs(conn):
    sid = evals.create_suite(conn, "m9")
    with pytest.raises(EvalError):
        evals.ingest_maldi_agent(conn, sid, {"runs": []})


def test_ingest_file_dispatches_by_shape(conn, ids16, native_file):
    sid = evals.create_suite(conn, "mixed")
    native_path = native_file(make_native("A", ids16[:11], ids16), "native.json")
    assert len(evals.ingest_file(conn, sid, str(native_path))) == 1

    agent_path = native_file(make_agent_report({"A_full": ids16[:11], "C_no_tool": []}, ids16), "agent.json")
    assert len(evals.ingest_file(conn, sid, str(agent_path), label_prefix="m9")) == 2


# --------------------------------------------------------------------------- #
# 比较
# --------------------------------------------------------------------------- #
def test_compare_identical_runs_is_not_significant(conn, ids16):
    sid = evals.create_suite(conn, "s")
    (a,) = evals.ingest_native(conn, sid, make_native("A", ids16[:11], ids16))
    (b,) = evals.ingest_native(conn, sid, make_native("A-copy", ids16[:11], ids16))
    r = evals.compare_runs(conn, a, b)
    assert r["table"]["a_only"] == 0 and r["table"]["b_only"] == 0
    assert r["mcnemar_p"] == 1.0
    assert r["verdict"] == "差异不显著"


def test_compare_full_vs_no_tool_is_significant(conn, ids16):
    """真实实测形状：A 11/16、C 0/16，仅 A 过 11、仅 C 过 0 → p≈0.000977。"""
    sid = evals.create_suite(conn, "s")
    (a,) = evals.ingest_native(conn, sid, make_native("A", ids16[:11], ids16))
    (b,) = evals.ingest_native(conn, sid, make_native("C", [], ids16))
    r = evals.compare_runs(conn, a, b)
    assert r["table"]["a_only"] == 11
    assert r["table"]["b_only"] == 0
    assert r["mcnemar_p"] == pytest.approx(2 / 2 ** 11, rel=1e-6)
    assert r["verdict"] == "差异显著"


def test_compare_reports_per_task_class(conn, ids16):
    sid = evals.create_suite(conn, "s")
    (a,) = evals.ingest_native(conn, sid, make_native("A", ids16[:11], ids16))
    (b,) = evals.ingest_native(conn, sid, make_native("C", [], ids16))
    r = evals.compare_runs(conn, a, b)
    assert "baseline" in r["by_task_class"]
    assert r["by_task_class"]["baseline"]["a_n"] > 0


def test_compare_across_suites_rejected(conn, ids16):
    s1 = evals.create_suite(conn, "s1")
    s2 = evals.create_suite(conn, "s2")
    (a,) = evals.ingest_native(conn, s1, make_native("A", ids16[:11], ids16))
    (b,) = evals.ingest_native(conn, s2, make_native("B", ids16[:5], ids16))
    with pytest.raises(EvalError):
        evals.compare_runs(conn, a, b)


def test_compare_handles_partial_overlap(conn):
    """两次 run 的 case 集合不完全一致时只比交集，不把缺失当失败。"""
    sid = evals.create_suite(conn, "s")
    (a,) = evals.ingest_native(
        conn, sid, {"label": "A", "cases": [{"case_id": "x", "passed": True}, {"case_id": "y", "passed": True}]}
    )
    (b,) = evals.ingest_native(conn, sid, {"label": "B", "cases": [{"case_id": "x", "passed": True}]})
    r = evals.compare_runs(conn, a, b)
    assert r["table"]["n_paired"] == 1
    assert r["table"]["both"] == 1


# --------------------------------------------------------------------------- #
# 门禁
# --------------------------------------------------------------------------- #
def test_gate_min_pass_rate_passes_and_fails(conn, ids16):
    sid = evals.create_suite(conn, "s")
    (rid,) = evals.ingest_native(conn, sid, make_native("A", ids16[:11], ids16))
    assert evals.gate_min_pass_rate(conn, rid, 0.6)["passed"] is True
    assert evals.gate_min_pass_rate(conn, rid, 0.8)["passed"] is False


def test_gate_no_regression_blocks_significantly_worse(conn, ids16):
    """基线 11/16、候选 0/16：候选显著变差，门禁必须拦。"""
    sid = evals.create_suite(conn, "s")
    (base,) = evals.ingest_native(conn, sid, make_native("baseline", ids16[:11], ids16))
    (cand,) = evals.ingest_native(conn, sid, make_native("cand", [], ids16))
    r = evals.gate_no_regression(conn, cand, base)
    assert r["passed"] is False
    assert r["p"] < 0.05


def test_gate_no_regression_allows_improvement(conn, ids16):
    sid = evals.create_suite(conn, "s")
    (base,) = evals.ingest_native(conn, sid, make_native("baseline", [], ids16))
    (cand,) = evals.ingest_native(conn, sid, make_native("cand", ids16[:11], ids16))
    r = evals.gate_no_regression(conn, cand, base)
    assert r["passed"] is True


def test_gate_no_regression_tolerates_noise(conn, ids16):
    """候选只少过 1 条（n=16 的噪声范围），不该被判回归。"""
    sid = evals.create_suite(conn, "s")
    (base,) = evals.ingest_native(conn, sid, make_native("baseline", ids16[:11], ids16))
    (cand,) = evals.ingest_native(conn, sid, make_native("cand", ids16[:10], ids16))
    r = evals.gate_no_regression(conn, cand, base)
    assert r["passed"] is True


def test_gate_require_tool(conn, ids16):
    sid = evals.create_suite(conn, "s")
    payload = make_native("A", ids16[:11], ids16)
    for c in payload["cases"]:
        c["detail"] = {"called_permutation": False}
    (rid,) = evals.ingest_native(conn, sid, payload)
    r = evals.gate_require_tool(conn, rid)
    assert r["passed"] is False


def test_gates_persisted(conn, ids16):
    sid = evals.create_suite(conn, "s")
    (rid,) = evals.ingest_native(conn, sid, make_native("A", ids16[:11], ids16))
    evals.gate_min_pass_rate(conn, rid, 0.6)
    evals.gate_require_tool(conn, rid)
    rows = evals.list_gates(conn, rid)
    assert {r["name"] for r in rows} == {"min_pass_rate", "require_perm_called_rate"}
    assert all(r["passed"] == 1 for r in rows)


# --------------------------------------------------------------------------- #
# 血缘
# --------------------------------------------------------------------------- #
def test_ingest_links_dataset_version(conn, ids16, data_dir):
    ds = datasets.register_dataset(conn, "synth")
    v = datasets.add_version(conn, ds, "v1", source=data_dir)
    sid = evals.create_suite(conn, "s", dataset_version_id=v)
    (rid,) = evals.ingest_native(conn, sid, make_native("A", ids16[:11], ids16))
    edges = datasets.lineage_of(conn, "run", rid)
    assert len(edges) == 1
    assert edges[0]["src_id"] == v


def test_ingest_without_dataset_version_has_no_lineage(conn, ids16):
    sid = evals.create_suite(conn, "s")
    (rid,) = evals.ingest_native(conn, sid, make_native("A", ids16[:11], ids16))
    assert datasets.lineage_of(conn, "run", rid) == []
