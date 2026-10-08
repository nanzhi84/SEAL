# SEAL

Self-Evolving Agentic Ingestion Loop：面向异构信息源的持续采集、原文归档和结构化提取项目。

## V1 当前设计

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
- [协作准则](AGENTS.md)：文档维护规则。

当前是设计和独立实验阶段，没有生产应用、生产依赖锁或数据库迁移。Plan 中 `seal` CLI 和阶段验收脚本均为待实现合同；本轮不实现业务代码、安装依赖或搭建基础设施。

文档使用 [seiso](https://github.com/nanzhi84/seiso) 管理，运行 `seiso check` 检查。文档检查通过不代表框架 PoC、真实来源覆盖或业务验收通过。
