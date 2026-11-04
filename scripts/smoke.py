#!/usr/bin/env python
"""端到端冒烟：从使用者视角把关键契约跑一遍，逐条断言退出码与输出。

单元测试保证函数对，这个脚本保证**命令行契约**对：比较要报显著、门禁失败要返回 1、
真实数据 payload 要导不出、只有聚合结论的报告要被拒收、审计要留痕。

用法：``python scripts/smoke.py``（无需先安装，脚本自己加 src 到 sys.path）
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

_passed = 0
_failed: list[str] = []


def cli(*args: str, cwd: Path | None = None) -> tuple[int, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-m", "maldi_platform", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(cwd or ROOT),
        env=env,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def check(name: str, cond: bool, detail: str = "") -> None:
    global _passed
    if cond:
        _passed += 1
        print(f"[通过] {name}")
    else:
        _failed.append(name)
        print(f"[失败] {name} {detail}")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        db = str(work / "smoke.db")
        examples = ROOT / "examples"

        code, out = cli("init", "--db", db)
        check("初始化数据库", code == 0, out)

        code, out = cli("db", "check", "--db", db)
        check("schema 体检一致", code == 0 and "一致" in out, out)

        code, out = cli(
            "dataset", "add", "--db", db, "--name", "synth-uti", "--version", "v1",
            "--classification", "synthetic", "--policy", "open",
            "--source", str(examples / "data"), "--n-samples", "40", "--batch", "B1",
        )  # fmt: skip
        check("登记合成数据集", code == 0 and "内容哈希" in out, out)

        code, out = cli("suite", "add", "--db", db, "--name", "m9-agent", "--dataset-version", "1")
        check("创建评测套件", code == 0, out)

        code, out = cli(
            "run", "ingest", "--db", db, "--suite", "1", "--file", str(examples / "demo_report.json")
        )
        check("入库多臂报告（自动拆臂）", code == 0 and out.count("已入库") == 3, out)

        code, out = cli("compare", "--db", db, "--a", "1", "--b", "3")
        check("配对比较报显著", code == 0 and "差异显著" in out, out)

        code, out = cli("gate", "--db", db, "--run", "1", "--min-pass", "0.6")
        check("通过率门禁放行", code == 0 and "通过" in out, out)

        code, out = cli("gate", "--db", db, "--run", "3", "--baseline", "1")
        check("回归门禁拦住并返回 1", code == 1 and "未通过" in out, out)

        # 真实数据 + 受限策略：payload 必须导不出
        code, out = cli(
            "dataset", "add", "--db", db, "--name", "real-uti", "--version", "v1",
            "--classification", "real", "--policy", "restricted", "--source", str(examples / "data"),
        )  # fmt: skip
        check("登记真实数据集（只登记元数据）", code == 0, out)

        code, out = cli("dataset", "export-check", "--db", db, "--version-id", "2", "--target", "payload")
        check("真实数据 payload 导出被拒", code == 1 and "拒绝" in out, out)

        code, out = cli("dataset", "export-check", "--db", db, "--version-id", "2", "--target", "metadata")
        check("真实数据 metadata 可导出", code == 0, out)

        code, out = cli(
            "dataset", "add", "--db", db, "--name", "bad-real", "--version", "v1",
            "--classification", "real", "--policy", "open",
        )  # fmt: skip
        check("real + open 在登记阶段即报错", code == 1, out)

        # 第二个项目的同构接入
        cli("suite", "add", "--db", db, "--name", "rag-retrieval", "--dataset-version", "1")
        cli("run", "ingest", "--db", db, "--suite", "2", "--file", str(examples / "rag_vector.json"))
        cli("run", "ingest", "--db", db, "--suite", "2", "--file", str(examples / "rag_rerank.json"))
        code, out = cli("run", "list", "--db", db, "--suite", "2", "--json")
        runs = json.loads(out[out.index("[") :]) if code == 0 else []
        check("检索基准两臂入库", len(runs) == 2, out)

        if len(runs) == 2:
            code, out = cli("compare", "--db", db, "--a", str(runs[1]["id"]), "--b", str(runs[0]["id"]))
            check("检索臂配对比较", code == 0 and "显著" in out, out)

        agg = work / "agg.json"
        agg.write_text('{"left":"a","right":"b","n_pairs":10,"scopes":{}}', encoding="utf-8")
        code, out = cli("run", "ingest", "--db", db, "--suite", "2", "--file", str(agg))
        check("只有聚合结论的报告被拒收", code == 1 and "逐题明细" in out, out)

        code, out = cli("report", "--db", db, "--out", str(work / "dashboard.html"))
        html = (
            (work / "dashboard.html").read_text(encoding="utf-8")
            if (work / "dashboard.html").exists()
            else ""
        )
        check("看板生成且自包含", code == 0 and "评测中台" in html and "https://" not in html, out)

        code, out = cli("audit", "list", "--db", db, "--outcome", "denied")
        check("被拒的导出留有审计", code == 0 and "dataset.export" in out, out)

        code, out = cli("audit", "list", "--db", db, "--action", "gate.evaluate", "--json")
        check("门禁结论留有审计", code == 0 and "failed" in out, out)

    total = _passed + len(_failed)
    print(f"\nsmoke: {_passed}/{total} 通过")
    if _failed:
        print("失败项：" + "、".join(_failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
