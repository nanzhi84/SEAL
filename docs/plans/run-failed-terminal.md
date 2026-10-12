---
kind: plan
lang: zh
---

> 历史设计或验收材料：命令与 Record/Replay 合同仅对应当时版本。当前合同见 [Document-only Plan](unified-document-pipeline.md)。历史结果不代表本次验收。

# Run 统一失败终态

依据 [Issue #7](https://github.com/nanzhi84/SEAL/issues/7)，基线为 main
`fb5733a`。唯一派生物为 [Generated](../generated/run-failed-terminal.html)。

## 目标与领域边界

Run 描述执行进度；成功终态 `complete` 只表示技术执行完成，失败终态统一为
`failed`。`pending`、`running`、`finishing`、`retryable` 保留过程语义。
原因直接读取已有 `errors`：稳定错误码组成的 JSON 数组，排序、去重。
不新增错误分类对象，不保留与 errors 重复的 `terminal_reason` 或 `termination`。
Scrapy 原生 `finish_reason` 保留为执行上下文；日志不能决定重试或导出。

Run 生命周期负责次数、总期限和 fencing；Completion 验证输入、结果及归档，
根据错误和剩余预算决定重试或终态；Manifest 只冻结执行证据；Export 根据每条
结果的既有技术校验、输入血缘和归档可读性导出。Discovery 只记录请求证据。
将 Discovery 汇总读模型拆出超长模块，保持调度器与证据层依赖方向。

## 状态、错误与并发合同

临时下载故障、HTTP 5xx 且没有非临时错误时，在线 Run 在次数和总期限内成为
`retryable`；耗尽写入 `failed` 与 `attempts_exhausted` / `deadline_exceeded`。
解析、Schema、robots、归档、权限错误直接 `failed`。HTTP 429 沿用冷却，
不在当前 Run 自动重试。Replay 保持零网络，不自动重试。
单次 Scrapy 时间预算耗尽追加 `crawl_time_budget_exceeded`，保留原生
`finish_reason=closespider_timeout`。耗尽错误也进入未知覆盖证据；慢回调留下的
合法结果仍可显式导出，不把这次预算耗尽当作临时故障。

generation 改变、Source 暂停或更高 run_seq 获得写资格时，旧 Run 以
`source_generation_changed`、`source_paused`、`run_superseded` 明确失效。
旧 attempt 的迟到回调受 epoch 保护；旧 finisher 不得终结当前 attempt。
只有 `complete` 推进 Record 成功基线与检查时间；失败有效结果可以用
`export SOURCE --run RUN` 查看，默认导出保留历史成功值。

## 持久化与外部接口

新增 `0007_run_failed_terminal.sql`，停止写入进程后运行 `seal db migrate`。
历史 `partial` / `superseded` 行改成 `failed`，合并原 errors 与 report.errors，
把历史 `report.terminal_reason` 的具体错误码补入 errors，再删除重复字段。
没有可用错误码的旧失败行使用 `historical_execution_failed`；旧 superseded
保留已有 `superseded` 错误码。迁移加状态 CHECK，阻止写入旧终态。
不修改已应用迁移，不改 Snapshot、Manifest 或 body 的不可变字节与 Hash。
因此当前数据库状态为 failed 时，历史 Manifest 仍可能记录 partial/superseded；
历史归档是当时的事实，不作为当前状态机输入。迁移可重复调用。

CLI Run/Collect/Replay/Retry/Finish 的失败回执继续非零退出；Inspect/Export
仍可读取失败工件。errors 数组和成功导出接口保持结构，summary 保留 terminal
布尔及计数，删除重复分类字段。依赖旧终态或重复字段的调用方须更新。
Recipe 环境摘要仍包含引擎/SQL：本次 Runtime 升级使旧 Bundle 出现
`environment_drift`，旧 Binding 在新 Runtime 严格拒绝；历史 Record/Export
仍可读，归档不可变。同一 Recipe 源码重新 pack、创建新 Binding 后可零网络
Replay 旧 Run.inputs，结果值和 Locator 保持一致，不绕过防漂移。精确复现旧
Runtime 使用旧 checkout/venv 与升级前数据库副本；旧 Worker 不连接新库。
代码回滚不能配合旧写入器直接复用已迁移库；保留升级前数据库备份，归档可直接复用。

## 先列失败方式与行为验收

| 场景 | 真实 CLI 外部可见断言 |
| --- | --- |
| 解析失败、有成功基线与有效新输出 | failed、errors 可读；显式导出有效项；默认成功值不变 |
| 临时 503 恢复 | 单请求原生重试恢复与逻辑 Run 重试恢复都 complete；旧 attempt errors 留在历史 |
| 重试耗尽 | 三次 attempt 后 failed，错误含 http_5xx 与 attempts_exhausted；第四次不能重置 |
| 期限耗尽 | 未开始或 retryable 的过期 Run failed，deadline_exceeded，无新下载 |
| 单次采集时间预算耗尽、慢回调仍完成 | failed、crawl_time_budget_exceeded 可机读；原生 finish_reason 保留；有效结果可显式导出 |
| 新 run / generation 取代、旧 attempt 晚到 | failed 与具体 errors；旧写入不能覆盖新成功值 |
| 历史迁移 | 旧终态收敛、历史 reason 成为 errors，重复字段删除，归档原 Hash 不变 |
| 失败归档损坏 | 显式导出拒绝损坏条目并返回 unavailable；失败不保证有可用结果 |

复用 M3、V1.2 Record/附件、V1.3 Persistence/robots/Replay 验收，不补单元测试。
先提交设计和外部行为测试，再实现状态收敛。删除被真实 CLI 覆盖的旧分类函数
镜像单元测试。验收数据使用 Harness 自建临时 PostgreSQL 与 loopback 服务，
SEAL_ARCHIVE 独立目录；不读取开发/生产数据、不包含凭据。

## 验证命令与工件

环境：Python 3.12、uv.lock 冻结依赖、PostgreSQL 17+ 的 initdb/pg_ctl 在 PATH。
使用每次不存在的新输出目录；manifest.json 记录环境、源码摘要、命令和文件 SHA，
assertions.json、receipts.json、request-ledger.json 与 archive 可离线复算。

```sh
uv sync --frozen
uv run --frozen python experiments/v1.1-runtime-acceptance/acceptance.py --stage m3 --output artifacts/run-failed/m3
uv run --frozen python experiments/v1.2-acceptance/ci.py --group core --output artifacts/run-failed/core
uv run --frozen python experiments/v1.2-acceptance/ci.py --group offline --output artifacts/run-failed/offline
uv run --frozen python experiments/v1.1-runtime-acceptance/acceptance.py --stage smoke --baseline artifacts/run-failed/offline/m3 --output artifacts/run-failed/smoke
uv run --frozen python experiments/v1.3-acceptance/ci.py --output artifacts/run-failed/seeded
```

## 执行证据

2026-10-11 在冻结产品 head `600d632`、macOS arm64、Python 3.12.13、
uv 0.11.23、PostgreSQL 17.11 下执行；各集合自建独立数据库、loopback 服务与
Archive。设计/行为测试提交先于实现；审核发现的时间预算缺口也先提交真实 CLI
断言并复现 RED，再提交修复。

| 本地集合 | 实际结果 | 工件目录 |
| --- | --- | --- |
| core | PASS：联合 475/475，公开探测 33/33；离线复核 599 + 4 文件 | `artifacts/run-failed/review-core` |
| offline | PASS：M3 185/185、URL 29/29、Recheck 115/115；工件离线复核通过 | `artifacts/run-failed/review-offline` |
| Smoke | 1191/1191 断言、32 cases PASS、0 FAIL；C01 UNVERIFIED；基线源码摘要一致、离线复核 267 文件 | `artifacts/run-failed/review-smoke` |
| seeded | FAIL：8 条业务路径共 369/369 与 75 项既有测试通过；跨主机附件路径未进入产品执行 | `artifacts/run-failed/review-seeded` |

本机未配置 `127.0.0.2`，seeded 的 dynamic-attachment fixture 在绑定该地址时
返回 `OSError: [Errno 49] Can't assign requested address`。不替换跨主机条件、不改
系统网络；完整跨主机验收由 Ubuntu 24.04 / PostgreSQL 17 的远端 CI 复跑。
本地 FAIL 工件保留，不计入完整通过结论。

Smoke C01 的外部历史基线 `artifacts/acceptance/v1.1-smoke/court-confirmed/database.sql`
未提供，保留 UNVERIFIED；没有生成替代工件冒充该历史输入。本 Issue 的迁移、
原归档 Hash、旧 Binding 拒绝及同源码重打包后零网络 Replay 在 M3 中通过。
PR 合并前以最终 head 的全部 [CI checks](https://github.com/nanzhi84/SEAL/pull/8/checks)
和独立审核为准；本地工件保存冻结产品版本，后续文档提交不改执行源码。

真实来源覆盖、业务质量、生产部署及断电耐久性不属于该状态合同的合成验收。
