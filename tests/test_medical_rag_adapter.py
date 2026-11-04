"""medical-rag 检索基准的入库适配。

这组测试守三件事：
1. 通过判据是逐题的**全中**，不是报告里的 overall 自报值；
2. 拒答题默认不进通过率（阈值是逐臂后验最优挑出来的，用它给自己打分是循环论证）；
3. 三个臂落在同一套件里，可以直接按 case_id 配对做 McNemar。
"""

from __future__ import annotations

import json

import pytest

from helpers import make_medical_rag_report
from maldi_platform import evals

ALL_IDS = [f"QA{i:03d}" for i in range(1, 13)]  # 12 条可答题
UNANS_IDS = ["QU001", "QU002", "QU003"]


def _write(tmp_path, payload, name):
    p = tmp_path / name
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return p


def _suite(conn):
    return evals.create_suite(conn, "rag-retrieval", task_type="retrieval")


def test_one_file_one_run_and_pass_uses_per_query_field(conn, tmp_path):
    case_ok = ALL_IDS[:8]
    path = _write(tmp_path, make_medical_rag_report("纯向量", case_ok, ALL_IDS), "vector.json")
    sid = _suite(conn)

    ids = evals.ingest_file(conn, sid, str(path))
    assert len(ids) == 1
    run = evals.get_run(conn, ids[0])
    assert run["label"] == "纯向量"
    assert run["n_cases"] == len(ALL_IDS)  # 拒答题默认不计
    assert run["n_passed"] == 8


def test_overall_self_report_is_ignored(conn, tmp_path):
    """报告自报 overall.full_coverage 故意写错，入库后必须按逐题重算。"""
    payload = make_medical_rag_report("纯向量", ALL_IDS[:6], ALL_IDS)
    payload["overall"]["full_coverage"] = 0.99
    path = _write(tmp_path, payload, "vector.json")
    sid = _suite(conn)

    rid = evals.ingest_file(conn, sid, str(path))[0]
    metrics = {m["name"]: m["value"] for m in evals.run_metrics(conn, rid)}
    assert metrics["pass_rate"] == pytest.approx(6 / 12)


def test_unanswerable_excluded_by_default_but_recorded(conn, tmp_path):
    payload = make_medical_rag_report("精排", ALL_IDS[:9], ALL_IDS, unanswerable_ids=UNANS_IDS)
    path = _write(tmp_path, payload, "rerank.json")
    sid = _suite(conn)

    rid = evals.ingest_file(conn, sid, str(path))[0]
    run = evals.get_run(conn, rid)
    cfg = json.loads(run["config"])
    assert cfg["n_unanswerable_excluded"] == 3
    assert cfg["refusal_threshold_posthoc"] == 5.0
    assert "循环论证" in cfg["note"]
    metrics = {m["name"]: m["value"] for m in evals.run_metrics(conn, rid)}
    assert metrics["unanswerable_top1_mean"] == pytest.approx(3.0)
    # 拒答没被摊进通过率：9/12 而不是 9/15
    assert metrics["pass_rate"] == pytest.approx(9 / 12)


def test_include_unanswerable_adds_them_as_failures(conn, tmp_path):
    payload = make_medical_rag_report("精排", ALL_IDS[:9], ALL_IDS, unanswerable_ids=UNANS_IDS)
    path = _write(tmp_path, payload, "rerank.json")
    sid = _suite(conn)

    rid = evals.ingest_file(conn, sid, str(path), include_unanswerable=True)[0]
    run = evals.get_run(conn, rid)
    assert run["n_cases"] == 15
    assert run["n_passed"] == 9
    assert json.loads(run["config"])["n_unanswerable_excluded"] == 0


def test_answer_recall_and_mrr_means_recomputed_from_cases(conn, tmp_path):
    payload = make_medical_rag_report("纯向量", ALL_IDS[:6], ALL_IDS, mrr=1.0, answer_recall=1.0)
    payload["overall"]["mrr"] = 0.123  # 自报值，故意错
    path = _write(tmp_path, payload, "vector.json")
    sid = _suite(conn)

    rid = evals.ingest_file(conn, sid, str(path))[0]
    metrics = {m["name"]: m["value"] for m in evals.run_metrics(conn, rid)}
    # 6 条 mrr=1.0、6 条 mrr=0.0；answer_recall 6 条 1.0、6 条 0.2
    assert metrics["mrr_mean"] == pytest.approx(0.5)
    assert metrics["answer_recall_mean"] == pytest.approx((1.0 * 6 + 0.2 * 6) / 12)
    assert metrics["mrr_mean"] != pytest.approx(0.123)


def test_paired_compare_across_three_arms(conn, tmp_path):
    sid = _suite(conn)
    weak = _write(tmp_path, make_medical_rag_report("纯向量", ALL_IDS[:3], ALL_IDS), "v.json")
    mid = _write(tmp_path, make_medical_rag_report("精排", ALL_IDS[:7], ALL_IDS), "r.json")
    strong = _write(tmp_path, make_medical_rag_report("句级精排", ALL_IDS[:11], ALL_IDS), "s.json")

    a = evals.ingest_file(conn, sid, str(weak))[0]
    b = evals.ingest_file(conn, sid, str(mid))[0]
    c = evals.ingest_file(conn, sid, str(strong))[0]

    cmp = evals.compare_runs(conn, a, c)
    assert cmp["table"]["a_only"] == 0
    assert cmp["table"]["b_only"] == 8  # 句级精排多救了 8 条
    # 8 对不一致 → 双侧精确 p = 2 * (1/2)^8 = 0.0078
    assert cmp["mcnemar_p"] == pytest.approx(0.0078, abs=1e-4)
    assert cmp["verdict"] == "差异显著"

    # 逐类看：fact 六条、numeric 六条
    assert set(cmp["by_task_class"]) == {"fact", "numeric"}

    # 同一套件、同批样本，三臂可任意配对
    cmp2 = evals.compare_runs(conn, b, c)
    assert cmp2["table"]["b_only"] == 4


def test_regression_gate_blocks_weaker_arm(conn, tmp_path):
    sid = _suite(conn)
    better = _write(tmp_path, make_medical_rag_report("句级精排", ALL_IDS[:11], ALL_IDS), "s.json")
    worse = _write(tmp_path, make_medical_rag_report("纯向量", ALL_IDS[:3], ALL_IDS), "v.json")
    base = evals.ingest_file(conn, sid, str(better))[0]
    cand = evals.ingest_file(conn, sid, str(worse))[0]

    res = evals.gate_no_regression(conn, cand, base)
    assert res["passed"] is False
    assert res["p"] < 0.05

    # 反向不误报
    assert evals.gate_no_regression(conn, base, cand)["passed"] is True


def test_aggregate_only_report_is_rejected(conn, tmp_path):
    """只有聚合结论、没有逐题明细的报告必须拒收，否则等于替对方背书。"""
    sid = _suite(conn)
    p = _write(
        tmp_path,
        {"left": "strict", "right": "naive", "n_pairs": 100, "scopes": {"全部题": {}}},
        "generation_compare.json",
    )
    with pytest.raises(evals.EvalError, match="逐题明细"):
        evals.ingest_file(conn, sid, str(p))


def test_duplicate_arm_label_rejected(conn, tmp_path):
    sid = _suite(conn)
    p1 = _write(tmp_path, make_medical_rag_report("纯向量", ALL_IDS[:6], ALL_IDS), "a.json")
    p2 = _write(tmp_path, make_medical_rag_report("纯向量", ALL_IDS[:7], ALL_IDS), "b.json")
    evals.ingest_file(conn, sid, str(p1))
    with pytest.raises(evals.EvalError, match="已存在同名的 run"):
        evals.ingest_file(conn, sid, str(p2))
