---
kind: plan
lang: zh
---

# Document-only 采集解析流水线

依据 [Issue #13](https://github.com/nanzhi84/SEAL/issues/13)，替代 #10 的可选文档路径。
唯一派生物：[Generated](../generated/unified-document-pipeline.html)。

## 目标与数据流

Source YAML → 不可变 Recipe / Binding → Run → Scrapy 发现与下载 → Archive bytes /
Snapshot / Observation → 通用格式 Parser → Document / Revision / Version / Emission → SQL。
Recipe 只负责合法网络交互、链接发现、分页、表单与解析配置。所有内容响应由 Runtime
统一解析，解析失败不阻断来源发现。唯一内容产物是 Document，未来消费者另立模块。
删除 Record、业务字段映射及 Schema、细粒度 Locator、Replay、Inspect、Export 和内部周期调度。
原始 JSON 作为 json 节点保存，不进行业务 JSON Extraction。无 OCR 和业务质量评估。

## 模块与合同

Archive 先保存已接收的解压后应用 bytes。重定向、HTTP 错误和 robots/sitemap 为控制或失败
响应，保留原文与观察证据但不生成文档。成功 GET/POST 内容响应必经统一解析；空内容、
不支持格式、扫描 PDF、损坏文件与解析异常保存 processing_stage / processing_error。
内容解析、来源回调分别运行；来源回调不能自行提交内容项。

Parser 使用已有 HTML、文本 PDF、DOCX、XLS/XLSX、CSV、TXT、JSON、图片解析器，不强迫
所有格式使用同一种算法。可信 Adapter 可定义 parse_options(response)，只允许 body_css、
title_css、format、csv_delimiter；默认使用 params.options。XLS/XLSX 空单元格以 null 保存，保留二维布局；CSV 空字段保持空字符串。内容根为 {format, children}，
仅允许 article、image、table、json、document_ref。最多一个 article，table 为二维 JSON
标量，JSON 保留完整 envelope 与标量/null；附件引用以实际 document 外键关联。
文档结构、Source / Run / epoch / Snapshot / Observation 归属与 hash 必须验证；不复算
字段证据，不验证业务字段准确性。失败不生成成功 Version，不覆盖成功当前指针。

Document 的 GET 身份为 Source / namespace / URL；POST 身份加入语义 request body 摘要（仅排除声明的 query nonce 与 ASP.NET 隐藏状态，实际 body 摘要仍归档），
避免同 URL 不同页互相覆盖。Raw Revision 按 bytes 变化生成。Document Version 的处理键
包含身份、body hash、绑定、解析代码/依赖摘要、options 和影响解释的 Content-Type/encoding；
同输入与规则复用版本，Emission 独立保存本次 Run/attempt 的观察证据。不同 Run 不共享
可变执行状态。成功 Run 原子推进 latest_document_version；失败 Run 的 staged 版本仍可用 SQL
读取。引用仅保存身份关系，SQL 使用者自行联查当前或历史目标。

Recheck 在线获取变化。GET 使用已保存实际请求角色和 URL；POST 来源重新从配置种子获取
新表单/Token 并遍历声明范围，不重发历史 nonce。冻结目标保存在 Run.recheck_plan。未变化
复用 Version，变化产生新版本；未知/未观察不表示删除。attempt fencing、generation、run_seq、
超时、预算、有限重试、幂等、Source 状态和崩溃自动恢复继续保留。显式 Recheck 检查当前保存目标；next_check/recheck_retry_at 保留为 SQL 调度元数据，外部 Cron 决定触发时间。

## 四命令 CLI 与配置

仅保留 init、collect、recheck、worker。collect/recheck 接收一个 YAML，包含现有 Source
范围、entry_urls、预算与 recipe: {path, params}；相对路径以该 YAML 所在目录解析。
自动注册/更新 Source、打包或复用 Recipe、创建/复用 Binding、创建 Run。选择配置与创建
Run 在同一 Source 锁事务完成，配置变化增加 generation，不能静默覆盖运行中配置。
删除 output_schema、business_key 身份与 poll_seconds。外部 Cron 调用相同命令。

collect/recheck 默认前台并自动有限重试，--enqueue 只返回 pending Run ID；worker 消费
Procrastinate 持久任务并自动恢复中断任务。stdout 为单份 JSON，失败退出 1，参数/操作错误
退出 2；Ctrl+C 返回 interrupted 回执与退出 130，并终止子进程。无隐式 Schema Migration。

TTY 前台使用 rich.Live 在 stderr 显示只读 ProgressSnapshot；非 TTY、--no-progress、入队
不显示。按单调时钟计时，刷新最多 4Hz，数据库采样最多 1Hz。Discovered 为去重内容 URL，
Fetched 为已归档内容响应数，Documents 为本 Run 文档身份数，Failed 为最终失败 URL 数，
Pending/Active 为实际请求状态数。重试/重定向尝试不作为 URL 分母，附件属于内容；控制
URL 不计。Known URL progress 仅对当前发现集合展示，递归扩展可回退，不表示全站覆盖。
Recheck 展示 Unchanged/Updated，当前 URL 去除认证与敏感参数并截断。面板异常不影响执行。

## 显式数据库迁移与回滚

历史 schema.sql 与 0002–0008 原样保留。新增 Migration 删除 public.seal_record、
seal_record_result、seal_record_version、seal_record_emission 及旧 generic seal_result。
只使用明确 DROP，不用 CASCADE；未知外部依赖必须使迁移失败。清理专属列及输出选择配置，
将保留自建表、索引、约束、触发器、函数及序列迁入 seal 并去 seal_ 前缀。自建 SQL 显式
使用 seal.<table>；Procrastinate 原名/原 Schema 不动。迁移记录最终为 seal.migration。
历史 Run、Document、Revision、Version、Emission、Observation 与 Archive 保留；旧执行
mode 作为历史只读数据保留，Runtime 只接受 collect/recheck，不重新执行历史路径。

seal init 首次安装执行完整迁移；已有库先返回目标标识、将删除对象/行数与全部依赖清单，
要求显式 --backup PATH --acknowledge-data-loss。CLI 生成 pg_dump 备份并核验成功后才执行。
升级前停所有 Worker；生产需另行确认目标、备份和停机窗口，本次仅操作隔离测试库。
DROP 无无损逆向迁移，回滚依赖备份恢复。已迁移 init 幂等；collect/recheck/worker 仅检查
当前迁移版本，未初始化或旧 Schema 明确拒绝。Archive 不被清理。

## SQL 访问

seal.document.latest_document_version → seal.document_version.content(JSONB)；历史直接查询
Version，指定 Run 通过 seal.document_emission 联查 Observation/Snapshot/body hash。
使用 PostgreSQL COPY / \\copy 导出查询结果。数据库导出不等于打包 Archive，原始 bytes
根据对象摘要从 Archive 读取。无便利查询 API、DSL 或 Document Bundle。

## 实施前失败方式与端到端断言

1. 四命令 help、无旧入口；新库未 init、旧库未升级时禁止 collect/worker 隐式迁移。
2. 九种格式与五节点、列表/分页/共享附件、重复 URL、重定向、重试均从真实 CLI 入库。
3. 扫描 PDF、损坏文件、空内容、未知格式、解析失败保留原文与可查询阶段/原因，继续发现。
4. 重复 Collect、Recheck unchanged/updated、配置变化绑定不可变；失败不推进成功指针。
5. 真实 --enqueue / worker、Ctrl+C / 崩溃 / 有限重试 / 过期 / generation 与并发覆盖防护。
6. 旧库插入 Record 与 Document 历史，备份、对象清单、迁移及备份恢复；外部依赖阻止误删。
7. 独立 pg_catalog 核验 Schema / 索引 / 函数 / 约束 / 行数 / 外键 / 第三方队列不回归。
8. SQL/COPY、Archive bytes/hash、Version/Emission 血缘独立核验；TTY 进度与 SQL 计数一致，
   非 TTY 无 ANSI、stdout 单份 JSON、窄终端/异常/中断不污染状态。
9. Business、异构、越南法律表单与通用来源用固定样本核验发现、文档与原文；公网小样本
   单独标记范围，合成样本不代表真实全站。

验收入口为 experiments/document-only/acceptance.py，使用独立 PostgreSQL 17、loopback 合成
HTTP 与专用 Archive。实际配置在 config/，原文/数据库在 work/<run>/，命令回执、冻结配置、
SQL/COPY、对象清单、环境/代码摘要、断言与未覆盖项在 artifacts/<run>/；工件不含凭据。
历史工件保持原样，旧功能专属可执行脚本与测试删除，文档明确历史边界。

```sh
umask 077
uv sync --frozen
uv run --frozen python experiments/document-only/acceptance.py --name acceptance-new
uv run --frozen python experiments/document-only/verify.py experiments/document-only/artifacts/acceptance-new
uvx --from ruff==0.16.10 ruff check src recipes experiments/document-only
uvx --from ruff==0.16.10 ruff format --check src recipes experiments/document-only
seiso check
```

本次验证结果以最终验收工件和精确 PR head 的 CI 为准，不复用 #10 的历史 PASS。

## 本次实现与验收结果

[PR #14](https://github.com/nanzhi84/SEAL/pull/14) 收敛了统一入口，删除旧输出及专属测试；
手写运行代码和本次验收脚本均小于 500 行。历史 schema.sql / 0002–0008 无改动，
历史原文、冻结配置和结果不重写。当前验收只调用四命令与 PostgreSQL，未执行生产迁移。

| 节点 | 输入与实际命令 | 实际结果、输出及下游用途 |
| --- | --- | --- |
| Collect / Parser | 九格式、五节点、所有保留 Adapter 的固定合成 HTTP；真实 collect YAML | 467 项 E2E 断言 PASS，SQL 保存文档与失败诊断；覆盖分页、API、iframe、POST nonce、压缩响应，后续用于 Recheck 和查询 |
| Run / Worker / TTY | --enqueue、worker --once、真实 PTY、Ctrl+C、SIGKILL 与真实心跳等待 | 版本复用、变化、不可变绑定、generation/epoch、截止时间、前台与 Worker 恢复、面板及 stdout JSON PASS |
| Migration / Backup | 含真实 CLI Document 与合成旧 Record 的隔离旧库；init 对象清单、备份和确认 | 未知依赖使迁移原子失败；重试升级移除 Record，保留 Document/Run/历史 mode 与第三方队列；备份实际恢复 PASS |
| 独立 Oracle | pg_restore final.dump，再用独立 SQL、Archive 与 canonical content hash 核验 | 1492 项断言 PASS；281 个 Observation、88 个 Version、232 个 Emission 的保存证据通过；无需 Runtime Parser 或 Replay |
| 公网有界样本 | AMAC trustee、教育部单篇通知与其 XLS；Collect → Recheck | 分别 8 / 3 个 Document、完整 JSON envelope 和 XLS 原文通过核验。新闻出版署单篇 PDF 的 robots 不可用，FAILED；全站覆盖一律 UNKNOWN |

本机环境为 Python 3.12.13 / PostgreSQL 17.11，uv.lock 和代码摘要在各工件 summary.json。
固定样本工件位于 `experiments/document-only/artifacts/final-local/`；公网工件位于
`experiments/document-only/artifacts/final-live/`，原文和备份留在对应 work/。verification.json
保存独立 Oracle 的数量、方法、备份/验证代码摘要和范围。CI 对精确 PR head 重跑固定验收，
上传无凭据合成样本的元数据；[Checks](https://github.com/nanzhi84/SEAL/pull/14/checks) 为合并前结果依据。

公网运行配置在 config/live.yaml；可使用以下命令独立复跑，来源变化可能造成新失败，不自动
降低 robots 或网络范围约束。验收仅证明列出的有界来源与合同，未验证生产升级、全站覆盖、
掉电耐久性、真实站点长期变化或所有分区的全国许可证数据。

```sh
uv run --frozen python experiments/document-only/live.py --name live-new
uv run --frozen python experiments/document-only/verify.py experiments/document-only/artifacts/live-new
```
