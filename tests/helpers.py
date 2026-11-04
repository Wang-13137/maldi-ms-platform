"""测试用的数据构造器（放独立模块，避免从 conftest 反向 import）。"""
from __future__ import annotations

from typing import Any, Dict, List


def ids16() -> List[str]:
    """4 类任务 × 4 个 seed = 16 条，与该项目的真实实测规模一致。"""
    kinds = ["baseline", "null", "batch", "lure"]
    return [f"T{i}-{k}@s{s}" for s in range(4) for i, k in enumerate(kinds)]


def make_native(label: str, passed_ids: List[str], all_ids: List[str], **extra: Any) -> Dict[str, Any]:
    """平台原生格式：一个文件一个 run。"""
    payload: Dict[str, Any] = {
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


def make_agent_report(arms: Dict[str, List[str]], all_ids: List[str]) -> Dict[str, Any]:
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
