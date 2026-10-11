# SEAL

Self-Evolving Agentic Ingestion Loop：面向异构信息源的持续采集、原文归档和结构化提取项目。

## V1.3：Seeded Site Collection

从业务 Seed 扩展同站点入口、Sitemap、栏目、分页、详情和附件，使用现有 Scrapy
Scheduler 与请求指纹去重。Specific 规则优先，未知 HTML 由同一 Recipe / Record
合同下的 Fallback 解析；正文结构与字段 Locator 从归档原文复算。

手动采集使用持久 PostgreSQL 与私有内容寻址归档。运行前设置 `umask 077`、
`SEAL_DATABASE_URL` 和 `SEAL_ARCHIVE`，再应用自己的 Source 范围；
seeded Source 必须设置 `robots: true` 和 `output_schema: record.v1`。
主机与路径不能由发现链接自动扩大；跨域附件需要明确的 `host_path_scopes`。
受管网络需要平台代理时设置 `SEAL_USE_ENV_PROXY=1`，继承环境代理与 CA。

```sh
uv sync --frozen
uv run --frozen seal db migrate
uv run --frozen seal source apply SOURCE_CONFIG.yaml
uv run --frozen seal recipe pack recipes/seeded
uv run --frozen seal binding create SOURCE_ID --recipe RECIPE_VERSION --params PARAMS.yaml
uv run --frozen seal collect SOURCE_ID --binding BINDING_ID
uv run --frozen seal inspect run RUN_ID
uv run --frozen seal inspect record RECORD_ID
uv run --frozen seal inspect snapshot SNAPSHOT_HASH
uv run --frozen seal export SOURCE_ID --run RUN_ID
```

最小参数文件可以是 `{}`；默认深度 12、同路径查询变体 40，Source 的总请求与时间预算
仍是最终限制。可通过 `specific_rules` 配置同一 Recipe 中的匹配模板，或由可信 Python
Recipe 提供专用 API / 游标规则；不会猜测搜索条件或任意 JSON API 的业务合同。
旧 `seal run`、Worker、Retry、Replay 和版本行为继续复用。
`inspect run` / 回执区分队列耗尽、预算、政策阻塞及部分完成，计数包括 robots 辅助交换。
队列耗尽不能证明全站完整，所有结构化结果仍标记 `not_evaluated`。

seeded Recheck 只处理冻结计划内的目标；页面里的新链接、iframe、附件与 Sitemap
均不扩展，robots 仍作为访问政策辅助输入。直接列入计划的附件目标可以解析。
partial Collect 的 Replay 重用已归档输入与冻结的历史拒绝证据，保留原拒绝原因；
新增请求或缺失的已成功归档输入仍报 `replay_miss`，不会联网补齐。
同一 robots 政策版本的 Sitemap 声明每次 Crawl 只发现一次；gzip Sitemap 受
Source 的 `response_bytes` 解压大小上限约束。政策规则拒绝、政策 HTTP 401/403
与政策不可用分别记录 `robots_denied`、`robots_policy_http_denied`、`robots_unavailable`。
无扩展名附件按最终 Content-Type 确认，保留首次下载的父页面输入；全部引用关系
保留在 Discovery。XLSX 当前保存原文并明确 `unsupported_content_type`。

设计、增量迁移与实际验收见 [V1.3 Plan](docs/plans/v1.3-seeded-site-collection.md)、
[唯一可视化](docs/generated/v1.3-seeded-site-collection.html) 和
[ADR-0004](docs/adr/0004-seeded-collection-native-scrapy.md)。

## V1.2 当前实现：Runtime

**Scrapy + 可信 Python Recipe + Procrastinate + PostgreSQL + 内容寻址原文归档。** 模块化单体，不新增服务。团队维护的 Recipe 使用普通 Python 和原生 Scrapy；不执行未经审查的 Agent 代码或外部任意 Python。

V1.2 增加业务 Record、Discovery、多记录 JSON、静态 iframe 和业务附件。2026-10-09 的真实来源扩展处理全部 111 个 A/B/C 研究入口：98 个在冻结样本范围内 complete、3 个 partial、8 个受阻、2 个最终下载验收失败；比首轮新增 90 个通过来源。每项保留具体取样范围，不代表全站或原地图主体核验业务完成。[V1.2 Plan](docs/plans/v1.2-heterogeneous-sources.md) 集中记录合同、样本替换、运行命令和证据，[可视化](docs/generated/v1.2-heterogeneous-sources.html) 为其唯一派生物。

2026-10-10 已修正 JSON 稳定身份 Recheck、到期记录分批、失败重试日期和重复 JSON 解析；补齐 AMAC 完整 7 页、67 条的来源 Recipe。[上一轮修复验收索引](experiments/v1.2-acceptance/results/review-fixes-final/review-evidence-index.json) 和 Plan 第 9 节保留该版本四个真实来源的复验。

2026-10-10 按新的归档合同移除 Runtime 正文敏感性扫描（功能引擎 `c886007`）。
响应按原始 bytes 归档，再解析 Record；URL/日志脱敏、范围、HTTP 错误、重试、Hash 与
归档故障处理保持原合同。原文必须放在私有目录，运行前设置 `umask 077`，不直接公开。
实验发布隐私检查独立于 Runtime。

PR #2 仅交付 V1.2 Runtime 基础设施及 A/B/C 代表性来源的真实验收。独立 Review
覆盖 URL、请求去重、Recheck、任务幂等性与数据库迁移；发现的两项 P2 已修复并复审关闭：
空/裸敏感查询键在冻结前拒绝，Accept/Accept-Language 协商表示分别请求、归档和解析。
同表示正文变化仍拒绝，包括不同 role、不同重定向入口与 A/B/A 交错请求。

最终冻结引擎 `78cbda7`：联合473/473、公开探测33/33、M3 134/134、Smoke1200/1200、
分页154/154、父请求35/35、Recheck115/115、URL29/29、空查询75/75、错误URL负向22/22。
八个真实来源786/786（A3/B3/C2，118条首轮Record），dd-009补充75/75（29条Record）；
完整分页489/489、T7S323/323。总计3943/3943，T0–T6、T7S、T8全部PASS。
dd-102真实iframe产生41条Record；dd-357正确 `!` URL产生1条，错误 `%21` 记录单独保留。

[最终验收报告](experiments/v1.2-acceptance/results/pr2-merge/report.json)、
[独立 Review](experiments/v1.2-acceptance/results/pr2-merge/independent-review.json)与
[Plan第15节](docs/plans/v1.2-heterogeneous-sources.md#15-独立审查轻量ci与合并交付2026-10-10)
保存源码Hash、状态、命令及证据；原文仅在私有目录。
[URL根因审计](experiments/v1.2-acceptance/results/url-correction/url-audit.json)及旧报告保留为历史。
[GitHub Actions](.github/workflows/ci.yml)固定执行Ruff、core（506项）、offline（278项），
只运行隔离CLI/HTTP/PG端到端回归并上传脱敏元数据。精确PR head三项检查通过后合并。
台账101 adapted中9个在本轮引擎复验，92个历史成功未复验；另有2 partial、7 blocked、
1 acceptance_failed。完整111来源、全站/主体核验及Evaluation批准不属于本次交付。


长期架构分为三个逻辑平面：

- **Runtime**：执行、归档、解析、技术校验、持久化、版本追溯和故障恢复，本次交付。
- **Evaluation**：未来针对具体 Binding 的真实结果与原文独立评估质量，本次未实现。
- **Management**：当前仅配置、不可变版本和默认 Binding 选择；人工审核、正式激活、发布与治理回滚未实现。

V1.1 删除 Trial、Gold Comparison、Review 和发布门禁。候选 Binding 与默认 Binding 使用同一 Run 路径。运行成功不等于数据准确或完整；导出明确标记 `quality_status: not_evaluated`，**不是已审核的业务数据出口**。

## 设计文档与历史

- [版本记录](CHANGELOG.md)：V1.1 / 0.1.1 的变更、升级注意事项、验收结果及复现前提。
- [扩展真实验收工件](experiments/v1.2-acceptance/results/expanded-final/real-evidence-index.json)：111 项处理结果、401 次 Run、8497/8497 验收断言及复跑入口；SAFE 的 9 个含会话标识原文对象仅留本地，公开包明确排除。
- [本轮敏感内容与附件修复工件](experiments/v1.2-acceptance/results/pr2-remediation-final/remediation-evidence-index.json)：精确引擎摘要、联合/M3/Smoke、真实 XLS/DOCX、A/B/C、拒绝边界与历史失败；成功与未复验口径分开。
- [上一轮修复工件](experiments/v1.2-acceptance/results/review-fixes-final/review-evidence-index.json)：该引擎的联合、迁移、Smoke、性能、分页及四个真实来源，保留有效 RED 与验收配置失败。
- [首轮与 Runtime 历史回归工件](experiments/v1.2-acceptance/results/real-final/real-evidence-index.json)：首轮 10 个样本和当时引擎上的合成端到端回归，保留历史成功及失败证据。
- [V1.1 Plan](docs/plans/v1.1-runtime.md)：架构、配置/数据合同、增量迁移、失败方式、验收及未交付能力。
- [ADR-0003](docs/adr/0003-runtime-evaluation-management.md)：正式确立 Runtime、Evaluation、Management 三平面边界。
- [V1.1 可视化](docs/generated/v1.1-runtime.html)：Plan 的唯一单页派生物。
- [V1 Plan](docs/plans/v1-information-pipeline.md)、[ADR-0002](docs/adr/0002-python-recipes-minimal-v1.md)、[V1 可视化](docs/generated/v1-overview.html)：原样保留的历史设计，不是当前 CLI 合同。
- [ADR-0001](docs/adr/0001-recipe-driven-fixed-pipeline.md)：早期历史提案。
- [独立实验](experiments/scraper-benchmark/README.md)、[Source 适配与审计总表](docs/reference/source-adaptation.md)：原始研究及当前逐源验收台账；分类研究不由 Runtime 使用，也不代表业务质量审核。
- [协作准则](AGENTS.md)：文档使用 seiso 管理，每个计划只配一个 Generated。

## 安装与升级

前提：`uv`、Python 3.12.13、PostgreSQL 17。`.python-version` 与 `uv.lock` 固定解释器和依赖，数据库需预先创建。首次初始化账号需要建表/函数权限。

```bash
uv sync --frozen
export SEAL_DATABASE_URL='postgresql://localhost/seal'
export SEAL_ARCHIVE="$PWD/.seal"
uv run --frozen seal db migrate
uv run --frozen seal --help
```

凭据使用环境或 PostgreSQL 标准认证配置，不放 Source YAML、代码或工件。运行账号应独占归档目录；数据库与归档配套备份。

**已有 V1 部署需先停所有 Worker/CLI 写入并备份。** 原 `src/seal/schema.sql` 不改写，新增 `src/seal/migrations/0002_runtime.sql`：

- 保留 V1 历史 Run、Trial、Review/Gold payload、发布记录、Revision、Result 和原文。
- 终止尚未完成的旧任务，防止升级后恢复旧流程。旧 Source 暂停并递增操作代次；旧配置保存到迁移审计。
- 当前 Source 配置不再接受 `expected_urls`；历史 Binding 快照保持不变。
- 在线 Document namespace 改为 runtime，文档 ID 和修订链不变。旧治理列只保留历史数据，当前代码不读写它们。
- 升级后重新打包 Recipe、创建 Binding，执行普通 Run/Replay 并选择默认版本。旧版本包含 V1 引擎摘要，不能冒充在 V1.1 环境原样复现。

V1.2 增量迁移 `0003_records.sql`、`0004_discovery.sql`、`0005_recheck_plan.sql` 新增 Record、Discovery 与复查计划/退避元数据；保留历史结果和原文。更新前停 Worker，迁移后重新 pack Recipe 并创建 Binding。

迁移带摘要登记，可重复执行。旧 checkout/venv 应保留；严格复现 V1 需使用配套的旧数据库副本，不让旧 Worker 连接升级后的库。回退恢复配套备份，不执行破坏性的 down migration。**V1 发布 JSON 消费者必须显式适配运行数据出口，不能继续把它视为已审核数据。**

## 从配置到一次完整运行

复制 [Source 示例](examples/source.yaml) 与 [参数示例](examples/params.yaml)，填写获授权 URL、访问范围、预算及选择器。确认来源允许存档后才设置 `archive_approved: true`；这是存档许可确认，不是解析质量审批。

```bash
uv run --frozen seal source apply source.yaml
uv run --frozen seal recipe pack recipes/generic
uv run --frozen seal binding create public_notices --recipe RECIPE_DIGEST --params params.yaml

# ID 取自 JSON 回执；不要求默认 Binding、Trial 或审核。
uv run --frozen seal run public_notices --binding BINDING_ID
uv run --frozen seal inspect run RUN_ID
uv run --frozen seal export public_notices --run RUN_ID --output run-results.json
```

原文先落盘、提交 Observation 后才交给 Spider；Pipeline 验证 Schema、输入归属与 locator，再保存 Revision 和 ProcessingResult。整个 Crawl 结束后保存技术报告，不生成发布事件。partial 的有效结果也可按 Run 导出；归档损坏条目进入 `unavailable`。原文、Source URL、Binding、RecipeVersion、Run、Revision、Observation 和 Result 均可追溯。

同一个 RecipeVersion 可被多个 Source 的 Binding 复用。修改参数/执行配置需创建新 Binding，不影响旧 Binding 或在途任务。跨 Source 使用 Binding 会拒绝。

## 默认版本、持久任务与恢复

```bash
# 只选择调度默认值，不代表正式启用或发布批准。
uv run --frozen seal source select public_notices --binding BINDING_ID --expect-generation 0
uv run --frozen seal run public_notices --enqueue
uv run --frozen seal worker

# 候选也可以走同一持久队列，不改变默认值。
uv run --frozen seal run public_notices --binding CANDIDATE_BINDING_ID --enqueue
uv run --frozen seal run public_notices --recheck --enqueue
uv run --frozen seal schedule
uv run --frozen seal inspect source public_notices
uv run --frozen seal export public_notices --output latest-runtime-results.json
```

Worker 每分钟检查到期 Source、已知 Record/文档复查和 stalled jobs。自动 Recheck 仅冻结到期目标，按唯一父 API/详情 GET 的请求预算分批；未计划的记录不计缺失。排队不推进 `next_check`，成功完成才推进实际检查目标；失败保留到期日期，并使用独立的 30 秒至 1 小时退避。手动 `--recheck` 即刻复查全部已知范围，超过预算明确返回 `recheck_budget_exceeded`，需使用调度分批或调整冻结预算。默认一个 Crawl 执行槽；Scrapy 只管理单 Crawl 内的请求。Run 与 Procrastinate defer 同事务，不建第二套 URL Frontier。

默认导出汇总每个文档最近成功在线 Run 的结果，包含候选 Binding 的运行，不只包含默认版本。失败的新 Run 不覆盖旧成功视图，报告执行状态和 stale。这是运行便利视图，不是完整性或质量保证；需要确定范围时使用 `--run`。

```bash
uv run --frozen seal pause public_notices --reason '人工维护'
uv run --frozen seal replay HISTORICAL_RUN_ID --binding REPAIRED_BINDING_ID
uv run --frozen seal retry RUN_ID
uv run --frozen seal recover
uv run --frozen seal finish RUN_ID

# 选择新版本或选回旧版本使用同一命令，也可解除暂停。
uv run --frozen seal source select public_notices --binding BINDING_ID --expect-generation N
```

选择版本或暂停递增 generation；CAS 冲突非零退出。候选与默认 Binding 共用 Source 的 run_seq/write_seq、attempt epoch 和 generation fencing，旧进程不得覆盖新状态。暂停与冷却对所有在线 Binding 生效，Replay 不推进在线状态。

每个逻辑 Run 最多三次 attempts，总 deadline 不重置。网络暂时性故障可有限 retry；解析/Schema 错误返回 partial，修复后直接发起新 Run，不需要重新审核。`finish` 仅重入已结束采集的技术提交事务，完成提交后崩溃不重复下载。

## 模块、版本与安全边界

```text
CLI / Procrastinate
  → configuration + recipes：Source / 不可变 RecipeVersion / Binding
  → runs：固定上下文、epoch、期限与进程生命周期
  → crawl：Scrapy 原生下载和回调
  → archive：内容寻址 body / snapshot / Observation
  → items：Schema / 血缘 / Revision / ProcessingResult
  → completion：技术完成报告，无审核发布
  → export / inspect：运行数据、原文引用和版本追溯
```

[示例 Recipe](recipes/generic/recipe.py) 支持分页 HTML、TXT、JSON、文本层 PDF，附件只保存 `not_fetched` 引用。当前身份规则为规范化 URL，输出 Schema 为 `generic_document.v1`。不支持 OCR、浏览器或复杂附件合并。

RecipeVersion 摘要覆盖包内源码/helper/资源、依赖锁、解释器、系统库及 SEAL 引擎/迁移。运行检查环境漂移；升级使用新 checkout/venv，不原位覆盖共享环境。字段 locator 从归档输入重新核对；可选 `transform: date_iso` 做日期规范化，`segments.separator` 连接已定位片段，最终仍严格比较输出。匹配原文或转换正确不证明业务语义正确。V1.3 对显式 `robots: true` 启用可审计的政策请求，seeded Recipe 要求该配置；旧默认 false 保持兼容。业务 URL 的范围、地址和 HTTP 错误检查仍生效。

在线关闭 HTTP 缓存、Cookie、环境代理；Replay 缺失或多义映射明确失败，不下载补齐。Scrapy/中间件和普通子进程不是恶意 Python 隔离设施。内容哈希用于去重和损坏检测，不是防篡改存证。部署使用最小权限和配套备份，不承诺多租户隔离、分布式高可用或传输层取证；孤儿对象暂保留，清理需停机核对引用。

同键同内容的 Record 保留所有结果和输入证据，代表结果优先选择带已验证父请求的候选，按 `(url, method, role)` 字典序及 `output_hash` 决定；到达时间和随机 ID 不参与选择。该规则在本轮候选集合内确定，后续 Recheck 使用选中的父请求；不保证来源页面永久包含该记录。

原文归档前拒绝非空敏感值，包括 `csrfToken`、`xsrfToken`、`authToken` 及对应 snake_case、kebab-case 和大小写变体，JSON/脚本赋值与 HTML 表单共用字段规则。空占位符和 `csrfTokenHint` 等公开辅助字段仍可归档；这不是任意 JavaScript 或混淆内容的完整安全分析。

## 可重复端到端验收

额外要求 `initdb`、`pg_ctl` 在 PATH。脚本覆盖外部业务数据库环境变量，创建独立 PostgreSQL 集群、临时原文目录和仅监听 `127.0.0.1` 的合成站点；结束后销毁测试集群。M2/M3 使用真实 Worker；故障注入只作用于隔离测试数据。

```bash
./experiments/v1.1-runtime-acceptance/acceptance.sh --stage m3 --output artifacts/acceptance/v1.1
uv run --frozen python experiments/v1.1-runtime-acceptance/verify_artifacts.py ACTUAL_OUTPUT_DIRECTORY
uvx --from ruff==0.16.10 ruff check src recipes experiments/v1.1-runtime-acceptance
seiso check
uv run --frozen python experiments/v1.2-acceptance/p1_acceptance.py --output artifacts/acceptance/p1
uv run --frozen python experiments/v1.1-runtime-acceptance/verify_artifacts.py artifacts/acceptance/p1
```

M0–M3 仍为累积入口，但场景已改为 V1.1 Runtime 合同，不再包含业务 Gold Comparison。保留工程独立预期，覆盖原生采集、候选运行、历史迁移/Replay、A→A→B→A、同键异值、版本共享、事务回滚、进程强杀、三层 fencing、有限恢复和敏感信息不落盘。

重用输出目录会创建时间戳子目录，保留失败与历史工件。manifest、断言 expected/actual、CLI 回执、请求账本、合成原文和 JSON 可独立校验；完整行为复验需重跑 E2E。实际结果见 V1.1 Plan 第 9 节。

独立小样本验收复用同一入口，需要仓库内 `experiments/seal-v1.1-runtime-golden-fixtures/` 和本次 M3 工件：

```bash
./experiments/v1.1-runtime-acceptance/acceptance.sh --stage smoke --baseline artifacts/acceptance/v1.1 --output artifacts/acceptance/smoke
# 公开来源仅显式启用；可用 --live-source live_court / live_spp / live_python 缩小范围。
./experiments/v1.1-runtime-acceptance/acceptance.sh --stage smoke --baseline artifacts/acceptance/v1.1 --live --output artifacts/acceptance/smoke-live
uv run --frozen python experiments/v1.1-runtime-acceptance/verify_artifacts.py artifacts/acceptance/smoke --recorded-outcomes
```

`smoke` 校验 M3 与当前 Runtime 的文件摘要一致，逐例保存 PASS/FAIL/UNVERIFIED；存在失败时非零退出。`--recorded-outcomes` 校验工件完整性和断言状态是否如实记录，同时列出失败断言，并不把失败改成通过。外网不加入必须通过的 CI。固定 Golden 的 `update_date/document_no` 当前无对应输出字段；日期和表格转换修复的独立验收见本地 `artifacts/acceptance/v1.1-runtime-fixes/report.md`。原始 `v1.1-smoke` 工件保持不变。

尚未验证真实来源质量/完整性、真实断电持久性或生产规模。Evaluation 的分层/风险抽样和小样本人工核对、Management 的审核/正式发布与治理回滚、Agent 自进化均留待后续版本。
