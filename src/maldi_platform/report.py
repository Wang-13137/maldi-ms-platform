"""静态看板：把库里的事实渲染成一张自包含 HTML。

为什么不用前端框架：这张页面要能直接发给别人、丢进仓库、离线打开。所以零外部资源、
零 CDN、零 JS 依赖——所有的图都是内联 SVG，所有的样式都在 <style> 里。
"""
from __future__ import annotations

import html
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import evals
from .datasets import list_datasets, list_versions

_CSS = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { margin: 0; padding: 32px 24px 64px; background: #f6f7f9; color: #1a1d21;
  font-family: -apple-system, "Segoe UI", "Microsoft YaHei", "PingFang SC", sans-serif;
  font-size: 14px; line-height: 1.6; }
.wrap { max-width: 1120px; margin: 0 auto; }
h1 { font-size: 22px; margin: 0 0 4px; letter-spacing: .2px; }
h2 { font-size: 17px; margin: 36px 0 12px; padding-bottom: 8px; border-bottom: 1px solid #e3e6ea; }
h3 { font-size: 14px; margin: 20px 0 8px; color: #41474f; }
.sub { color: #6b7280; font-size: 13px; margin: 0 0 20px; }
.cards { display: flex; gap: 12px; flex-wrap: wrap; margin: 18px 0 4px; }
.card { background: #fff; border: 1px solid #e3e6ea; border-radius: 8px; padding: 12px 16px; min-width: 132px; }
.card .k { color: #6b7280; font-size: 12px; }
.card .v { font-size: 22px; font-weight: 600; font-variant-numeric: tabular-nums; }
table { width: 100%; border-collapse: collapse; background: #fff; border: 1px solid #e3e6ea;
  border-radius: 8px; overflow: hidden; font-size: 13px; }
th, td { padding: 9px 12px; text-align: left; border-bottom: 1px solid #eef0f3; vertical-align: middle; }
th { background: #fafbfc; font-weight: 600; color: #41474f; font-size: 12px; white-space: nowrap; }
tr:last-child td { border-bottom: none; }
td.num { font-variant-numeric: tabular-nums; white-space: nowrap; }
.mono { font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace; font-size: 12px; }
.badge { display: inline-block; padding: 1px 7px; border-radius: 999px; font-size: 11px; font-weight: 600; }
.b-open { background: #e7f5ec; color: #1c7a41; }
.b-syn { background: #e8f1fd; color: #1f5fa8; }
.b-real { background: #fdeceb; color: #a8322a; }
.b-internal { background: #fff4e0; color: #92610a; }
.b-restricted { background: #fdeceb; color: #a8322a; }
.pass { color: #1c7a41; font-weight: 600; }
.fail { color: #a8322a; font-weight: 600; }
.bar { display: inline-block; vertical-align: middle; width: 96px; height: 8px; background: #eef0f3;
  border-radius: 4px; overflow: hidden; margin-right: 8px; }
.bar > span { display: block; height: 100%; background: #2f6fd0; }
.note { color: #6b7280; font-size: 12px; margin-top: 8px; }
.empty { color: #9aa1ab; font-style: italic; }
footer { margin-top: 44px; color: #9aa1ab; font-size: 12px; }
"""


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _bar(rate: float, width: int = 96) -> str:
    pct = max(0.0, min(1.0, rate))
    return (
        f'<span class="bar" style="width:{width}px"><span style="width:{pct * 100:.1f}%"></span></span>'
    )


def _class_badge(classification: str) -> str:
    cls = "b-real" if classification == "real" else "b-syn"
    label = "真实数据·仅元数据" if classification == "real" else "合成数据"
    return f'<span class="badge {cls}">{label}</span>'


def _policy_badge(policy: str) -> str:
    mapping = {"open": ("b-open", "open"), "internal": ("b-internal", "internal"), "restricted": ("b-restricted", "restricted")}
    cls, label = mapping.get(policy, ("b-internal", policy))
    return f'<span class="badge {cls}">{_esc(label)}</span>'


# --------------------------------------------------------------------------- #
# 各区块
# --------------------------------------------------------------------------- #
def _section_datasets(conn: sqlite3.Connection) -> str:
    rows = list_datasets(conn)
    if not rows:
        return '<p class="empty">还没有登记任何数据集。</p>'

    parts: List[str] = []
    for d in rows:
        versions = list_versions(conn, d["id"])
        v_rows = "".join(
            "<tr><td class='mono'>{v}</td><td class='num'>{n_files}</td><td class='num'>{n}</td>"
            "<td>{batch}</td><td class='mono'>{h}…</td><td class='mono'>{path}</td></tr>".format(
                v=_esc(v["version"]),
                n_files=v["n_files"],
                n=_esc(v["n_samples"] if v["n_samples"] is not None else "—"),
                batch=_esc(v["acquisition_batch"] or "—"),
                h=_esc(v["content_hash"][:16]),
                path=_esc(v["storage_path"] or "—"),
            )
            for v in versions
        )
        v_table = (
            "<table><thead><tr><th>版本</th><th>文件数</th><th>样本数</th><th>采集批次</th>"
            "<th>内容哈希</th><th>存放位置（指针）</th></tr></thead><tbody>"
            + (v_rows or "<tr><td colspan='6' class='empty'>暂无版本</td></tr>")
            + "</tbody></table>"
        )
        parts.append(
            f"<h3>{_esc(d['name'])} {_class_badge(d['classification'])} {_policy_badge(d['export_policy'])}"
            f" <span class='note'>modality={_esc(d['modality'])} · {len(versions)} 个版本</span></h3>{v_table}"
        )
    return "".join(parts)


def _suite_runs(conn: sqlite3.Connection, suite_id: int) -> List[Dict[str, Any]]:
    return evals.list_runs(conn, suite_id)


def _section_evals(conn: sqlite3.Connection) -> str:
    suites = evals.list_suites(conn)
    if not suites:
        return '<p class="empty">还没有评测套件。</p>'

    blocks: List[str] = []
    for s in suites:
        runs = _suite_runs(conn, s["id"])
        if runs:
            body = "".join(
                "<tr><td class='num'>{id}</td><td>{label}</td><td class='mono'>{arm}</td>"
                "<td>{model}</td><td class='num'>{k}/{n}</td>"
                "<td class='num'>{bar}{rate:.1%}</td><td class='num'>[{lo:.1%}, {hi:.1%}]</td>"
                "<td class='mono'>{commit}</td></tr>".format(
                    id=r["id"],
                    label=_esc(r["label"]),
                    arm=_esc(r["arm"] or "—"),
                    model=_esc(r["model"] or "—"),
                    k=r["n_passed"],
                    n=r["n_cases"],
                    bar=_bar(r["n_passed"] / r["n_cases"] if r["n_cases"] else 0.0),
                    rate=(r["n_passed"] / r["n_cases"]) if r["n_cases"] else 0.0,
                    lo=r["ci_low"] if r["ci_low"] is not None else 0.0,
                    hi=r["ci_high"] if r["ci_high"] is not None else 0.0,
                    commit=_esc((r["git_commit"] or "—")[:10]),
                )
                for r in runs
            )
            table = (
                "<table><thead><tr><th>#</th><th>Run</th><th>臂</th><th>模型</th><th>通过</th>"
                "<th>通过率</th><th>95% Wilson CI</th><th>commit</th></tr></thead><tbody>"
                + body
                + "</tbody></table>"
            )
        else:
            table = '<p class="empty">该套件还没有 run。</p>'

        blocks.append(
            f"<h3>{_esc(s['name'])} <span class='note'>task_type={_esc(s['task_type'] or '—')} · "
            f"{len(runs)} 个 run</span></h3>{table}"
        )

        # 以第一个 run 为基线，其余逐个做配对比较
        if len(runs) >= 2:
            baseline = runs[0]
            cmp_rows = []
            for cand in runs[1:]:
                try:
                    cmp = evals.compare_runs(conn, baseline["id"], cand["id"])
                except Exception as exc:  # noqa: BLE001 - 报告不该因为一个比较失败而整体失败
                    cmp_rows.append(
                        f"<tr><td>{_esc(cand['label'])}</td><td colspan='4' class='empty'>{_esc(exc)}</td></tr>"
                    )
                    continue
                t = cmp["table"]
                v_cls = "pass" if cmp["mcnemar_p"] < 0.05 else ""
                cmp_rows.append(
                    "<tr><td>{cand}</td><td class='num'>{a_only}</td><td class='num'>{b_only}</td>"
                    "<td class='num'>{p:.4f}</td><td class='{cls}'>{v}</td></tr>".format(
                        cand=_esc(cand["label"]),
                        a_only=t["a_only"],
                        b_only=t["b_only"],
                        p=cmp["mcnemar_p"],
                        cls=v_cls,
                        v=_esc(cmp["verdict"]),
                    )
                )
            blocks.append(
                f"<h3 class='note'>与基线「{_esc(baseline['label'])}」的配对比较（McNemar 精确检验）</h3>"
                "<table><thead><tr><th>候选</th><th>仅基线通过</th><th>仅候选通过</th>"
                "<th>p</th><th>结论</th></tr></thead><tbody>"
                + "".join(cmp_rows)
                + "</tbody></table>"
            )
    return "".join(blocks)


def _section_gates(conn: sqlite3.Connection) -> str:
    rows = evals.list_gates(conn)
    if not rows:
        return '<p class="empty">还没有门禁记录。</p>'
    body = []
    for g in rows:
        mark = "<span class='pass'>通过</span>" if g["passed"] else "<span class='fail'>未通过</span>"
        body.append(
            f"<tr><td class='num'>{g['run_id']}</td><td class='mono'>{_esc(g['name'])}</td>"
            f"<td>{mark}</td><td>{_esc(g['detail'])}</td></tr>"
        )
    return (
        "<table><thead><tr><th>run</th><th>门禁</th><th>结果</th><th>细节</th></tr></thead><tbody>"
        + "".join(body)
        + "</tbody></table>"
    )


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def build_html(conn: sqlite3.Connection, *, title: str = "maldi-ms 评测与数据中台") -> str:
    from .db import now_iso

    n_datasets = len(list_datasets(conn))
    n_versions = conn.execute("SELECT COUNT(*) AS c FROM dataset_versions").fetchone()["c"]
    n_runs = conn.execute("SELECT COUNT(*) AS c FROM runs").fetchone()["c"]
    n_gates = conn.execute("SELECT COUNT(*) AS c FROM gates").fetchone()["c"]
    n_gate_fail = conn.execute("SELECT COUNT(*) AS c FROM gates WHERE passed = 0").fetchone()["c"]

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)}</title>
<style>{_CSS}</style>
</head>
<body>
<div class="wrap">
  <h1>{_esc(title)}</h1>
  <p class="sub">生成时间 {_esc(now_iso())} · 数据来源：本地 SQLite（只读渲染）</p>

  <div class="cards">
    <div class="card"><div class="k">数据集</div><div class="v">{n_datasets}</div></div>
    <div class="card"><div class="k">数据版本</div><div class="v">{n_versions}</div></div>
    <div class="card"><div class="k">评测运行</div><div class="v">{n_runs}</div></div>
    <div class="card"><div class="k">门禁记录</div><div class="v">{n_gates}</div></div>
    <div class="card"><div class="k">门禁未通过</div><div class="v" style="color:{'#a8322a' if n_gate_fail else '#1c7a41'}">{n_gate_fail}</div></div>
  </div>

  <h2>数据中台</h2>
  {_section_datasets(conn)}

  <h2>评测中台</h2>
  {_section_evals(conn)}

  <h2>门禁</h2>
  {_section_gates(conn)}

  <p class="note">说明：真实数据（real）在库里只存元数据与存放指针，原始谱与特征矩阵不随库分发；
  置信区间为 Wilson 区间，比较用配对 McNemar 精确检验。</p>
  <footer>maldi-ms-platform · 静态看板（无外部依赖，可离线打开）</footer>
</div>
</body>
</html>
"""


def write_report(conn: sqlite3.Connection, out_path: str | Path, *, title: Optional[str] = None) -> Path:
    p = Path(out_path)
    if p.parent and not p.parent.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(build_html(conn, title=title or "maldi-ms 评测与数据中台"), encoding="utf-8")
    return p


__all__ = ["build_html", "write_report"]
