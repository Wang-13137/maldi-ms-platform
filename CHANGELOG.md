# 变更日志

本文件记录所有对外可见的变更。格式参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

**本项目额外遵循一条规则**：schema 版本号（`db.SCHEMA_VERSION`）与包版本号独立演进。
包版本升级不一定动 schema；但 schema 一动，包版本必须升次版本号或以上，并在下方
「Schema」小节写明迁移方向。

## [Unreleased]

## [0.2.0] - 2026-11-05

这一版把项目从"能跑的原型"补成"能交付的工程"：加了质量门禁、审计、schema 迁移与发布流程，
业务逻辑（指标重算、配对检验、门禁判据）未改变。

### Added

- **审计日志**（`audit.py` + `audit_log` 表）：记录 `dataset.add` / `dataset.export` /
  `run.ingest` / `gate.evaluate` 四类动作的操作者、目标、结论（ok / denied / failed）。
  库层用触发器禁止 `UPDATE` / `DELETE`，只追加不修改；被拒绝的导出同样留痕。
- **schema 版本迁移**（`db.SCHEMA_VERSION = 2`）：`init` 自动把旧库升到当前版本，
  迁移脚本只增不改；库比程序新时拒绝启动（退出码 3），避免用旧程序写坏新库。
- **新命令**：`db check`（只读体检，库版本不一致时退出码 1）、`audit list`（可按动作与结论过滤）。
- **全局 `--actor`**：操作者默认取环境变量 `MALDI_PLATFORM_ACTOR`，否则取系统用户。
- **质量门禁**：ruff（E/F/W/I/N/UP/B/C4/SIM/RUF）+ mypy + 分支覆盖率 85% 下限，
  三者都进 CI；`py.typed` 标记随包分发。
- **工程治理文件**：`CONTRIBUTING.md`、`SECURITY.md`、`CHANGELOG.md`、
  `docs/engineering.md`、`docs/adr/`（5 篇架构决策记录）、Issue / PR 模板、Dependabot。
- **发布流程**：`.github/workflows/release.yml`，打 `v*` 标签时构建 sdist + wheel 并附到 Release。

### Changed

- 测试从 90 项增至 111 项，覆盖率（分支）90.2%。
- `ExportDenied` 更名为 `ExportDeniedError`，与 Python 异常命名惯例一致。
- 全仓按 ruff format 统一格式；类型注解统一为 PEP 604 写法（`str | None`）。
- 修掉 8 处 `int(cur.lastrowid)` 的 `int | None` 类型漏洞——运行期不会出错，
  但类型检查能查出"插入失败时可能拿到 None"这条路径，顺手堵掉。

### Schema

- v1 → v2：新增 `audit_log` 表与两个只追加触发器。`init` 时自动迁移，历史数据不动。

## [0.1.0] - 2026-11-04

首个可用版本：评测中台 + 数据中台，零第三方运行时依赖。

### Added

- 数据中台：数据集 / 版本 / 字段 / 切分登记；内容哈希作为版本身份（排序后的
  `sha256(路径 + 内容)`，跨目录可复现、内容变则哈希变）；导出门禁
  （`real` 数据的 payload 一律拒绝，`real + open` 在登记阶段即报错）；
  `dataset_version → run` 血缘。
- 评测中台：套件绑定数据版本；结果入库自动识别三种格式（平台原生 JSON、
  `maldi-ms-agent` 报告一文件拆多臂、`medical-rag` 检索基准一文件一臂）；
  指标从逐题 `cases` 重算（通过率 + Wilson 95% 区间，检索类另算
  `answer_recall_mean` / `mrr_mean`）；`compare` 按 `case_id` 配对 + McNemar 精确检验；
  三类门禁（通过率下限 / 相对基线不得显著回归 / 必须调用指定工具），未通过退出码 1。
- 静态看板：自包含 HTML（内联 CSS + SVG），零 JS、零外链，可离线打开。
- 真实数据验收：`maldi-ms-agent` 64 条四臂复现 11/16、11/16、0/16、11/16（A vs C p=0.000977）；
  `medical-rag` 三臂 65/85、77/85、79/85（向量 vs 句级精排 p=0.000122，
  精排 vs 句级精排 p=0.625 不显著）。
- 90 项测试，GitHub Actions 三平台矩阵（3.10 / 3.11 / 3.12）+ 命令行冒烟。

[Unreleased]: https://github.com/Wang-13137/maldi-ms-platform/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/Wang-13137/maldi-ms-platform/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Wang-13137/maldi-ms-platform/releases/tag/v0.1.0
