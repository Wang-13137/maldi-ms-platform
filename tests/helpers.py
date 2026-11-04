"""测试用的数据构造器（放独立模块，避免从 conftest 反向 import）。"""

from __future__ import annotations

from typing import Any


def ids16() -> list[str]:
    """4 类任务 × 4 个 seed = 16 条，与该项目的真实实测规模一致。"""
    kinds = ["baseline", "null", "batch", "lure"]
    return [f"T{i}-{k}@s{s}" for s in range(4) for i, k in enumerate(kinds)]


def make_native(label: str, passed_ids: list[str], all_ids: list[str], **extra: Any) -> dict[str, Any]:
    """平台原生格式：一个文件一个 run。"""
    payload: dict[str, Any] = {
        "label": label,
        "arm": extra.pop("arm", label),
        "model": extra.pop("model", "glm-4-flash"),
        "cases": [
            {
                "case_id": cid,
                "task_class": "baseline" if "-baseline" in cid else "null",
                "passed": cid in passed_ids,
                "detail": {"called_permutation": True},
            }
            for cid in all_ids
        ],
    }
    payload.update(extra)
    return payload


def make_agent_report(arms: dict[str, list[str]], all_ids: list[str]) -> dict[str, Any]:
    """仿 maldi-ms-agent 报告：一个文件里含多个消融臂（runs[].group 区分）。"""
    runs = []
    for arm, passed_ids in arms.items():
        for cid in all_ids:
            runs.append(
                {
                    "task": {"id": cid, "kind": "baseline" if "-baseline" in cid else "null"},
                    "grade": {
                        "correct": cid in passed_ids,
                        "called_permutation": arm != "C_no_tool",
                        "used_batch_check": False,
                        "claims_discovery": arm == "C_no_tool",
                        "failure_mode": "" if cid in passed_ids else "未做置换检验就下结论",
                        "perm_p": 0.002,
                    },
                    "group": arm,
                }
            )
    return {
        "agent": "openai",
        "model": "glm-4-flash",
        "n_perm": 30,
        "seeds": [0, 1, 2, 3],
        "offline": False,
        "warning": "",
        "runs": runs,
    }


def make_medical_rag_report(
    mode: str,
    full_cover_ids: list[str],
    all_ids: list[str],
    *,
    mrr: float = 0.9,
    answer_recall: float = 0.9,
    unanswerable_ids: list[str] | None = None,
    threshold: float | None = 5.0,
) -> dict[str, Any]:
    """仿 medical-rag 的 benchmark_eval_*.json：一个文件一个臂，逐题明细齐全。"""
    unanswerable_ids = unanswerable_ids or []
    answerable = []
    for cid in all_ids:
        hit = cid in full_cover_ids
        answerable.append(
            {
                "id": cid,
                "type": "fact" if int("".join(ch for ch in cid if ch.isdigit()) or 0) <= 6 else "numeric",
                "gold_doc": f"{cid}.txt",
                "first_gold_rank": 1 if hit else 9,
                "mrr": mrr if hit else 0.0,
                "answer_recall": answer_recall if hit else 0.2,
                "full_coverage": 1.0 if hit else 0.0,
                "hit_keys": 2 if hit else 1,
                "total_keys": 2,
                "missing_keys": [] if hit else ["k2"],
                "top_score": 8.0 if hit else 4.0,
                "top1_doc": f"{cid}.txt",
            }
        )
    unanswerable = [
        {
            "id": qid,
            "type": "unanswerable",
            "gold_doc": None,
            "first_gold_rank": None,
            "mrr": 0.0,
            "answer_recall": 0.0,
            "full_coverage": 0.0,
            "hit_keys": 0,
            "total_keys": 0,
            "missing_keys": [],
            "top_score": 3.0,
            "top1_doc": "some_doc.txt",
        }
        for qid in unanswerable_ids
    ]
    return {
        "mode": mode,
        "index": "data/index/faiss_clean_base.index",
        "budget_chars": 2400,
        "top_n": 8,
        "candidate_k": 40,
        "overall": {"n": len(answerable), "full_coverage": len(full_cover_ids) / len(all_ids)},
        "refusal_tradeoff": {"best": {"threshold": threshold}},
        "answerable_per_query": answerable,
        "unanswerable_per_query": unanswerable,
    }
