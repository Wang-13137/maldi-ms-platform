# maldi-ms-platform

MALDI-TOF 质谱算法项目的**评测中台**与**数据中台**。零第三方依赖，一个 SQLite 文件就是全部状态。

> 这不是"平台化"演示。它解决的是我在三个算法项目里真实踩到的四个问题：数据版本说不清、
> 评测口径散的、小样本比较用错统计、以及"这一版到底比上一版好还是坏"只能靠感觉。

- 测试：**90 项**
- 依赖：Python ≥ 3.10，**无第三方运行时依赖**（dev 只装 pytest）
- 形态：CLI + 静态看板（HTML 单文件，可离线打开）

---

## 一、两个中台各做什么

### 数据中台

| 能力 | 命令 | 说明 |
|---|---|---|
| 数据集登记 | `dataset add` | 名字、模态、分类（synthetic/real）、导出策略、负责人 |
| 内容哈希 | 自动 | 目录内按相对路径排序逐文件 sha256，再对清单取哈希；换目录、换拷贝顺序哈希不变 |
| 版本管理 | `dataset add --version` | 同版本不可重复登记（不允许"版本号相同、内容不同"） |
| 字段与切分 | `dataset field` / `dataset split` | role 区分 feature/label/id/time/meta，切分记 seed 与哈希 |
| 导出门禁 | `dataset export-check` | **真实数据 payload 一律拒绝**，只放行元数据 |
| 血缘 | 自动 | 评测运行引用数据版本时留边，能回答"这个结论出于哪份数据" |

### 评测中台

| 能力 | 命令 | 说明 |
|---|---|---|
| 套件 | `suite add` | 绑定数据版本与任务类型 |
| 结果入库 | `run ingest` | 自动识别三种格式：平台原生 JSON、`maldi-ms-agent` 报告（一文件拆多臂）、`medical-rag` 检索基准（一文件一臂）；只有聚合结论、没有逐题明细的报告一律拒收 |
| 指标 | 自动 | 通过率 + Wilson 95% 区间；工具调用率；检索类再重算 `answer_recall_mean` / `mrr_mean` |
| 配对比较 | `compare` | 按 case_id 配对，McNemar 精确检验，附分任务类明细 |
| 门禁 | `gate` | 通过率下限、**相对基线不得显著回归**、必须调用某工具；未通过退出码 1 |
| 看板 | `report` | 自包含 HTML，内联 CSS + SVG，无外部资源 |

---

## 二、快速开始

```bash
pip install -e ".[dev]"

# 1. 数据中台：登记一个数据集（--source 只读，不复制数据）
maldi-platform dataset add \
  --db demo.db --name synth-uti --version v1 \
  --classification synthetic --policy open \
  --modality maldi-spectrum --n-samples 40 --batch B1 \
  --source ./examples/data

# 2. 评测中台：建套件 + 入库结果
maldi-platform suite add --db demo.db --name m9-agent --dataset-version 1
maldi-platform run ingest --db demo.db --suite 1 --file examples/demo_report.json --label-prefix m9

# 3. 配对比较（同批样本上的两个消融臂）
maldi-platform compare --db demo.db --a 1 --b 3

# 4. 门禁（未通过返回退出码 1，可直接当 CI 步骤）
maldi-platform gate --db demo.db --run 1 --min-pass 0.6 --require-tool perm_called_rate
maldi-platform gate --db demo.db --run 3 --baseline 1

# 5. 看板
maldi-platform report --db demo.db --out reports/dashboard.html
```

真实产物可以直接吃进去：`maldi-ms-agent` 的多 seed 报告（4 组消融 × 4 类任务 × 4 seed = 64 条）
入库后会拆成 4 个 run，通过率分别复现为 `11/16`、`11/16`、`0/16`、`11/16`，
A 与 C 的配对 McNemar `p = 0.000977`。

---

## 三、五个关键设计决定

### 1. 只登记元数据，真实数据没有"导出"这个选项

真实受试者数据（`classification='real'`）在库里只存：名字、样本数、采集批次、内容哈希、
存放指针。**payload 导出被硬拒**，而且不是靠策略配置——`real + open` 在登记阶段就报错，
`check_export(..., 'payload')` 直接抛 `ExportDenied`。原因很简单：只要留了一个"配置一下就能导出"
的口子，迟早有人配置它。

### 2. 内容哈希是版本的主键

版本号是人起的，会撞、会改、会被复用。哈希不会。所以同一版本号不允许重复登记，
换个目录拷贝同一份数据哈希必须一致（测试里就是这么断言的）。这样"这份结果用的是哪份数据"
才是可验证的，而不是可回忆的。

### 3. 比较必须配对，不能用两个独立比率

同一次评测的不同消融臂跑的是**同一批样本**。拿 `11/16` 和 `0/16` 各算一个比率再比大小，
在 n=16 上会把样本难度当成方法差异。中台强制按 `case_id` 配对，用 McNemar 精确检验：

- 只用不一致格（A 过 B 没过、B 过 A 没过），一致性高的样本对 p 值贡献为零；
- 小样本用**精确**二项而不是卡方近似——`b+c=11` 时近似会把 p 报小一个数量级；
- 两次 run 样本不完全一致时只比交集，缺失的 case 不当失败。

### 4. 拒答题不进通过率

`medical-rag` 的检索报告给了 15 条应拒答题，但报告里的拒答阈值是**用同一批数据按
Youden J 后验挑出来的最优阈值**，而且每个臂的阈值不同。拿它给自己的拒答打分是
循环论证，跨臂比较还会把"检索质量"和"阈值调参"混成一个差值。所以默认不计入，
只记 `unanswerable_top1_mean` 作观测；确实要算时用 `--include-unanswerable` 显式声明，
`config.note` 里会留下这段理由。

### 5. 回归门禁看显著性，不看小数点

"通过率掉了 6% 就算回归"在小样本上必然误报：n=16 时掉 1 条就是 6.25%。
`gate_no_regression` 只在候选**显著劣于**基线（丢的样本显著多于赢的）时才拦，
其余一律放行。宁可漏报一次退步，也不要因为噪声把正常的迭代卡死。

---

## 四、数据模型

```
datasets ──< dataset_versions ──< dataset_fields
                    │            └< dataset_splits
                    │
                    └──< lineage >── runs ──< cases
                                      ├──< metrics
                                      └──< gates
suites ──< runs
```

9 张表，全部在 `src/maldi_platform/db.py` 里，schema 版本记在 `meta` 表。
没有 ORM：SQL 就在代码里，读起来就是数据长什么样。

---

## 五、和三个算法项目的关系

| 项目 | 中台接什么 |
|---|---|
| `maldi-ms-pipeline` | 数据集版本、内容哈希、采集批次、特征字段 |
| `maldi-ms-agent` | 评测套件、4 组消融臂、64 条实测结果、门禁 |
| `medical-rag` | 检索三臂（纯向量 / 精排 / 句级精排）逐题入库、配对检验、回归门禁 |

两个项目都接过一次，验证了中台不是只对某一种报告格式成立：

```
$ maldi-platform run ingest --db rag.db --suite 1 --file benchmark_eval_vector.json
已入库 run #1 纯向量：65/85
$ maldi-platform compare --db rag.db --a 1 --b 3
配对样本：85  仅 A 通过：0  仅 B 通过：14  都通过：65  都未过：6
McNemar 精确 p = 0.000122 → 差异显著
```

三个检索臂的通过率（65/85、77/85、79/85）与报告自报的 `full_coverage`
（76.5%、90.6%、92.9%）**逐位一致**——"指标从 cases 重算"这条设计没有误差。
而 `精排 vs 句级精排` 的配对检验 p=0.625，即句级精排那 +2 条在 85 条样本上
**不足以称为提升**，这与"看小数点"的直觉相反，正是配对检验要解决的问题。

---

## 六、明确没做的

- **没有 Web 服务 / 多用户 / 权限体系**：CLI + 静态页，够我一个人的三个项目用，多租户是另一个量级的事；
- **没有前端框架**：看板是服务端渲染的 HTML，零 JS，能发给人、能进仓库、能离线打开；
- **没有实验调度**：不负责启动作业，只负责"结果进来之后怎么被可信地记录和比较"；
- **没有自动拉取模型 API**：评测跑在项目侧，中台不碰外部服务，也就不会把数据带出去。

---

## 七、测试

```bash
pytest -q          # 90 passed
```

- `test_stats.py`（15）：Wilson 区间边界、McNemar 精确值（`11 vs 0 → 2/2^11`）、配对表只算交集；
- `test_datasets.py`（25）：哈希跨目录可复现、内容变则哈希变、真实数据 payload 必拒；
- `test_evals.py`（26）：agent 报告拆臂、配对比较、回归门禁拦 / 放行 / 容忍噪声；
- `test_report.py`（6）：看板含真实事实、HTML 转义、无外部资源；
- `test_cli.py`（9）：端到端命令流、门禁失败反映到退出码；
- `test_medical_rag_adapter.py`（9）：检索基准入库、自报值被忽略、拒答题默认排除、三臂配对与回归门禁。

CI：GitHub Actions，3.10 / 3.11 / 3.12 矩阵。

## 八、许可

MIT。示例数据全部为合成数据；真实队列数据不在本仓库，也不随库分发。
