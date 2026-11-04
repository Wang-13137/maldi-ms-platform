"""命令行入口：数据中台与评测中台的全部操作都能用一条命令完成。

约定：
- 所有命令都要 ``--db``，默认 ``platform.db``，方便本地和 CI 用不同库。
- 输出默认是给人看的中文文本；``--json`` 输出机器可读结果，便于脚本与 CI 断言。
- 门禁命令在未通过时返回退出码 1，能直接当 CI 步骤用。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import __version__, datasets, evals, report
from .db import init_db

DEFAULT_DB = "platform.db"


# --------------------------------------------------------------------------- #
# 输出辅助
# --------------------------------------------------------------------------- #
def _print_json(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _table(headers: List[str], rows: List[List[Any]]) -> None:
    """极简定宽表。中文按两个字符宽度算，避免列错位。"""
    def width(s: Any) -> int:
        text = str(s)
        return sum(2 if ord(ch) > 0x2E80 else 1 for ch in text)

    str_rows = [[str(c) for c in r] for r in rows]
    widths = [width(h) for h in headers]
    for r in str_rows:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], width(cell))

    def pad(text: str, w: int) -> str:
        return text + " " * (w - width(text))

    print("  ".join(pad(h, widths[i]) for i, h in enumerate(headers)))
    print("  ".join("-" * widths[i] for i in range(len(headers))))
    for r in str_rows:
        print("  ".join(pad(c, widths[i]) for i, c in enumerate(r)))


# --------------------------------------------------------------------------- #
# 子命令实现
# --------------------------------------------------------------------------- #
def cmd_init(args: argparse.Namespace) -> int:
    init_db(args.db)
    print(f"已初始化数据库：{args.db}（schema v1）")
    return 0


def cmd_dataset_add(args: argparse.Namespace) -> int:
    conn = init_db(args.db)
    ds_id = datasets.register_dataset(
        conn,
        args.name,
        classification=args.classification,
        export_policy=args.policy,
        modality=args.modality,
        owner=args.owner,
        description=args.description,
    )
    v_id = datasets.add_version(
        conn,
        ds_id,
        args.version,
        source=args.source,
        storage_path=args.storage_path,
        n_samples=args.n_samples,
        acquisition_batch=args.batch,
        notes=args.notes,
    )
    info = datasets.describe_version(conn, v_id)
    print(
        f"已登记数据集 #{ds_id} {args.name} @ {args.version}\n"
        f"  内容哈希：{info['version']['content_hash']}\n"
        f"  文件数：{info['version']['n_files']}  样本数：{args.n_samples if args.n_samples is not None else '—'}\n"
        f"  导出策略：{args.classification} / {args.policy}"
    )
    return 0


def cmd_dataset_list(args: argparse.Namespace) -> int:
    conn = init_db(args.db)
    rows = datasets.list_datasets(conn)
    if args.json:
        _print_json(rows)
        return 0
    if not rows:
        print("还没有登记任何数据集。")
        return 0
    _table(
        ["#", "名称", "类型", "导出策略", "modality", "版本数"],
        [[r["id"], r["name"], r["classification"], r["export_policy"], r["modality"], r["n_versions"]] for r in rows],
    )
    return 0


def cmd_dataset_show(args: argparse.Namespace) -> int:
    conn = init_db(args.db)
    info = datasets.describe_version(conn, args.version_id)
    if args.json:
        _print_json(info)
        return 0
    d, v = info["dataset"], info["version"]
    print(f"{d['name']} @ {v['version']}（版本 id={v['id']}）")
    print(f"  类型：{d['classification']}  导出策略：{d['export_policy']}  modality：{d['modality']}")
    print(f"  内容哈希：{v['content_hash']}")
    print(f"  文件数：{v['n_files']}  样本数：{v['n_samples'] if v['n_samples'] is not None else '—'}  批次：{v['acquisition_batch'] or '—'}")
    print(f"  存放位置：{v['storage_path'] or '—'}")
    if info["fields"]:
        _table(["字段", "类型", "角色", "单位"], [[f["name"], f["dtype"] or "", f["role"] or "", f["unit"] or ""] for f in info["fields"]])
    if info["splits"]:
        _table(["切分", "样本数", "seed", "哈希"], [[s["name"], s["n_samples"], s["seed"] or "", (s["split_hash"] or "")[:12]] for s in info["splits"]])
    return 0


def cmd_dataset_field(args: argparse.Namespace) -> int:
    conn = init_db(args.db)
    datasets.add_field(conn, args.version_id, args.name, dtype=args.dtype, role=args.role, unit=args.unit)
    print(f"已登记字段 {args.name}（role={args.role or '—'}）")
    return 0


def cmd_dataset_split(args: argparse.Namespace) -> int:
    conn = init_db(args.db)
    datasets.add_split(conn, args.version_id, args.name, args.n_samples, seed=args.seed, split_hash=args.split_hash)
    print(f"已登记切分 {args.name}：{args.n_samples} 条")
    return 0


def cmd_export_check(args: argparse.Namespace) -> int:
    conn = init_db(args.db)
    try:
        datasets.check_export(conn, args.version_id, args.target)
    except datasets.ExportDenied as exc:
        print(f"拒绝：{exc}")
        return 1
    print(f"允许导出 target={args.target}")
    return 0


def cmd_suite_add(args: argparse.Namespace) -> int:
    conn = init_db(args.db)
    suite_id = evals.create_suite(
        conn, args.name, dataset_version_id=args.dataset_version, task_type=args.task_type
    )
    print(f"已创建套件 #{suite_id} {args.name}")
    return 0


def cmd_suite_list(args: argparse.Namespace) -> int:
    conn = init_db(args.db)
    rows = evals.list_suites(conn)
    if args.json:
        _print_json(rows)
        return 0
    if not rows:
        print("还没有评测套件。")
        return 0
    _table(
        ["#", "套件", "数据版本", "任务类型", "run 数"],
        [[r["id"], r["name"], r["dataset_version_id"] or "—", r["task_type"] or "—", r["n_runs"]] for r in rows],
    )
    return 0


def cmd_run_ingest(args: argparse.Namespace) -> int:
    conn = init_db(args.db)
    ids = evals.ingest_file(
        conn, args.suite, args.file, label=args.label, label_prefix=args.label_prefix
    )
    for rid in ids:
        run = evals.get_run(conn, rid)
        print(f"已入库 run #{rid} {run['label']}：{run['n_passed']}/{run['n_cases']}")
    return 0


def cmd_run_list(args: argparse.Namespace) -> int:
    conn = init_db(args.db)
    rows = evals.list_runs(conn, args.suite)
    if args.json:
        _print_json(rows)
        return 0
    if not rows:
        print("还没有 run。")
        return 0
    _table(
        ["#", "套件", "Run", "臂", "模型", "通过", "通过率", "95% CI"],
        [
            [
                r["id"],
                r["suite_id"],
                r["label"],
                r["arm"] or "—",
                r["model"] or "—",
                f"{r['n_passed']}/{r['n_cases']}",
                f"{(r['n_passed'] / r['n_cases']):.1%}" if r["n_cases"] else "—",
                f"[{r['ci_low']:.1%}, {r['ci_high']:.1%}]" if r["ci_low"] is not None else "—",
            ]
            for r in rows
        ],
    )
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    conn = init_db(args.db)
    result = evals.compare_runs(conn, args.a, args.b)
    if args.json:
        _print_json(result)
        return 0
    a, b, t = result["a"], result["b"], result["table"]
    print(f"A = #{a['id']} {a['label']}：{a['k']}/{a['n']}（{a['rate']:.1%}）")
    print(f"B = #{b['id']} {b['label']}：{b['k']}/{b['n']}（{b['rate']:.1%}）")
    print(f"配对样本：{t['n_paired']}  仅 A 通过：{t['a_only']}  仅 B 通过：{t['b_only']}  都通过：{t['both']}  都未过：{t['neither']}")
    print(f"McNemar 精确 p = {result['mcnemar_p']:.6f} → {result['verdict']}")
    if result["by_task_class"]:
        _table(
            ["任务类", "A 通过率", "B 通过率"],
            [[cls, f"{s['a_rate']:.1%}（{s['a_k']}/{s['a_n']}）", f"{s['b_rate']:.1%}（{s['b_k']}/{s['b_n']}）"] for cls, s in sorted(result["by_task_class"].items())],
        )
    return 0


def cmd_gate(args: argparse.Namespace) -> int:
    conn = init_db(args.db)
    results = []
    if args.min_pass is not None:
        results.append(evals.gate_min_pass_rate(conn, args.run, args.min_pass))
    if args.baseline is not None:
        results.append(evals.gate_no_regression(conn, args.run, args.baseline, alpha=args.alpha))
    if args.require_tool:
        results.append(evals.gate_require_tool(conn, args.run, metric=args.require_tool, threshold=args.tool_threshold))
    if not results:
        print("没有指定任何门禁条件（--min-pass / --baseline / --require-tool）", file=sys.stderr)
        return 2
    failed = 0
    for r in results:
        mark = "通过" if r["passed"] else "未通过"
        print(f"[{mark}] {r['name']}：{r['detail']}")
        failed += 0 if r["passed"] else 1
    return 0 if failed == 0 else 1


def cmd_report(args: argparse.Namespace) -> int:
    conn = init_db(args.db)
    path = report.write_report(conn, args.out, title=args.title)
    print(f"看板已生成：{path}")
    return 0


# --------------------------------------------------------------------------- #
# 解析器
# --------------------------------------------------------------------------- #
def _db_parent() -> argparse.ArgumentParser:
    """让 --db 既能写在子命令前，也能写在子命令后。

    argparse 的坑：父解析器若用默认值，会把顶层已解析的 --db 覆盖回默认值。
    所以这里用 ``SUPPRESS``，只有在子命令后真正传了才写入 namespace。
    """
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--db", default=argparse.SUPPRESS, help="SQLite 库路径（可写在子命令后）")
    return p


_DB_PARENT = _db_parent()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="maldi-platform",
        description=f"maldi-ms 评测中台与数据中台（v{__version__}）",
    )
    p.add_argument("--db", default=DEFAULT_DB, help=f"SQLite 库路径（默认 {DEFAULT_DB}）")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="初始化数据库", parents=[_DB_PARENT]).set_defaults(func=cmd_init)

    # dataset
    d = sub.add_parser("dataset", help="数据中台操作")
    dsub = d.add_subparsers(dest="sub", required=True)

    da = dsub.add_parser("add", help="登记数据集与其首个版本", parents=[_DB_PARENT])
    da.add_argument("--name", required=True)
    da.add_argument("--version", default="v1")
    da.add_argument("--classification", default="synthetic", choices=list(datasets.CLASSIFICATIONS))
    da.add_argument("--policy", default="open", choices=list(datasets.EXPORT_POLICIES))
    da.add_argument("--modality", default="unknown")
    da.add_argument("--owner", default=None)
    da.add_argument("--description", default=None)
    da.add_argument("--source", default=None, help="本地文件/目录，用于计算内容哈希（只读）")
    da.add_argument("--storage-path", dest="storage_path", default=None)
    da.add_argument("--n-samples", dest="n_samples", type=int, default=None)
    da.add_argument("--batch", default=None, help="采集批次，用于批次效应追踪")
    da.add_argument("--notes", default=None)
    da.set_defaults(func=cmd_dataset_add)

    dl = dsub.add_parser("list", help="列出数据集", parents=[_DB_PARENT])
    dl.add_argument("--json", action="store_true")
    dl.set_defaults(func=cmd_dataset_list)

    ds = dsub.add_parser("show", help="查看某个数据版本", parents=[_DB_PARENT])
    ds.add_argument("--version-id", dest="version_id", type=int, required=True)
    ds.add_argument("--json", action="store_true")
    ds.set_defaults(func=cmd_dataset_show)

    df = dsub.add_parser("field", help="给数据版本登记字段", parents=[_DB_PARENT])
    df.add_argument("--version-id", dest="version_id", type=int, required=True)
    df.add_argument("--name", required=True)
    df.add_argument("--dtype", default=None)
    df.add_argument("--role", default=None, choices=["feature", "label", "id", "time", "meta"])
    df.add_argument("--unit", default=None)
    df.set_defaults(func=cmd_dataset_field)

    dsp = dsub.add_parser("split", help="给数据版本登记切分", parents=[_DB_PARENT])
    dsp.add_argument("--version-id", dest="version_id", type=int, required=True)
    dsp.add_argument("--name", required=True)
    dsp.add_argument("--n-samples", dest="n_samples", type=int, required=True)
    dsp.add_argument("--seed", type=int, default=None)
    dsp.add_argument("--split-hash", dest="split_hash", default=None)
    dsp.set_defaults(func=cmd_dataset_split)

    de = dsub.add_parser("export-check", help="检查某个版本能否以指定形式导出", parents=[_DB_PARENT])
    de.add_argument("--version-id", dest="version_id", type=int, required=True)
    de.add_argument("--target", default="metadata", choices=list(datasets.EXPORT_TARGETS))
    de.set_defaults(func=cmd_export_check)

    # suite
    s = sub.add_parser("suite", help="评测套件")
    ssub = s.add_subparsers(dest="sub", required=True)
    sa = ssub.add_parser("add", help="新建套件", parents=[_DB_PARENT])
    sa.add_argument("--name", required=True)
    sa.add_argument("--dataset-version", dest="dataset_version", type=int, default=None)
    sa.add_argument("--task-type", dest="task_type", default=None)
    sa.set_defaults(func=cmd_suite_add)
    sl = ssub.add_parser("list", help="列出套件", parents=[_DB_PARENT])
    sl.add_argument("--json", action="store_true")
    sl.set_defaults(func=cmd_suite_list)

    # run
    r = sub.add_parser("run", help="评测运行")
    rsub = r.add_subparsers(dest="sub", required=True)
    ri = rsub.add_parser("ingest", help="把评测结果 JSON 入库（自动识别平台格式 / agent 报告格式）", parents=[_DB_PARENT])
    ri.add_argument("--suite", type=int, required=True)
    ri.add_argument("--file", required=True)
    ri.add_argument("--label", default=None)
    ri.add_argument("--label-prefix", dest="label_prefix", default=None)
    ri.set_defaults(func=cmd_run_ingest)
    rl = rsub.add_parser("list", help="列出运行", parents=[_DB_PARENT])
    rl.add_argument("--suite", type=int, default=None)
    rl.add_argument("--json", action="store_true")
    rl.set_defaults(func=cmd_run_list)

    c = sub.add_parser("compare", help="配对比较两个 run（McNemar 精确检验）", parents=[_DB_PARENT])
    c.add_argument("--a", type=int, required=True)
    c.add_argument("--b", type=int, required=True)
    c.add_argument("--json", action="store_true")
    c.set_defaults(func=cmd_compare)

    g = sub.add_parser("gate", help="对某个 run 跑门禁（未通过时退出码 1）", parents=[_DB_PARENT])
    g.add_argument("--run", type=int, required=True)
    g.add_argument("--min-pass", dest="min_pass", type=float, default=None)
    g.add_argument("--baseline", type=int, default=None)
    g.add_argument("--alpha", type=float, default=0.05)
    g.add_argument("--require-tool", dest="require_tool", default=None, help="如 perm_called_rate")
    g.add_argument("--tool-threshold", dest="tool_threshold", type=float, default=1.0)
    g.set_defaults(func=cmd_gate)

    rp = sub.add_parser("report", help="生成静态看板 HTML", parents=[_DB_PARENT])
    rp.add_argument("--out", default="reports/dashboard.html")
    rp.add_argument("--title", default=None)
    rp.set_defaults(func=cmd_report)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (datasets.DataError, evals.EvalError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
