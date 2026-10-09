---
kind: changelog
lang: zh
---

# 版本记录

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

升级后重新 `recipe pack`、`binding create`，再执行 Run/Replay 和默认版本选择。旧 Recipe 源码与归档可以复用，但旧引擎的 Binding 不绕过环境摘要校验。回退需要恢复配套备份。**旧发布 JSON 消费者必须适配新的运行数据出口，不能继续将其视为审核通过的数据。** 详细步骤见 [README](README.md#安装与升级) 和 [V1.1 Plan](docs/plans/v1.1-runtime.md)。

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
