# SEAL

Self-Evolving Agentic Ingestion Loop：面向异构信息源的持续采集、原文归档和结构化提取项目。

## V1 当前实现

**Scrapy + 可信 Python Recipe + Procrastinate + PostgreSQL + 轻量原文归档。** Recipe 由团队开发者编写、审查和维护，使用普通 Python 分支、循环、分页与共享 helper。V1 不接入 Agent 自动生成并执行未经人工审查的代码，不接受外部用户上传任意 Python。

- **原生采集**：Scrapy 管理单次 Crawl 的请求、下载、重试、跳转和解析回调；Procrastinate 管理持久 Crawl 任务、周期采集与有界重跑，不建立第二套 URL Frontier。
- **轻量归档**：ResponseArchiveMiddleware 在 HTTP 解压之后、Spider 解析之前保存应用层 Response；正文按内容去重，每次有效观察和内容变化历史仍保留。归档失败阻断发布。
- **独立版本与启用**：不可变 RecipeVersion 包含代码、helper 和依赖环境；多个 Source 可共享代码，但各自保留 YAML 参数、Binding、审核与 Activation。
- **业务质量与发布**：首次接入、升级和异常修复经过 Trial、少量 Gold Fixtures、基础校验及人工核对。正常周期通过固定门禁后自动发布 JSON；无预期集合时覆盖率保持未知。
- **恢复与维护**：历史原文 Replay、内容 Revision、ProcessingResult、旧任务提交校验和发布幂等支持诊断与修复。回滚不删除历史，错误结果通过独立撤回处理。

不建设 gVisor/容器沙箱、可信 Fetcher、Fetcher RPC、网络代理、自定义 Download Handler、Agent Harness、Evaluation 平台或管理服务。CLI 与本地内容寻址目录即可起步，不要求容器或额外存储产品。

**信任边界：** Scrapy 设置和中间件是可信代码的工程约束，不能阻止恶意 Python 绕过网络限制或使用同权限存储。内容哈希不是防篡改存证；应用层归档不是网络传输取证。将来开放不可信代码执行时，必须单独引入隔离与可信执行边界。

## 文档与状态

- [V1 Plan](docs/plans/v1-information-pipeline.md)：完整架构、数据流、关键接口、版本/发布规则、M0–M3 与端到端验收合同。
- [ADR-0002](docs/adr/0002-python-recipes-minimal-v1.md)：当前设计决策、删除的复杂度、框架依据及技术风险。
- [可视化设计说明](docs/generated/v1-overview.html)：Plan 的单页派生物。
- [ADR-0001](docs/adr/0001-recipe-driven-fixed-pipeline.md)：已被取代的历史提案，不再约束 V1。
- [独立实验](experiments/scraper-benchmark/README.md)：实验边界、已有工件与未验证项；不等于生产架构已经验收。
- [尽调入口地图](docs/reference/due-diligence-source-map.md)：491 条官方核验入口原始清单，作为逐项实验的来源基线。
- [尽调地图逐项实验](docs/reference/due-diligence-source-experiment.md)：491 条入口结果、失败原因、182 条 golden 记录及零网络复验命令；其中 7 条核对了具体内容字段。
- [协作准则](AGENTS.md)：文档维护规则。

当前已有可运行 CLI、Worker、九张业务表、不可变 Recipe/Binding、审核启用、归档/Replay、修订/发布和 M0–M3 合成端到端验收。真实来源访问许可、人工核对及完整性验收尚未完成；合成站点成绩不等于生产接入结论。

文档使用 [seiso](https://github.com/nanzhi84/seiso) 管理，运行 `seiso check` 检查。文档检查通过不代表框架 PoC、真实来源覆盖或业务验收通过。

## 安装与数据边界

前提：`uv`、Python 3.12.13、PostgreSQL 17。`.python-version` 与 `uv.lock` 固定解释器和完整依赖。数据库需预先创建，首次初始化账号需要建表/函数权限。

```bash
uv sync --frozen
export SEAL_DATABASE_URL='postgresql://localhost/seal'
export SEAL_ARCHIVE="$PWD/.seal"
uv run --frozen seal db migrate
uv run --frozen seal --help
```

`db migrate` 可重复执行，安装 `seal_*` 业务表和 Procrastinate 官方表，不清空已有数据。凭据放环境或 PostgreSQL 标准认证配置，不提交到代码或 Source YAML。运行账号应独占归档目录；数据库与归档需配套备份。

## 注册、审核与发布

从 [Source 示例](examples/source.yaml) 与 [参数示例](examples/params.yaml) 复制配置，填写获授权 URL、允许路径、范围、频率和选择器。确认适合存档后才将 `archive_approved` 改为 `true`。可用 `expected_urls` 声明独立确认的有限文档集合；省略时覆盖率保持 `unknown`。

```bash
uv run --frozen seal source apply source.yaml
uv run --frozen seal recipe pack recipes/generic
uv run --frozen seal binding create public_notices --recipe RECIPE_DIGEST --params params.yaml
uv run --frozen seal trial BINDING_ID
uv run --frozen seal inspect run TRIAL_RUN_ID
uv run --frozen seal review export TRIAL_RUN_ID --output review.json
```

ID 均取自 JSON 回执。人工从来源入口检查分页、原文和字段后，编辑 `review.json`：填写实际检查的 `scope`、设置 `approved: true`，在 `gold` 填独立预期。每个 Gold 对象至少包含 `title` 和 `body`，可加 `url`、`date`。审核人取执行 CLI 的 OS 账号，不信任文件自报身份。

```bash
uv run --frozen seal review import review.json
uv run --frozen seal activate BINDING_ID --expect-generation 0
uv run --frozen seal run public_notices
uv run --frozen seal export public_notices --output published.json
```

未审核、证据不匹配或 generation 冲突均非零退出。YAML 变更不会改变旧 Binding/在途 Run；执行配置变化后创建新 Binding 并重新审核。正常周期合格即可自动发布。导出只读取发布指针，包含原文、版本、时间、覆盖范围、陈旧状态和撤回信息；证据损坏条目进入 `unavailable`。

## 持续运行、恢复和修复

```bash
uv run --frozen seal run public_notices --enqueue
uv run --frozen seal worker
# 常驻 Worker 每分钟自动检查到期来源与 stalled jobs；也可手动触发。
uv run --frozen seal schedule
uv run --frozen seal run public_notices --recheck --enqueue
uv run --frozen seal inspect source public_notices
uv run --frozen seal pause public_notices --reason '人工维护'
uv run --frozen seal replay HISTORICAL_RUN_ID --binding REPAIRED_BINDING_ID
uv run --frozen seal trial REPAIRED_BINDING_ID
```

升级需新 Binding、历史 Replay、在线 Trial 与人工审核；在新审核文件填 `replay_run_id`，再以当前 generation 启用。A/B 独立审核与升级。模板/字段故障置 `needs_repair`，旧 Trial 不能清除新故障；临时下载失败可有限重跑，429 设置冷却。

```bash
uv run --frozen seal retry RUN_ID
uv run --frozen seal recover
uv run --frozen seal finish RUN_ID
uv run --frozen seal rollback OLD_BINDING_ID --expect-generation N --reason '回滚原因'
uv run --frozen seal withdraw RESULT_ID --reason '错误提取及影响说明'
```

`recover` 使用官方 stalled 检测，默认 30 秒 heartbeat 超时；每个逻辑 Run 最多三次 attempts，总 deadline 不重置。`finish` 只重入已结束采集的提交事务。回滚创建新 generation，不撤回错误结果；独立 `withdraw` 后，自动任务不会复活被撤回的 Result。

每次 Crawl 使用独立短生命周期进程；父进程死亡、暂停与 deadline 会终止它。生产日常使用队列，默认一个 Crawl 执行槽；同步 CLI 用于人工操作，重叠提交受 fencing 保护，但不提供跨 Crawl 网络总限流。

## Recipe 和运行边界

[示例 Recipe](recipes/generic/recipe.py) 是普通 Scrapy Spider，支持分页 HTML、TXT、JSON、文本层 PDF，附件仅为 `not_fetched` 引用。PDF 按页提取并保存文本跨度，不支持 OCR/视觉表格；复杂阅读顺序需要人工核对。字段从归档输入重新验证 XPath、字符范围、JSON Pointer 或 PDF 页码/跨度。

Recipe 包由 `recipe.yaml`、源码和包内 helper/资源组成；manifest 声明 family、entrypoint、参数 JSON Schema。打包不运行 hook，拒绝符号链接、常见私密文件与已知非 Scrapy 网络 import；这些检查不能替代代码审查。候选支持有序 `supplementary_inputs`，主资源决定 Revision，全体输入决定 Result。

当前 Source 身份只支持规范化 URL，输出 Schema 为 `generic_document.v1`。RecipeVersion 记录包、依赖锁、解释器、系统库及 SEAL 代码摘要，每次运行检查环境漂移。升级必须保留旧 checkout/虚拟环境，在新目录安装；不要原位覆盖旧环境。Run 按版本保存的 Python 路径启动。

模块边界：`config/recipes/governance` 管准入；`archive/items/crawl` 接入 Scrapy；`runs/publish` 管执行和发布；`queue` 只连接 Procrastinate。生产/Trial 关闭缓存、Cookie、环境代理。Replay 缺失或多义输入立即失败，不下载补齐。孤儿对象暂保留、不自动删除；清理需停机核对数据库引用。

## 端到端验收

额外要求 `initdb`、`pg_ctl` 在 PATH。脚本创建和销毁独立 PostgreSQL 集群、临时原文目录、仅监听 `127.0.0.1` 的合成站点，覆盖传入的业务数据库环境变量；M2/M3 使用真实 Worker。故障注入只影响临时测试库。

```bash
./scripts/acceptance.sh --stage m0 --output artifacts/acceptance/m0
./scripts/acceptance.sh --stage m1 --output artifacts/acceptance/m1
./scripts/acceptance.sh --stage m2 --output artifacts/acceptance/m2
./scripts/acceptance.sh --stage m3 --output artifacts/acceptance/m3
uv run --frozen python scripts/verify_artifacts.py ACTUAL_OUTPUT_DIRECTORY
seiso check
uvx --from ruff==0.16.10 ruff check src recipes scripts
```

后续阶段包含前阶段。重用输出目录会新建带时间戳的子目录，保留首次失败。工件包含 manifest、断言 expected/actual、CLI 回执、请求账本、缓存 PoC、合成原文和报告；manifest 保存命令、环境锁、代码及全部工件摘要。校验脚本重算摘要与断言，完整复验需重跑 E2E。

合成验收不能确认真实来源许可/覆盖、真实断电持久性或生产性能。外部连接事务与 stalled 恢复依据 Procrastinate 的[官方事务接口](https://procrastinate.readthedocs.io/en/stable/howto/production/external_connection.html)和[恢复接口](https://procrastinate.readthedocs.io/en/stable/howto/production/retry_stalled_jobs.html)，以锁定版本实测工件为准。
