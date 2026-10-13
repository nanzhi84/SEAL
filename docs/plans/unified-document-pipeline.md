---
kind: plan
lang: zh
---

# Crawl4AI 来源采集与 URL 版本时间线

状态：Issue #17 已实施并通过有界独立业务验收。唯一派生物：[Generated](../generated/unified-document-pipeline.html)。
本计划替代 Document-only 目标；文末 PR #14 仅为历史证据。生产迁移未执行。

## 目标、领域边界与产物流向

SEAL 负责来源配置、周期触发、运行恢复、响应归档与资源时间线；Crawl4AI 0.9.4
负责每轮请求调度、递归遍历、获取与通用解析。下游直接消费 CrawlResult，不转成自定义
Document AST，不新写 Document/Revision/Emission/document_reference。外部周期调度
仍通过四命令触发；不提供替代 Crawl4AI CLI 入口，不引入另一套内部 URL Scheduler。

```mermaid
flowchart TD
 S[Source 来源入口与冻结配置] --> F[Frontier 长期来源状态]
 F --> R[Resource 来源内URL]
 CLI[SEAL CLI / Worker] --> C[Crawl4AI 本轮获取与遍历]
 F --> C
 C -->|SDK checkpoint| F
 C --> O[Observation 正文归档与处理结果]
 R --> O
 O -->|响应变化且目标有效且解析成功| V[Version 原生CrawlResult]
 R --> V
 V --> D[下游 当前版本 / timeline / cursor增量]
```

Source:Frontier 为 1:1，Frontier:Resource、Resource:Observation、Resource:Version
均为 1:N。同 URL 在不同 Source 独立；唯一资源身份为 frontier_id + URL，保留查询参数
及其顺序，移除 fragment。重定向实际 URL 记在 Observation，不静默合并资源。
Source 使用地图 dd-xxx；UUID、cursor、内容哈希保留各自语义。地图不是采集许可。

## 模块职责与依赖方向

| 模块 | 实际职责 |
| --- | --- |
| cli / configuration / config | 四命令与严格 YAML；冻结 Source 配置及 config_hash |
| runs / queue / run_state | Source 会话锁、Run epoch/generation、子进程、取消、有限恢复 |
| crawl | 装配 SDK、消费 streaming CrawlResult、保存 framework checkpoint |
| sdk / robots / scope | 薄 HTTP/DOM 获取适配、范围与 robots 边界、SDK HTML/PDF scraper |
| resources | 来源内资源身份、事务 Observation/Version 提交与去重 |
| archive / core | CrawlResult 存储编码、文件/bytes CAS 与原子落盘 |
| completion / manifest / progress | Run 终态、无敏感内容的 JSON 统计、stderr TTY 面板 |
| db / migration | PostgreSQL 基础与追加迁移、清单、备份校验 |

依赖方向为 CLI→配置/生命周期→SDK与资源提交→数据库/CAS；core、scope 和 run_state
不依赖上层。Scrapy、Recipe 打包、Binding 执行和旧五节点 AST 模块及其共用 recipes/
已删除，不存在 shim 或双写。历史实验、原文与迁移保持原样；实验旧源码不代表当前能力。

## Source 配置与兼容边界

[最小模板](../../examples/source.yaml) 是当前配置入口。旧 YAML 含 recipe 时明确报
legacy_recipe_config_requires_migration；不静默忽略原有分页、表单、字段或输出合同。
CLI 命令/选项保持，YAML 与 JSON receipt 是有版本的不兼容变更。

```yaml
id: dd-017
entry_urls: [https://example.org/]
allowed_hosts: [example.org]
allowed_path_prefixes: [/]
archive_approved: true
scope: Authorized bounded collection after source review
robots: true
recheck_seconds: 86400
concurrency: 2
budget: {requests: 100, seconds: 120, response_bytes: 10485760}
crawl4ai: {mode: http, max_depth: 3, page_timeout_ms: 30000, word_count_threshold: 1}
```

入口可以多个；host_path_scopes 可按 host 缩小路径。robots 默认 true；缺失政策404/410
被明确记为缺失，其余政策错误拒绝业务请求；业务与政策请求均计预算。robots:false 只能
在来源审查与授权后使用。HTTP 重定向逐跳检查范围，DNS 解析地址拒绝非公网地址；
隔离验收显式设置 SEAL_ALLOW_LOOPBACK=1。浏览器子请求与重定向通过 Chromium Fetch Request 阶段逐跳检查 host/path、robots 和
请求预算；被拒绝记录错误，不宣称整页所有 CSS/JS/图片/API 均被归档。浏览器 URL 检查
不构成操作系统网络沙箱，部署仍需网络出口约束防 DNS 重绑定。

首版仅固定公开 GET 上下文，不带用户浏览器状态或隐式登录；旧 POST/ASP.NET 表单、
来源专用分页规则尚未迁入。旧配置不能作为当前兼容入口。合法会话、可信来源 hook、
Office/OCR 等未来扩展需独立实证，不恢复 Recipe 包/Binding。

crawl4ai.mode 可选 http 或 rendered_dom。http 归档 aiohttp 获取阶段 Content-Encoding
解码后的正文 bytes，先于字符解码与 SDK 解析；不是从 result.html 反编码伪造 HTTP。
rendered_dom 归档 SDK 获取阶段渲染 HTML 的 UTF-8 bytes，并显式标记模式。已有成功
Version 后切换模式报 input_mode_change_requires_new_source，禁止混合比较。
配置还支持 page_timeout_ms、word_count_threshold、css_selector、wait_until；
blocked_patterns 可补充来源阻拦检测。无自动登录、凭据刷新或验证码求解。

SDK 0.9.4 会将有正文的短 HTML 判为 minimal_text 阻拦页；仅对该尺寸启发式且有
p/h/main 正文结构的页面，以 SDK fallback 回调复用同次捕获的 HTML，不新增请求。
SDK clone 会丢失此回调，薄 RunConfig 仅修复该字段传递；升级至修复版本后可删除。
密码/挑战标记仍拒绝。CrawlResult 响应头中的Cookie/授权敏感值脱敏。

## 资源、观察与版本合同

| 对象 | 公开字段与约束 |
| --- | --- |
| source | id、config、generation、run_seq、write_seq、paused、cooldown_until；沿用旧来源身份 |
| frontier | id、source_id UNIQUE、run_id、checkpoint；长期来源状态，非框架原生服务 |
| resource | id、frontier_id、url、role、current_version_id、last_checked_at、last_outcome、next_check |
| observation | id、resource_id、run_id、attempt_epoch、delivery_key、url、actual_url、status、body_hash、archive_ref、input_mode、outcome、error、version_id、observed_at |
| version | id、resource_id、sequence、observation_id、body_hash、input_mode、crawl_result、framework_version、parser_config、cursor、created_at |

正文归档后校验 CAS 再提交引用；archive_ref=body_hash，路径为
$SEAL_ARCHIVE/objects/<哈希前2位>/<剩余62位>。Observation 与 Version 不可变，
复合 FK 约束资源、版本、原始观测、body_hash 与模式归属。一次 run/URL 仅提交一次；
checkpoint 恢复的重复交付不追加版本。不同 Run 每次形成新 Observation。

| 输入情况 | 结果 |
| --- | --- |
| 首次有效内容且解析成功 | Observation + V1，推进资源指针 |
| 等于当前成功版本的响应 | 新 Observation，outcome=unchanged，关联旧版本 |
| 响应变化且解析成功 | 新 Observation + 连续 Version，推进指针 |
| 响应变化但解析失败或不支持 | 保存原文及失败 Observation，不推进；下次新 Run 重试 |
| 网络错误、403/429、登录/挑战页 | 保存失败/blocked，存在正文则归档，不生成内容版本 |
| 仅更新解析配置或框架 | 正文相同则仍关联旧版本，不追加内容增量 |

比较基准始终是当前成功 Version 的正文。A→B→A 为 V1→V2→V3，CAS 可复用 A。
版本时间是发现变化时间；不推定网站修改时间。有效资源逐个事务提交，即便本轮其他 URL
失败也保留已提交结果；Run.complete 仅表示本轮有界处理无错误，whole-site 始终 UNKNOWN。
阻拦检测组合 SDK 结果与密码输入、验证码标记、登录/挑战标题和配置规则；无证据时不宣称
任意站点挑战均可识别，人工业务质量保持 not_evaluated。

## CrawlResult 与格式边界

冻结 crawl4ai[pdf]==0.9.4 与 uv.lock。HTML/TXT 使用 SDK LXML scraper；HTTP PDF
使用同次归档文件的临时副本交给 SDK PDFContentScrapingStrategy，避免再次网络下载
不同输入或清理 CAS 原件。无 OCR；无文本层 PDF 不创建成功内容版本。DOCX/XLS/XLSX/
CSV/JSON/图片首版尚无语义解析保证，标记 unsupported/parse_failed 并保留获取原文，
不能沿用九格式 Document 的历史支持声明。动态 DOM 模式以 HTML 为目标。

Version.crawl_result 是存储封装：result 为 SDK model_dump（包括完整 Markdown、links、
media 等），artifacts 将 pdf/screenshot/mhtml 二进制外置为 CAS；downloaded_files
也以 CAS 哈希替换临时路径。seal.archive.deserialize_result 可在新进程重建 CrawlResult
与长期本地路径；这是编码封装，不是自定义内容 AST。链接只保留 SDK URL 引用，不建立
document_reference 外键，也不承诺目标已获取或绑定历史版本。

## 发现、复查、取消与恢复

collect 每轮从入口重新建立 visited；recheck 冻结 next_check 到期的已知 content Resource，
即便已从导航消失也复查。无到期资源返回空结果；超过复查预算明确拒绝，避免默默裁剪。
一个 Source 始终只有一个 Frontier，上一轮记录不作为新轮永久排除集合。

运行获取 Source 的 PostgreSQL session advisory lock，同时持有 Run 锁；不同 Source
允许独立执行。子进程只负责 SDK；父进程监控拥有者连接、generation、epoch、暂停和预算。
前台 Ctrl+C 保存 retryable 状态与 checkpoint、退出130；Worker 通过显式任务取消信号停止
执行中的子进程并保留恢复状态，也输出单JSON/130。父进程崩溃后子进程组自动终止，
Worker 按任务心跳和有限 attempt 恢复，默认超过30秒无心跳后回收；
worker --once 只消费当前就绪任务，不会在仍有效心跳窗口内抢占 doing 任务。SIGSTOP 期间锁仍有效，不能超时夺取一个仍持锁
执行者。数据库会话丢失后父进程终止子进程；接管后的旧 epoch 不得写入。

SDK 0.9.4 BFS 流式 checkpoint 只保存 next_level，会遗漏当前层尚未 yield 的 URL。
SEAL 将 visited 中尚未持久交付的 URL 和获取阶段 inflight 补回 SDK pending；已提交但尚未
保存发现状态的输入允许重复获取来重新发现链接，提交依 run/URL 幂等。checkpoint 与资源
事务不是原子提交，不声称 exactly-once 网络请求；budget 覆盖恢复重复请求。

请求次数在获取阶段有界，含错误、robots、redirect 与浏览器子请求；SDK max_pages 流式
边界会在 yield 前截掉最后一个成功结果，首版不使用该边界，实际网络预算负责上限。
有限预算、深度和范围仅界定可发现覆盖；孤立页、表单分页、鉴权阻拦均可能遗漏。

## CLI、环境、下游与迁移

```text
seal init [--backup PATH] [--acknowledge-data-loss]
seal collect source.yaml [--enqueue] [--no-progress]
seal recheck source.yaml [--enqueue] [--no-progress]
seal worker [--once]
```

Python3.12；uv sync 安装依赖；DOM 模式另运行 uv run python -m patchright install chromium。
SEAL_DATABASE_URL 通过环境传入，SEAL_ARCHIVE 指向独立持久目录。CLI stdout 仅一份
seal.receipt.v3 JSON，stderr TTY显示资源进度；失败退出1，参数/配置失败退出2，中断130。
receipt 的 binding_id 移除，新增 config_hash，报告资源/Observation/Version统计；旧回执
和冻结配置保留历史语义，SQL 下游不可假定旧 Document 输出列仍代表当前写入。

```sql
-- 当前资源版本；按 Source/URL 使用参数化 SQL
SELECT * FROM seal.current_resource_version WHERE source_id=$1 AND url=$2;
-- 一个资源完整 timeline
SELECT * FROM seal.version WHERE resource_id=$1 ORDER BY sequence;
-- 失败 Observation 不进入增量；cursor分配至提交受全局事务锁保护
SELECT * FROM seal.version_delta WHERE cursor>$1 ORDER BY cursor LIMIT 100;
```

0010_resource_timeline.sql 为追加迁移，schema.sql 与0002–0009登记名称/字节不修改。
保留旧 source/run 与 Document/Revision/Emission/Binding/Recipe/Discovery 表、FK、SQL
名称、原文和历史记录，不双写、不提供内部兼容 shim，不把旧 AST 伪造为 CrawlResult。
已有 Source 建立 Frontier，旧待运行任务明确失败，需改用新 YAML 重新触发。
新库初始化仍按历史 ledger 顺序建库后追加0010。

升级停 Worker 和所有数据库客户端；已有0.2库必须 seal init --backup <私有备份路径>，
pg_dump / pg_restore --list 校验成功后升级。更旧public.seal_*库会经过历史0009删除Record，
另需 --acknowledge-data-loss；本次不扩大该历史兼容承诺。外部旧SQL继续读历史表，
新SQL消费新视图。备份数据库与 Archive；回滚用匹配0.2代码和备份恢复到隔离库并核验后
切换连接，不能让旧 Worker 写新增版本库。生产升级、切流和恢复另行授权。

## 独立业务验收与证据

完全 fresh 子 agent 仅依据 Issue #17 业务目标在根目录 tests/ 编写真实 CLI/HTTP/PDF/
PostgreSQL 验收，临时数据 work/、工件 artifacts/。不在实现后补写单元测试；CI仅 Ruff
静态检查。应覆盖同URL来源隔离、变化/不变/A→B→A、解析失败后相同bytes重试、错误响应/
挑战页、正文hash/CAS、跨进程CrawlResult、递归新轮/孤立资源、Worker、TTY/Ctrl+C、
崩溃恢复与幂等、旧执行者栅栏、旧库备份恢复、模式/格式边界。实际命令、环境、冻结版本、
数据前提、断言和证据须可重复生成，不包含凭据；本次最终完整端到端运行 `uv run --no-sync python tests/issue17_e2e.py`：427 PASS / 0 FAIL / 0 UNVERIFIED。
工件目录 `artifacts/issue17-20261013-175357-a77b7/` 的 summary.json、assertions.json、
commands.json、oracle.json、各次JSON/TTY回执、fixture请求时序、数据库导出、CAS hashes、
跨进程读取回执与实际备份恢复记录构成证据。失败调试工件原样保留，不代替最终结果。

环境：Python3.12、uv.lock、Crawl4AI0.9.4、Chromium153、独立Docker PostgreSQL17、
loopback合成HTTP/TXT/PDF/JavaScript来源；不使用生产数据库、用户浏览器状态或凭据。
复跑先 `uv sync --frozen` 与 `uv run python -m patchright install chromium`，需要Docker、
pg_dump/pg_restore；脚本每轮新建 work/artifacts 和独立库，Worker崩溃恢复真实等待31秒。
迁移fixture默认固定旧提交04a50e59170dd592b4756ae9c048bd21a87f79b6，可用 --legacy-ref
显式选择其他兼容旧前提；不是用未来HEAD伪装旧环境。固定旧ref的迁移补验另通过22项，工件 `artifacts/issue17-20261013-175737-0b33f/`；
实际不带data-loss确认升级成功，pg_restore及旧字段/原文核对通过。公网真实站点适配、Office/OCR/专用
表单、全站覆盖与生产升级未验证或未支持，427项通过不扩展这些边界。

官方依据：[Deep Crawling](https://docs.crawl4ai.com/core/deep-crawling/)、
[Hooks/Auth](https://docs.crawl4ai.com/advanced/hooks-auth/)、
[CrawlResult](https://docs.crawl4ai.com/api/crawl-result/)、
[PDF](https://docs.crawl4ai.com/advanced/pdf-parsing/)。本次适配依冻结版本源码与真实样本，
文档名称不作为覆盖或保真证明。Issue #16 的旧Document-only血缘问题不扩展到新内容模型。

## PR #14 历史实现与验收结果

[PR #14](https://github.com/nanzhi84/SEAL/pull/14) 收敛了统一入口，删除旧输出及专属测试；
手写运行代码和本次验收脚本均小于 500 行。历史 schema.sql / 0002–0008 无改动，
历史原文、冻结配置和结果不重写。该轮验收只调用四命令与 PostgreSQL，未执行生产迁移。
以下结果是合并时的历史证据。其固定测试及验证脚本现已删除，不作为当前验收入口。

| 节点 | 输入与实际命令 | 实际结果、输出及下游用途 |
| --- | --- | --- |
| Collect / Parser | 九格式、五节点、所有保留 Adapter 的固定合成 HTTP；真实 collect YAML | 467 项 E2E 断言 PASS，SQL 保存文档与失败诊断；覆盖分页、API、iframe、POST nonce、压缩响应，后续用于 Recheck 和查询 |
| Run / Worker / TTY | --enqueue、worker --once、真实 PTY、Ctrl+C、SIGKILL 与真实心跳等待 | 版本复用、变化、不可变绑定、generation/epoch、截止时间、前台与 Worker 恢复、面板及 stdout JSON PASS |
| Migration / Backup | 含真实 CLI Document 与合成旧 Record 的隔离旧库；init 对象清单、备份和确认 | 未知依赖使迁移原子失败；重试升级移除 Record，保留 Document/Run/历史 mode 与第三方队列；备份实际恢复 PASS |
| 独立 Oracle | pg_restore final.dump，再用独立 SQL、Archive 与 canonical content hash 核验 | 1492 项断言 PASS；281 个 Observation、88 个 Version、232 个 Emission 的保存证据通过；无需 Runtime Parser 或 Replay |
| 公网有界样本 | AMAC trustee、教育部单篇通知与其 XLS；Collect → Recheck | 分别 8 / 3 个 Document、完整 JSON envelope 和 XLS 原文通过核验。新闻出版署单篇 PDF 的 robots 不可用，FAILED；全站覆盖一律 UNKNOWN |

本机环境为 Python 3.12.13 / PostgreSQL 17.11，uv.lock 和代码摘要在各工件 summary.json。
固定样本工件位于 `experiments/document-only/artifacts/final-reviewed/`；公网工件位于
`experiments/document-only/artifacts/reviewed-live/`，原文和备份留在对应 work/。verification.json
保存独立 Oracle 的数量、方法、备份/验证代码摘要和范围。该轮 CI 对精确 PR head 重跑固定验收，
上传无凭据合成样本的元数据；[历史 Checks](https://github.com/nanzhi84/SEAL/pull/14/checks) 为合并前结果依据。

历史执行源码可从 [9afc72e 对应实验](https://github.com/nanzhi84/SEAL/tree/9afc72ea28a22e988244b1cea7c7d238f7ccf067/experiments/document-only)
追溯；工件内旧命令只表示当时执行环境。后续由独立子 agent 使用当前四命令验收，来源变化
可能造成新失败，不自动降低 robots 或网络范围约束。历史验收仅证明列出的有界来源与合同，
未验证生产升级、全站覆盖、掉电耐久性、真实站点长期变化或所有分区的全国许可证数据。
