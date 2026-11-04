"""评测中台：评测套件、运行入库、指标、显著性、基线回归、门禁。

两件事是这个模块存在的理由：
1. **同一次评测的所有臂（arm）落在同一张表里**，比较时自动按 case_id 配对，
   避免"拿 16 条和 16 条各算一个比率然后比大小"这种在小样本上必然翻车的做法。
2. **回归是硬判据，不是感觉**。``gate_regression`` 拿候选 run 和基线 run 配对比较，
   差异不显著就不许说"变好了"。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .datasets import add_lineage
from .db import now_iso
from .stats import mcnemar_exact, paired_table, rate_with_ci, verdict

# agent 仓库报告里 detail 可能很长的字段，入库前截断，避免单条 SQL 几 MB
_DETAIL_LIMIT = 600

# detail 里这些连续型字段会被重算成 ``<字段>_mean`` 指标
_MEAN_DETAIL_FIELDS = ("answer_recall", "mrr")


class EvalError(RuntimeError):
    """评测中台的业务错误。"""


# --------------------------------------------------------------------------- #
# 套件
# --------------------------------------------------------------------------- #
def create_suite(
    conn: sqlite3.Connection,
    name: str,
    *,
    dataset_version_id: Optional[int] = None,
    task_type: Optional[str] = None,
    metric_spec: Optional[Dict[str, Any]] = None,
) -> int:
    """建一个评测套件。套件绑定数据版本，是为了让"这次评测用的是哪份数据"有据可查。"""
    try:
        cur = conn.execute(
            "INSERT INTO suites(name, dataset_version_id, task_type, metric_spec, created_at)"
            " VALUES(?, ?, ?, ?, ?)",
            (
                name,
                dataset_version_id,
                task_type,
                json.dumps(metric_spec or {}, ensure_ascii=False),
                now_iso(),
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise EvalError(f"套件已存在：{name}") from exc
    conn.commit()
    return int(cur.lastrowid)


def get_suite(conn: sqlite3.Connection, name_or_id: str | int) -> Dict[str, Any]:
    if isinstance(name_or_id, int) or str(name_or_id).isdigit():
        row = conn.execute("SELECT * FROM suites WHERE id = ?", (int(name_or_id),)).fetchone()
    else:
        row = conn.execute("SELECT * FROM suites WHERE name = ?", (name_or_id,)).fetchone()
    if row is None:
        raise EvalError(f"套件不存在：{name_or_id}")
    return dict(row)


def list_suites(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT s.*, COUNT(r.id) AS n_runs
        FROM suites s LEFT JOIN runs r ON r.suite_id = s.id
        GROUP BY s.id ORDER BY s.id
        """
    ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# 入库
# --------------------------------------------------------------------------- #
def _trim(detail: Any) -> Dict[str, Any]:
    """把 detail 里的长字符串截断；非 dict 一律包一层，保证列可解析。"""
    if not isinstance(detail, dict):
        return {"value": detail}
    out: Dict[str, Any] = {}
    for k, v in detail.items():
        if isinstance(v, str) and len(v) > _DETAIL_LIMIT:
            out[k] = v[:_DETAIL_LIMIT] + "…"
        else:
            out[k] = v
    return out


def _insert_run(
    conn: sqlite3.Connection,
    suite_id: int,
    *,
    label: str,
    arm: Optional[str],
    model: Optional[str],
    git_commit: Optional[str],
    config: Optional[Dict[str, Any]],
    source_file: Optional[str],
    started_at: Optional[str],
    cases: Sequence[Dict[str, Any]],
) -> int:
    n_cases = len(cases)
    n_passed = sum(1 for c in cases if c.get("passed"))

    try:
        cur = conn.execute(
            "INSERT INTO runs(suite_id, label, arm, model, git_commit, config, source_file,"
            " n_cases, n_passed, started_at, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                suite_id,
                label,
                arm,
                model,
                git_commit,
                json.dumps(config or {}, ensure_ascii=False),
                source_file,
                n_cases,
                n_passed,
                started_at,
                now_iso(),
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise EvalError(f"同一套件下已存在同名的 run：{label}") from exc
    run_id = int(cur.lastrowid)

    for c in cases:
        case_id = str(c.get("case_id") or "").strip()
        if not case_id:
            raise EvalError(f"run {label} 中有 case 缺少 case_id")
        try:
            conn.execute(
                "INSERT INTO cases(run_id, case_id, task_class, passed, detail) VALUES(?, ?, ?, ?, ?)",
                (
                    run_id,
                    case_id,
                    c.get("task_class"),
                    1 if c.get("passed") else 0,
                    json.dumps(_trim(c.get("detail")), ensure_ascii=False),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise EvalError(f"run {label} 中 case 重复：{case_id}") from exc

    _refresh_metrics(conn, run_id)
    conn.commit()
    return run_id


def _refresh_metrics(conn: sqlite3.Connection, run_id: int) -> None:
    """按 cases 重算指标。重复调用是幂等的，方便修数据后重跑。"""
    rows = conn.execute(
        "SELECT case_id, passed, detail FROM cases WHERE run_id = ?", (run_id,)
    ).fetchall()
    n = len(rows)
    k = sum(1 for r in rows if r["passed"])
    stats = rate_with_ci(k, n)
    _upsert_metric(conn, run_id, "pass_rate", stats["rate"], n, stats["ci_low"], stats["ci_high"])

    flags = []
    means: Dict[str, List[float]] = {}
    for r in rows:
        try:
            d = json.loads(r["detail"] or "{}")
        except json.JSONDecodeError:
            continue
        if "called_permutation" in d:
            flags.append(bool(d["called_permutation"]))
        # 逐题连续型指标（检索类）也从 cases 重算均值，同样不信任输入方自报的 overall
        for field in _MEAN_DETAIL_FIELDS:
            v = d.get(field)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                means.setdefault(field, []).append(float(v))
    if flags:
        ok = sum(1 for f in flags if f)
        s = rate_with_ci(ok, len(flags))
        _upsert_metric(conn, run_id, "perm_called_rate", s["rate"], s["n"], s["ci_low"], s["ci_high"])
    for field, vals in means.items():
        _upsert_metric(conn, run_id, f"{field}_mean", sum(vals) / len(vals), len(vals))


def _upsert_metric(
    conn: sqlite3.Connection,
    run_id: int,
    name: str,
    value: float,
    n: Optional[int] = None,
    ci_low: Optional[float] = None,
    ci_high: Optional[float] = None,
) -> None:
    conn.execute(
        "INSERT INTO metrics(run_id, name, value, n, ci_low, ci_high) VALUES(?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(run_id, name) DO UPDATE SET value=excluded.value, n=excluded.n,"
        " ci_low=excluded.ci_low, ci_high=excluded.ci_high",
        (run_id, name, float(value), n, ci_low, ci_high),
    )


def ingest_native(
    conn: sqlite3.Connection,
    suite_id: int,
    payload: Dict[str, Any],
    *,
    source_file: Optional[str] = None,
    label: Optional[str] = None,
) -> List[int]:
    """入库平台自己的结果格式（一个文件一个 run）。

    最小格式::

        {"label": "A-full", "model": "glm-4-flash",
         "cases": [{"case_id": "T0@0", "task_class": "baseline", "passed": true}]}
    """
    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        raise EvalError("payload 缺少非空的 cases 数组")
    run_label = label or payload.get("label")
    if not run_label:
        raise EvalError("payload 缺少 label（或用 --label 指定）")

    run_id = _insert_run(
        conn,
        suite_id,
        label=str(run_label),
        arm=payload.get("arm"),
        model=payload.get("model"),
        git_commit=payload.get("git_commit"),
        config=payload.get("config"),
        source_file=source_file,
        started_at=payload.get("started_at"),
        cases=cases,
    )
    _link_suite_dataset(conn, suite_id, run_id)
    return [run_id]


def ingest_maldi_agent(
    conn: sqlite3.Connection,
    suite_id: int,
    payload: Dict[str, Any],
    *,
    source_file: Optional[str] = None,
    label_prefix: Optional[str] = None,
) -> List[int]:
    """入库 maldi-ms-agent 的评测报告（一个文件里含多个消融臂）。

    兼容字段：``runs[].group``（臂）、``runs[].task.{id,kind}``、``runs[].grade.correct``。
    一个文件会被拆成 N 个 run（每个臂一个），因为它们本来就是配对的同批样本。
    """
    runs = payload.get("runs")
    if not isinstance(runs, list) or not runs:
        raise EvalError("payload 缺少非空的 runs 数组")

    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for r in runs:
        arm = r.get("group") or payload.get("group") or "ALL"
        buckets.setdefault(str(arm), []).append(r)

    base = label_prefix or payload.get("label") or payload.get("model") or "agent"
    model = payload.get("model")
    config = {
        "agent": payload.get("agent"),
        "n_perm": payload.get("n_perm"),
        "seeds": payload.get("seeds"),
        "offline": payload.get("offline"),
        "partial": payload.get("partial"),
        "warning": payload.get("warning"),
    }

    run_ids: List[int] = []
    for arm, items in buckets.items():
        cases = []
        for it in items:
            task = it.get("task") or {}
            grade = it.get("grade") or {}
            cases.append(
                {
                    "case_id": str(task.get("id") or ""),
                    "task_class": task.get("kind"),
                    "passed": bool(grade.get("correct")),
                    "detail": {
                        "called_permutation": grade.get("called_permutation"),
                        "used_batch_check": grade.get("used_batch_check"),
                        "claims_discovery": grade.get("claims_discovery"),
                        "failure_mode": grade.get("failure_mode"),
                        "perm_p": grade.get("perm_p"),
                    },
                }
            )
        run_label = base if len(buckets) == 1 else f"{base}:{arm}"
        run_id = _insert_run(
            conn,
            suite_id,
            label=run_label,
            arm=arm,
            model=model,
            git_commit=payload.get("git_commit"),
            config=config,
            source_file=source_file,
            started_at=payload.get("started_at"),
            cases=cases,
        )
        _link_suite_dataset(conn, suite_id, run_id)
        run_ids.append(run_id)
    return run_ids


def ingest_medical_rag(
    conn: sqlite3.Connection,
    suite_id: int,
    payload: Dict[str, Any],
    *,
    source_file: Optional[str] = None,
    label: Optional[str] = None,
    include_unanswerable: bool = False,
) -> List[int]:
    """入库 medical-rag 的检索基准报告（``benchmark_eval_*.json``）。

    一个文件就是一个臂（纯向量 / 精排 / 句级精排），所以一个文件入一个 run。
    通过判据用**全中**（``full_coverage == 1``）：该题的所有关键串都检索到了。

    拒答题默认**不进通过率**，原因写在 ``config.note`` 里：报告里的 refusal
    threshold 是用同一批数据按 Youden J 后验挑出来的最优阈值，拿它给自己的拒答
    打分等于循环论证；而且每个臂的阈值不同，跨臂比较会同时混进"检索质量"和
    "阈值挑得好不好"两个变量。拒答质量单独记成观测指标，不参与门禁。
    """
    answerable = payload.get("answerable_per_query")
    if not isinstance(answerable, list) or not answerable:
        raise EvalError("payload 缺少非空的 answerable_per_query 数组")
    unanswerable = payload.get("unanswerable_per_query") or []

    cases: List[Dict[str, Any]] = []
    for q in answerable:
        cov = q.get("full_coverage")
        cases.append(
            {
                "case_id": str(q.get("id") or "").strip(),
                "task_class": q.get("type") or "unknown",
                "passed": isinstance(cov, (int, float)) and float(cov) >= 1.0,
                "detail": {
                    "answer_recall": q.get("answer_recall"),
                    "mrr": q.get("mrr"),
                    "first_gold_rank": q.get("first_gold_rank"),
                    "missing_keys": q.get("missing_keys"),
                },
            }
        )

    if include_unanswerable:
        for q in unanswerable:
            cases.append(
                {
                    "case_id": str(q.get("id") or "").strip(),
                    "task_class": "unanswerable",
                    # 检索阶段没有"拒答"这个动作，这里退化为"没检索到任何 gold"，
                    # 恒为 False；因此只有显式要求时才纳入，默认不掺进通过率。
                    "passed": False,
                    "detail": {"top_score": q.get("top_score"), "top1_doc": q.get("top1_doc")},
                }
            )

    best = (payload.get("refusal_tradeoff") or {}).get("best") or {}
    unans_scores = [
        float(q["top_score"]) for q in unanswerable
        if isinstance(q.get("top_score"), (int, float))
    ]
    config = {
        "source_project": "medical-rag",
        "benchmark_mode": payload.get("mode"),
        "index": payload.get("index"),
        "budget_chars": payload.get("budget_chars"),
        "top_n": payload.get("top_n"),
        "candidate_k": payload.get("candidate_k"),
        "built_at": payload.get("built_at"),
        "n_unanswerable_excluded": 0 if include_unanswerable else len(unanswerable),
        "unanswerable_top1_mean": round(sum(unans_scores) / len(unans_scores), 4) if unans_scores else None,
        "refusal_threshold_posthoc": best.get("threshold"),
        "note": "拒答题默认不入通过率：阈值按 Youden J 后验挑选且逐臂不同，"
                "用它给自己的拒答打分是循环论证，也会把检索质量与阈值调参混为一个差值。",
    }

    run_id = _insert_run(
        conn,
        suite_id,
        label=label or payload.get("mode") or "medical-rag",
        arm=payload.get("mode"),
        model=payload.get("index"),
        git_commit=payload.get("git_commit"),
        config=config,
        source_file=source_file,
        started_at=payload.get("built_at"),
        cases=cases,
    )
    if unans_scores:
        _upsert_metric(
            conn, run_id, "unanswerable_top1_mean",
            sum(unans_scores) / len(unans_scores), len(unans_scores),
        )
    _link_suite_dataset(conn, suite_id, run_id)
    return [run_id]


def ingest_file(
    conn: sqlite3.Connection,
    suite_id: int,
    path: str,
    *,
    label: Optional[str] = None,
    label_prefix: Optional[str] = None,
    include_unanswerable: bool = False,
) -> List[int]:
    """按文件内容自动判格式入库。

    - 有 ``runs`` → maldi-ms-agent 报告（一个文件拆成多个消融臂）；
    - 有 ``answerable_per_query`` → medical-rag 检索基准（一个文件一个臂）；
    - 有 ``cases`` → 平台原生格式。
    """
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)

    if not isinstance(payload, dict):
        raise EvalError(f"不认识的报告格式（顶层不是对象）：{path}")
    if "runs" in payload:
        return ingest_maldi_agent(conn, suite_id, payload, source_file=str(path), label_prefix=label_prefix)
    if "answerable_per_query" in payload:
        return ingest_medical_rag(
            conn, suite_id, payload,
            source_file=str(path), label=label, include_unanswerable=include_unanswerable,
        )
    if "cases" not in payload:
        raise EvalError(
            "不认识的报告格式：既没有 runs / answerable_per_query，也没有 cases。"
            "只有聚合结论（如 generation_compare_*.json）的报告不含逐题明细，"
            "无法重算指标，平台不接收——这正是「不信任自报数字」这条设计。"
        )
    return ingest_native(conn, suite_id, payload, source_file=str(path), label=label)


def _link_suite_dataset(conn: sqlite3.Connection, suite_id: int, run_id: int) -> None:
    suite = conn.execute("SELECT dataset_version_id FROM suites WHERE id = ?", (suite_id,)).fetchone()
    if suite and suite["dataset_version_id"]:
        add_lineage(
            conn,
            src_kind="dataset_version",
            src_id=int(suite["dataset_version_id"]),
            dst_kind="run",
            dst_id=run_id,
            relation="consumed_by",
        )


# --------------------------------------------------------------------------- #
# 查询
# --------------------------------------------------------------------------- #
def list_runs(conn: sqlite3.Connection, suite_id: Optional[int] = None) -> List[Dict[str, Any]]:
    sql = """
        SELECT r.*, m.ci_low, m.ci_high
        FROM runs r LEFT JOIN metrics m ON m.run_id = r.id AND m.name = 'pass_rate'
    """
    params: Sequence[Any] = ()
    if suite_id is not None:
        sql += " WHERE r.suite_id = ?"
        params = (suite_id,)
    sql += " ORDER BY r.id"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_run(conn: sqlite3.Connection, run_id: int) -> Dict[str, Any]:
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise EvalError(f"run 不存在：id={run_id}")
    return dict(row)


def run_cases(conn: sqlite3.Connection, run_id: int) -> Dict[str, bool]:
    """取 run 的 {case_id: passed}，比较时用。"""
    rows = conn.execute("SELECT case_id, passed FROM cases WHERE run_id = ?", (run_id,)).fetchall()
    return {r["case_id"]: bool(r["passed"]) for r in rows}


def run_metrics(conn: sqlite3.Connection, run_id: int) -> List[Dict[str, Any]]:
    rows = conn.execute("SELECT * FROM metrics WHERE run_id = ? ORDER BY name", (run_id,)).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# 比较
# --------------------------------------------------------------------------- #
def compare_runs(conn: sqlite3.Connection, a_id: int, b_id: int) -> Dict[str, Any]:
    """配对比较两个 run。

    返回 2x2 表、McNemar 精确 p、各自的通过率与 Wilson 区间、分任务类的差值。
    直接比较两个独立比率是错的——同一批样本上跑的两个臂是配对的，
    配对信息能消掉样本难度带来的方差，这在 n=16 的小样本上是决定性的。
    """
    a, b = get_run(conn, a_id), get_run(conn, b_id)
    if a["suite_id"] != b["suite_id"]:
        raise EvalError("只能比较同一套件下的 run（样本必须来自同一批）")

    pa, pb = run_cases(conn, a_id), run_cases(conn, b_id)
    table = paired_table(pa, pb)
    p = mcnemar_exact(table["a_only"], table["b_only"])

    by_class: Dict[str, Dict[str, Any]] = {}
    rows = conn.execute(
        "SELECT run_id, task_class, passed FROM cases WHERE run_id IN (?, ?)", (a_id, b_id)
    ).fetchall()
    for r in rows:
        cls = r["task_class"] or "unknown"
        slot = by_class.setdefault(cls, {"a_n": 0, "a_k": 0, "b_n": 0, "b_k": 0})
        if r["run_id"] == a_id:
            slot["a_n"] += 1
            slot["a_k"] += int(r["passed"])
        else:
            slot["b_n"] += 1
            slot["b_k"] += int(r["passed"])
    for cls, slot in by_class.items():
        slot["a_rate"] = round(slot["a_k"] / slot["a_n"], 4) if slot["a_n"] else 0.0
        slot["b_rate"] = round(slot["b_k"] / slot["b_n"], 4) if slot["b_n"] else 0.0

    return {
        "a": {"id": a_id, "label": a["label"], "arm": a["arm"], "n": a["n_cases"], "k": a["n_passed"],
              "rate": round(a["n_passed"] / a["n_cases"], 4) if a["n_cases"] else 0.0},
        "b": {"id": b_id, "label": b["label"], "arm": b["arm"], "n": b["n_cases"], "k": b["n_passed"],
              "rate": round(b["n_passed"] / b["n_cases"], 4) if b["n_cases"] else 0.0},
        "table": table,
        "mcnemar_p": p,
        "verdict": verdict(p),
        "by_task_class": by_class,
        "note": "配对 McNemar 精确检验（双侧）；b=a_only 表示仅 %s 通过，c=b_only 表示仅 %s 通过"
        % (a["label"], b["label"]),
    }


# --------------------------------------------------------------------------- #
# 门禁
# --------------------------------------------------------------------------- #
def record_gate(
    conn: sqlite3.Connection, run_id: int, name: str, passed: bool, detail: str = ""
) -> int:
    cur = conn.execute(
        "INSERT INTO gates(run_id, name, passed, detail, created_at) VALUES(?, ?, ?, ?, ?)"
        " ON CONFLICT(run_id, name) DO UPDATE SET passed=excluded.passed,"
        " detail=excluded.detail, created_at=excluded.created_at",
        (run_id, name, 1 if passed else 0, detail, now_iso()),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def gate_min_pass_rate(
    conn: sqlite3.Connection, run_id: int, threshold: float, name: Optional[str] = None
) -> Dict[str, Any]:
    """通过率下限。用于"这一版不许比上一版差太多"的底线守护。"""
    run = get_run(conn, run_id)
    rate = run["n_passed"] / run["n_cases"] if run["n_cases"] else 0.0
    ok = rate >= threshold
    detail = f"pass_rate={rate:.4f} 阈值={threshold:.4f}（{run['n_passed']}/{run['n_cases']}）"
    record_gate(conn, run_id, name or "min_pass_rate", ok, detail)
    return {"name": name or "min_pass_rate", "passed": ok, "detail": detail}


def gate_no_regression(
    conn: sqlite3.Connection,
    run_id: int,
    baseline_id: int,
    *,
    alpha: float = 0.05,
    name: Optional[str] = None,
) -> Dict[str, Any]:
    """回归门禁：候选相对基线**显著变差**才判失败。

    刻意不用"通过率低一点就算回归"——小样本上 1/16 的波动完全可能是噪声。
    只有当 McNemar 显示候选显著劣于基线（b_only > a_only 且 p < alpha）才拦。
    """
    cmp = compare_runs(conn, baseline_id, run_id)  # a=baseline, b=candidate
    table = cmp["table"]
    # a_only = 基线过、候选没过（候选丢掉的）；b_only = 候选新增通过的。
    # 只有"丢的显著多于赢的"才算回归。
    regressed = table["a_only"] > table["b_only"] and cmp["mcnemar_p"] < alpha
    ok = not regressed
    detail = (
        f"baseline={cmp['a']['label']}({cmp['a']['k']}/{cmp['a']['n']}) "
        f"candidate={cmp['b']['label']}({cmp['b']['k']}/{cmp['b']['n']}) "
        f"仅基线过={table['a_only']} 仅候选过={table['b_only']} p={cmp['mcnemar_p']}"
    )
    record_gate(conn, run_id, name or "no_regression", ok, detail)
    return {"name": name or "no_regression", "passed": ok, "detail": detail, "p": cmp["mcnemar_p"]}


def gate_require_tool(
    conn: sqlite3.Connection, run_id: int, metric: str = "perm_called_rate", threshold: float = 1.0,
    name: Optional[str] = None
) -> Dict[str, Any]:
    """过程性门禁：结论前必须调用某个工具（默认置换检验），按指标值判定。"""
    rows = conn.execute("SELECT value FROM metrics WHERE run_id = ? AND name = ?", (run_id, metric)).fetchall()
    value = rows[0]["value"] if rows else 0.0
    ok = value >= threshold
    detail = f"{metric}={value:.4f} 阈值={threshold:.4f}"
    record_gate(conn, run_id, name or f"require_{metric}", ok, detail)
    return {"name": name or f"require_{metric}", "passed": ok, "detail": detail}


def list_gates(conn: sqlite3.Connection, run_id: Optional[int] = None) -> List[Dict[str, Any]]:
    if run_id is None:
        rows = conn.execute("SELECT * FROM gates ORDER BY run_id, id").fetchall()
    else:
        rows = conn.execute("SELECT * FROM gates WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()
    return [dict(r) for r in rows]


__all__ = [
    "EvalError",
    "create_suite",
    "get_suite",
    "list_suites",
    "ingest_native",
    "ingest_maldi_agent",
    "ingest_medical_rag",
    "ingest_file",
    "list_runs",
    "get_run",
    "run_cases",
    "run_metrics",
    "compare_runs",
    "record_gate",
    "gate_min_pass_rate",
    "gate_no_regression",
    "gate_require_tool",
    "list_gates",
]
