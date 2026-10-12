---
kind: changelog
lang: zh
---

# 版本记录

## 迁移文件整理 — 2026-10-12

将 schema.sql 移入 src/seal/migrations/，保持其内容、历史迁移登记名称和摘要不变。初始化仍先执行 schema.sql，再按编号执行后续迁移；已有库不重放已登记的迁移，无数据库结构变更。

## 验收方式调整 — 2026-10-12

移除固定测试套件、断言脚本、模拟来源及旧 benchmark 支撑代码。验收改由独立子 agent 按真实用户路径现场执行，范围、命令和证据保留在实验工件中；规则写入根目录 AGENTS.md。
CI 仅保留 Ruff 静态检查，业务验收单独记录。历史原文、冻结配置、结果及执行源码保持原样；不改变 Runtime 或数据库 Schema。

## 0.2.0 / Document-only（Issue #13）— 2026-10-12

所有成功内容响应统一进入 Archive → 格式 Parser → Document；来源发现与解析解耦。
Business、异构、越南法律和通用适配器保留网络发现/分页/公开表单，删除业务 Record 生成、
字段映射/Schema、细粒度 Locator。保留九格式、五节点、文档身份/版本/Emission 与原文血缘。
移除 Replay、Inspect/Export 与旧 CLI，改为 init、collect/recheck source.yaml、worker；
YAML 自动注册不可变绑定，--enqueue 可异步提交，周期采集交给外部 Cron。在线 Recheck、
有限重试、generation/attempt 防护和自动恢复保留。前台新增 stderr TTY rich.Live 只读面板。

新增 0009_document_only.sql，历史迁移不修改。Record/result 专属表实际 DROP；其余自建表、
索引、约束、函数与触发器迁入 seal 并去 seal_ 前缀，第三方 public.procrastinate_* 保持原样。
已有库必须停所有客户端、检查 init 清单并使用 --backup PATH --acknowledge-data-loss；
未知依赖会原子阻止升级。Record 历史有意删除，DROP 回滚依赖备份恢复；Document/Run/Archive
保留。外部 SQL 需改用 seal.<table>，不提供旧名兼容视图。结构化查询与导出仅用 SQL/COPY。

当前设计、验收范围与命令见 [Plan](docs/plans/unified-document-pipeline.md)。以下为历史版本记录，
其旧命令、Record/Replay 和验证结果不适用于 0.2.0。

## 统一文档流水线（Issue #10）— 2026-10-11

新增 Source `document.v1` 与 `recipes/documents`：响应先归档，再解析、校验和保存统一文档，
可独立查询/导出；业务提取为可选后续步骤，继续使用 #6 字段合同。统一文档只包含整篇
article、image 引用、二维 table、完整 json 值和 document_ref 外键五种内容节点。
复用 HTML、文本 PDF、XLS、DOCX、TXT、JSON，补齐真实 CSV 二维与 openpyxl XLSX；
图片关联原文容器/成员。PDF 不包含 OCR 或可靠表格识别，XLSX 公式不计算。

新增 `0008_unified_documents.sql`，沿用既有资源身份和原文 Revision，保存不可变解析版本、
实际 Run emission 与共享目标外键。`inspect document` / `inspect document-version` 和
`export --documents` 提供独立出口；引用查询区分未下载、解析失败和已解析。只有 complete
在线 Run 推进当前表示指针，失败保留原文和有效工件。Record 保存全部实际文档版本，
document Locator 从归档及冻结解析规则复算。旧 record/generic hash、对象和出口保持兼容。

升级前停止 Worker 并备份数据库/Archive，迁移后重新 pack Recipe 和建立 Binding。
解析代码/依赖变化后旧 document Locator 明确拒绝版本漂移；新 Binding 可零网络重解析
旧原文，精确旧版本仍使用配套旧环境。完整合同及验收入口见
[Plan](docs/plans/unified-document-pipeline.md) 和
[唯一可视化](docs/generated/unified-document-pipeline.html)。

## V1.3 / 0.1.3 — 2026-10-10

新增基于业务 Seed 的入口扩展、有界递归链接与 Sitemap 发现，复用原生 Scrapy
Scheduler、去重、既有 Discovery 与 Scope。seeded Recipe 要求 robots 开启；
政策请求归档、计入预算，失败关闭业务访问。旧 Source 默认 false 保持兼容。

Specific HTML 规则优先，无匹配时由同一 Recipe / Record 合同的 Fallback 提取正文。
新 HTML Locator 可复算标题、法律段落层级、编号、表格、日期 null、URL 和附件。
登录、导航、错误及无可靠正文的页面明确拒绝，不生成成功文档。

新增 `seal collect` 与 `runs.collect_source()`、当前 attempt 的运行计数和终止原因、
`inspect record` / `inspect snapshot`。存储、幂等、业务版本、Replay 和有限恢复
继续使用现有 PostgreSQL 与不可变原文归档。新增 `0006_seeded_discovery.sql`；
已应用迁移不修改，升级需停止旧 Worker、备份、迁移和新 RecipeVersion / Binding。

Review 修复：Recheck 关闭计划外递归；robots Sitemap 按政策版本每 Crawl 发现一次；
gzip Sitemap 有界解压并保留下载表示。partial Replay 冻结原拒绝证据，未知请求及
已成功归档但映射缺失的输入仍严格失败。robots 规则拒绝与政策获取失败分别审计；
动态附件使用最终 Content-Type 并保留父输入，XLSX 明确为未支持解析的格式。

新测试、实际长期留存演示及八个业务 Seed 的探索记录见
[V1.3 Plan](docs/plans/v1.3-seeded-site-collection.md)。网络政策拒绝与未知适配如实记录，
无站点 Ground Truth，不依据队列耗尽推断覆盖。历史工件保持原样。

## V1.2 / 0.1.2 — 2026-10-10

### 业务 Record 与异构来源扩展

- 增加业务 Record 模型、Discovery、多记录 JSON 提取、静态 iframe 处理与业务附件关联。
- 支持针对政企与公开平台的异构入口解析，为特定来源提供专用 API / 游标规则与结构化字段抽取。
- 归档合同调整：按新归档规范移除 Runtime 正文敏感性扫描，网络响应按原始 bytes 完整归档，URL/日志脱敏；归档前拒绝非空凭据字段（如 csrfToken/authToken）。归档目录要求私有权限（`umask 077`）。

### 任务与调度增强

- 新增增量迁移 `0003_records.sql`、`0004_discovery.sql`、`0005_recheck_plan.sql`，支持 Record、Discovery 与复查计划/退避元数据。
- 修复 JSON 稳定身份 Recheck、到期记录自动分批、失败重试日期以及重复 JSON 解析问题。
- 架构确立模块化单体：Scrapy + 可信 Python Recipe + Procrastinate + PostgreSQL + CAS 内容寻址归档。

### 升级与设计文档

- 升级需停止 Worker、备份数据库与归档，执行 `seal db migrate` 后重新 pack Recipe 并创建 Binding。
- 详细设计与验收参见 [V1.2 Plan](docs/plans/v1.2-heterogeneous-sources.md) 与 [唯一可视化](docs/generated/v1.2-heterogeneous-sources.html)。

## V1.1 / 0.1.1 — 2026-10-09

V1.1 交付独立 Runtime。产品版本称为 V1.1，Python 包版本为 `0.1.1`；沿用 Scrapy、可信 Python Recipe、Procrastinate、PostgreSQL 和本地原文归档。

### 执行与数据合同

- 候选 Binding 可直接同步运行或进入真实 Worker 队列，不必成为 Source 默认版本，也不会因运行而改变默认值。
- 删除 Trial、Gold Comparison、Review、Activation、发布门禁及相关 CLI；配置和默认 Binding 选择保留。
- 导出改为可追溯的运行结果。`complete` 表示技术执行完成，`partial` 保留技术校验通过的部分结果；二者均为 `quality_status=not_evaluated`。
- 保留独立 Observation、内容寻址原文、A→B→A 顺序修订、不可变 ProcessingResult，以及有限恢复、完成事务重入和过期任务 fencing。
- 离线 Replay 使用历史原文和明确的 Binding，不补发网络请求，不改写原始归档。

### 验收修复

- Locator 增加 `transform: date_iso` 与 `segments.separator`，支持日期规范化和原文片段拼接；保留定位凭据，严格复算并比较最终输出。旧 locator 不带新字段时保持原行为。
- 固定 `ROBOTSTXT_OBEY=False`，删除 robots 专属范围豁免和完成状态特判。普通业务 HTTP 错误仍参与执行状态，最高检不再被自动 robots 请求误标为 partial。
- 保留域名、路径和预算检查，拒绝非公网 IP 字面量及本地域名；loopback 仅供显式开启的隔离验收使用。

### 升级与兼容

运行 `seal db migrate` 应用 `0002_runtime.sql`；迁移前停止所有写入并备份数据库和归档。保留原 `schema.sql` 与历史 Run、Binding、Result、审核 payload、Revision 和原文；旧未完成任务终止，旧 Source 暂停。

升级后重新 `recipe pack`、`binding create`，再执行 Run/Replay 和默认版本选择。旧 Recipe 源码与归档可以复用，但旧引擎的 Binding 不绕过环境摘要校验。回退需要恢复配套备份。**旧发布 JSON 消费者必须适配新的运行数据出口，不能继续将其视为审核通过的数据。** 详细步骤见 [README](README.md) 和 [V1.1 Plan](docs/plans/v1.1-runtime.md)。

### 实际验证

全部测试使用独立数据库、队列和归档目录。最终受测 Runtime 与本次提交的代码摘要一致。

| 集合 | 实际结果 | 耗时 |
| --- | --- | --- |
| 既有 M3 回归，最终工作区复验 | 136/136 断言 PASS | 83.47 秒 |
| 独立 smoke | 36/36 Case、2182/2182 断言 PASS | 204.64 秒 |
| Golden / Runtime / 反向与边界 | 8/8、12/12、11/11 PASS | 已包含于 smoke |
| 历史兼容 / robots 与业务错误 | C01、X01 PASS | 已包含于 smoke |
| 最高法、最高检、Python 教程 | 各 10 条详情、两轮 complete、改版离线 Replay 通过 | 已包含于 smoke |

Golden 只覆盖当前 Schema 的 title/date/body；update_date/document_no 共 16 项字段检查为 NOT_APPLICABLE，不计 PASS。非法转换、错误定位和错误输出均被拒绝，CLI、Worker 与 Replay 使用一致校验。真实来源结果不构成质量、完整性或持续可用性承诺。

完整本地证据保留于 `artifacts/acceptance/v1.1-runtime-fixes/`，原 `v1.1-smoke/` 保持不变。工件按仓库规则不入 Git；下列 SHA-256 固定本次结果，不能用后来生成的结果替换本次记录：

- `m3-final/manifest.json`：`39356a6e6230c2a7e82a6c328e55f9e9f6283da5bcbe6df3ad0aed1e362807cb`
- `smoke/manifest.json`：`bd1f2fe4874fae9e46c3650b44ce822281700835640a22459e52e51dd4f409af`

复现环境为 Python 3.12.13、PostgreSQL 17.11 和 `uv.lock`；`initdb`、`pg_ctl`、`pg_dump`、`psql` 需在 PATH。

```sh
uv sync --frozen
./experiments/v1.1-runtime-acceptance/acceptance.sh --stage m3 --output artifacts/acceptance/v1.1-repeat/m3
./experiments/v1.1-runtime-acceptance/acceptance.sh --stage smoke --baseline artifacts/acceptance/v1.1-repeat/m3 --live --output artifacts/acceptance/v1.1-repeat/smoke
uv run --frozen python experiments/v1.1-runtime-acceptance/verify_artifacts.py artifacts/acceptance/v1.1-repeat/m3
uv run --frozen python experiments/v1.1-runtime-acceptance/verify_artifacts.py artifacts/acceptance/v1.1-repeat/smoke
```

C01 额外需要上一轮保留的 `artifacts/acceptance/v1.1-smoke/court-confirmed/` 数据库与归档；新 checkout 缺失时明确记为 UNVERIFIED，不能称为全量通过。省略 `--live` 可关闭外网测试；外网变化如实记录，不作为必须通过的 CI。

### 文档与工件整理

V1 Plan、历史 ADR、旧可视化及最终验收均保留审计用途。仅清理与仓库实验原件完全相同的重复输出，将早期开发验收和独有分析报告压缩到 `artifacts/archive/v1-development.tar.gz`；清理清单及摘要保留在 `artifacts/releases/v1.1-commit/cleanup.json`。不删除原始 Golden Expected，不改写历史验收结论。

### 未交付与未验证

Evaluation、Management 审核/正式发布和 Agent 均未实现。未验证真实 PDF、真实来源日期准确性、业务总体质量、生产规模、整机断电持久性、恶意 Python 隔离或 DNS 重绑定防护。V1.1 工程验收通过仅限本轮受测 Runtime 范围。

## V1 / 0.1.0 — 2026-10-09

初版提交 `b32184b3628302efcaf012f4b0cd006050bbb72c`：可信 Python Recipe、Scrapy 采集归档、Procrastinate 队列、版本血缘与恢复，以及当时的 Trial、人工审核、激活和发布流程。

[V1 Plan](docs/plans/v1-information-pipeline.md)、[ADR-0002](docs/adr/0002-python-recipes-minimal-v1.md) 与 [旧可视化](docs/generated/v1-overview.html) 保留原始合同；V1.1 的职责调整以 [ADR-0003](docs/adr/0003-runtime-evaluation-management.md) 为准。
