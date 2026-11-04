# 参与贡献

这个项目服务于"评测结论必须可复现、可审计"这一个目标，所以对改动的要求比一般工具库更严：
**任何会影响指标数值或门禁判据的改动，都属于破坏性变更**，必须走下面的流程。

## 环境

```bash
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pre-commit install        # 可选，但提交前跑一遍能省一次 CI 往返
```

要求 Python ≥ 3.10。运行时零第三方依赖，这是硬约束——新增任何 `dependencies`
都需要在 PR 里说明为什么标准库做不到。

## 提交前必须全绿

```bash
make check      # ruff + mypy + pytest(含覆盖率门槛)
```

等价的原始命令：

```bash
ruff check src tests
ruff format --check src tests
mypy src
pytest -q --cov=maldi_platform --cov-report=term
```

覆盖率是**分支覆盖**，下限 85%（当前 90.2%）。降覆盖率的 PR 需要说明哪几行无法被测到、
为什么。

## 提交信息

采用 [Conventional Commits](https://www.conventionalcommits.org/zh-hans/)：

```
feat(eval): 支持按任务类分别跑门禁
fix(ingest): 同一臂重名时报错而不是覆盖
docs(adr): 补 ADR-0005 拒答题为什么不计通过率
test(audit): 补被拒导出的留痕断言
refactor(db): 迁移脚本改为按版本号顺序执行
```

`feat` / `fix` 之外的类型不会进 CHANGELOG。

## 改代码时的四条硬约束

1. **不许把自报指标当结果**。所有指标必须从逐题 `cases` 重算；报告里给的
   `overall` / `accuracy` 只能用于交叉核对，不能直接入库。
2. **不许绕过导出门禁**。`real` 数据的 payload 导出必须在库层被拒，任何"临时放行"
   的开关都不接受，包括环境变量和调试参数。
3. **不许改历史迁移脚本**。`db._MIGRATIONS` 里已发布的条目只增不改——同一个库在不同
   机器上必须演化成同一种结构。要改结构就加新版本号。
4. **不许静默降级**。审计写不进去、schema 不兼容、报告缺逐题明细，都必须报错退出，
   不能"尽量继续"。

## 新增一种报告格式（最常见的一类贡献）

1. 在 `evals.py` 加一个 `ingest_xxx()`，输入是已经 `json.load` 过的对象；
2. 在 `ingest_file()` 里按**结构特征**（而不是文件名）分派；
3. 失败要抛 `EvalError`，并在信息里说清缺哪个字段——调用方多数是 CI，报错信息就是日志；
4. 在 `tests/helpers.py` 加一个构造器，在 `tests/` 里覆盖三种情形：
   正常入库、自报值被忽略、缺逐题明细被拒收；
5. 同步 README 的"支持的报告格式"表与 `CHANGELOG.md`。

## PR 检查清单

- [ ] `make check` 本地全绿
- [ ] 新行为有测试；修 bug 的 PR 带一个能复现的用例
- [ ] 改动涉及指标口径时，PR 描述里写明"哪个数字会变、为什么应该变"
- [ ] 涉及 schema 的改动：`SCHEMA_VERSION` +1、写了迁移脚本、README/CHANGELOG 同步
- [ ] 没有引入真实受试者数据（含截图、测试夹具、示例文件）

## 报告问题

功能缺陷请用 Issue 模板；安全问题与数据泄露按 [SECURITY.md](SECURITY.md) 私下报告，
不要开公开 Issue。
