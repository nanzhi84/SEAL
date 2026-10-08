---
kind: adr
lang: zh
---

# ADR-0002：可信 Python Recipe、Scrapy 原生执行与轻量 V1

- 日期：2026-10-08
- 状态：修订后的 V1 设计决策；尚未实现，关键集成必须通过 M0 PoC
- 取代：[ADR-0001](0001-recipe-driven-fixed-pipeline.md) 的声明式 Recipe、算子执行器和自研任务队列；同时替换本 ADR 前版的沙箱/可信 Fetcher 推荐
- 规范合同：[V1 Plan](../plans/v1-information-pipeline.md)；派生概览：[Generated](../generated/v1-overview.html)

## 背景与决策范围

V1 的 Recipe 由团队开发者编写、审查和维护，不执行未经人工审查的 Agent 生成代码，也不接受外部用户任意 Python。普通 Python 能直接表达分支、循环、分页、附件引用和共享 helper。当前业务需要可靠采集、历史追溯、有限 Trial、人工审核与故障修复，不需要一个执行不可信代码的平台。

仓库有独立采集实验，但没有生产管线或生产数据迁移。本修订不是以安全降级绕过现有线上约束，而是纠正未实施设计中的信任假设。实验的局部解析成绩和无有效配对的框架对比，不能当作本架构已经验收的依据。

## 决策

选择 **Scrapy + 可信 Python Recipe + Procrastinate + PostgreSQL + 轻量原文归档**。

1. Recipe 是普通 `scrapy.Spider` 和 Python helper，可在普通 Python 进程导入执行。不建设 gVisor、容器沙箱、专用隔离环境或 Recipe 网络权限平台。
2. 使用 Scrapy 原生 HTTP(S) Downloader、Scheduler、Request/Response、Retry、Redirect、Cookies、HttpCompression、AutoThrottle、Stats、Item Pipeline 和 Extensions。不替换 Download Handler，不增加可信 Fetcher、代理或 RPC。
3. 仅增加轻量 ResponseArchiveMiddleware：响应解压后、跳转/重试消费前持久化应用层 body 和元数据；写入成功才交给 Spider。归档失败阻断 Run 发布。
4. Procrastinate 管理持久 Crawl 任务、周期唤醒、执行锁和有限重跑；Scrapy 仅管理一次 Crawl 的请求。PostgreSQL URL Frontier、自研任务领取器、第二套 Workflow Engine 均不引入。
5. SEAL 保留 Source、不可变 RecipeVersion、独立 Binding/Activation、获取观察、内容修订、ProcessingResult、审核和固定发布门禁。它们是同一应用里的记录和函数，不拆成服务。
6. 初次接入、升级与异常修复需要 Trial/人工审核；已启用范围内的正常周期通过固定门禁即可自动发布。错误发布通过独立 withdraw 撤回，回滚不删除历史。
7. 管理入口仅 CLI/YAML，原文先用内容寻址本地目录；不要求容器、Redis、对象存储产品、管理页面或管理 HTTP API。

完整字段、函数职责、架构/时序图、M0–M3 与验收工件以 Plan 为唯一规范；本 ADR 不复制另一套计划。

## 原 Fetcher 职责的归属

| 原职责 | 新承担者 | 明确限制 |
| --- | --- | --- |
| 网络请求/超时/TLS/响应大小 | Scrapy 默认 Downloader/Handler | 无额外 HTTP 客户端；真实下载及解压限额待 PoC |
| 调度/请求去重/重试/跳转 | Scheduler、DupeFilter、Retry/Redirect/MetaRefresh | 只在 Crawl 内；不包成每 URL 持久任务 |
| Cookie/压缩/礼貌采集 | Cookies、HttpCompression、延迟/并发/AutoThrottle | 默认不保存会话；不提供恶意 Python 防护或跨 Crawl 总限流 |
| 应用层原文与获取历史 | ArchiveMiddleware + 内容寻址目录 + Observation | 是可复现输入，不是传输层取证或签名收据 |
| 历史解析输入 | 轻量 ReplayMiddleware 返回标准 Response | 缺失即失败，不下载补齐，不重放网络会话 |
| 访问范围/预算 | Source 配置映射原生设置，少量防误用检查 | 可信代码协作约定；不承诺 DNS 重绑定/内网访问的强制拦截 |
| 持久任务/恢复 | Procrastinate + 业务幂等/fencing | 整次有界重跑，不恢复 Python 栈 |
| 审核/修订/发布 | SEAL 应用函数、数据库约束 | 固定业务规则，非 Evaluation/Promotion 平台 |

## 关键取舍

### 原生下载与归档顺序

[Downloader Middleware](https://docs.scrapy.org/en/latest/topics/downloader-middleware.html) 的请求链低优先级先执行，响应链高优先级先执行；响应处理返回 Request 会短路后续链。官方 [默认优先级](https://docs.scrapy.org/en/latest/topics/settings.html#downloader-middlewares-base) 中 Redirect=600 高于 HttpCompression=590，不能随便插一个低优先级归档器。

设计将原生 HttpCompression 调到 610，Archive 设为 605，其后为 Redirect=600、MetaRefresh=580、Retry=550。这样完整 3xx/重试状态先完成 HTTP 解压和归档，再交原生流程决定重发。HttpError 是后续 Spider Middleware，不应让 404 原文从归档中消失。M0 验证实际版本顺序、大小限制和异常传播，不仅检查设置字典。

[HttpCompression 源码](https://github.com/scrapy/scrapy/blob/master/scrapy/downloadermiddlewares/httpcompression.py) 显示解压会改变 body、Content-Encoding 和 Response 类型。保存处理后的 body/头/encoding，不能把压缩前 Content-Length 或 `.text` 重新编码当作收到的同一字节序列。解压失败/截断没有完整可回放 Response，按失败处理。

[Middleware manager 源码](https://github.com/scrapy/scrapy/blob/master/scrapy/core/downloader/middleware.py) 与文档说明 `process_response` 异常如何转交 `process_exception` 受版本/配置影响。Archive 必须自身记录失败并由完成门禁兜底，不能只依赖通用 errback 或把磁盘故障交给网络重试。

### 不把缓存、归档与获取历史混成一件事

[HttpCache](https://docs.scrapy.org/en/latest/topics/downloader-middleware.html#httpcachemiddleware) 默认 DummyPolicy 不重新验证远端；[源码](https://github.com/scrapy/scrapy/blob/master/scrapy/downloadermiddlewares/httpcache.py) 显示 RFC 缓存可把 304 或网络错误替换成旧响应。只在 605 观察到 200/`cached` 无法完整判断本轮是否访问了来源。

因此生产和在线 Trial 默认关闭 HTTP 缓存，不主动使用条件 GET，也不增加“同 Run 冻结响应”下载缓存。整次重跑允许再次获取；归档字节和 ProcessingResult 可以幂等复用，但每次真实观察保留。M0 单独验证缓存命中、错误缓存、304 和错误回退；未建立精确归因之前，不开放生产缓存。意外 304 不变成空正文/新修订。

Replay 只查已存应用层输入清单，由小型 Middleware 直接构造标准 Response，缺失/多义映射明确失败；不走下载、不重试、不二次解压。不为“精确复演网络会话”再写一套 Downloader。其零网络行为是受审代码的验收合同，不是对恶意代码的强制隔离。

### 版本与发布保证保留，安全保证不虚构

相同 body 共用存储，不合并 Source、FetchObservation 或顺序 Revision；A→B→A 保留三次修订。改变 Recipe/参数/依赖只产生新的 ProcessingResult，不伪造来源变化。处理键包括输入元数据，不只包括 body 哈希。

所有影响执行的源码/helper/依赖/环境进入 RecipeVersion/Binding fingerprint；不同 Source 共享版本，但独立 Trial、Review、Activation。环境不原位更新，避免 A 升级悄悄改变 B。Binding 和 generation 在 Run 创建时固定，提交事务检查 attempt epoch、Activation generation、Source Run 顺序与最新 Revision，防止旧任务覆盖新数据。

独立 Schema/locator/少量 Gold Fixtures 能发现错误，但不能证明网站完整性。没有预期文档集合时覆盖率保持 unknown；人工批准的有限范围内可以自动发布，但不能输出虚假的全站覆盖结论。

内容寻址提供去重、复现和意外损坏检测；团队代码与管理员仍能访问应用权限。删除防恶意 Python、强制网络隔离、无生产凭据执行和防篡改原文等承诺。来源响应/依赖仍可能有风险，普通最小权限、输入限制、审查与备份继续执行；它们不等于沙箱。

### Procrastinate 与 Scrapy 并不重复

Procrastinate 的 [有限重试](https://procrastinate.readthedocs.io/en/stable/howto/advanced/retry.html)、[周期任务](https://procrastinate.readthedocs.io/en/stable/howto/advanced/cron.html) 和 [执行锁](https://procrastinate.readthedocs.io/en/stable/howto/advanced/locks.html) 负责业务任务；Scrapy 的内存队列负责同次 Crawl 的请求。这是两个粒度，而不是两个 URL 调度系统。

[外部连接](https://procrastinate.readthedocs.io/en/stable/howto/production/external_connection.html) 允许业务 Run 与 defer 使用同一 psycopg 事务；锁定 connector 后必须验证。无需再建自研 outbox 领取器。业务调度只筛选到期 Source/Document、合并错过时间槽；不重写周期引擎。

[stalled-job 恢复](https://procrastinate.readthedocs.io/en/stable/howto/production/retry_stalled_jobs.html) 需显式配置检测/重试任务，并共享 Run 的总 attempts/截止限制，不能直接用无界循环。heartbeat 缺失不证明旧进程已死，因此数据库 fencing 必需。执行锁和 queueing lock 不同；只阻止重复排队不足以控制实际并发。

[Scrapy JOBDIR](https://docs.scrapy.org/en/latest/topics/jobs.html) 只支持干净停止后的暂停/恢复，非正常终止可能损坏目录，跨版本也不保证兼容。V1 不用它作为崩溃恢复边界。每 Crawl 一个普通子进程是可选生命周期实现，避免 reactor 重启并管理超时；不称为隔离执行环境，不建立复杂进程协议。

## 进一步精简与未选方案

| 方案 | 结论与论证 |
| --- | --- |
| 仅同步 CLI、不用 Procrastinate | M1 可先做；无法覆盖 M2 周期、持久任务和崩溃恢复，长期替代会迫使自研队列 |
| 去掉 Scrapy、手写 HTTP 循环 | 首次简单详情可能短，但分页、去重、跳转、重试很快重复建设；无实证收益不替换 |
| DBOS 等工作流引擎 | 能提供持久工作流/步骤恢复，但 V1 人工审核只是存一条记录后发起新动作，无长期挂起 workflow 需求；不和 Procrastinate 叠加 |
| Crawlee 或浏览器一体化 | 并非能力不足；目前没有浏览器需求，不同时引入另一套队列/存储/执行概念 |
| 沙箱 + Fetcher RPC | 原版为不可信代码准备；当前信任假设不需要，反而抵消原生 Scrapy 的简化收益 |
| Evaluation、Agent Harness、独立管理服务 | 没有当前用户；用 Run 报告、decision、CLI 实现业务，不预留自动代码执行接口 |
| S3/容器/全局分布式限流 | 当前单机本地目录与单 Crawl 执行槽足够；有规模证据后再评估，不预装平台 |

## 历史状态与重新评估条件

**已废弃的前版 ADR-0002 选择：** gVisor/runsc、默认拒绝出站网络、可信 Fetcher 单跳 RPC、自定义 Download Handler、可信响应收据、不可信候选跨进程验证、Fetcher 全局限流/条件缓存，以及这些能力对应的恶意代码验收。它们均未实施，不再是 V1 约束；此段只是历史说明，不要求保留兼容路径。

当 Agent 可以自主执行未经人工审查的 Python，或外部作者可以上传 Recipe 时，必须另立代码隔离与可信执行边界的 ADR，重新评估网络、凭据、文件/租户、依赖与资源隔离；不能直接把人工作者替换为 Agent 并复用当前权限。真实浏览器、OCR、敏感认证来源、跨天工作流或无法分片的大 Crawl 也各自触发范围评估，不自动堆平台。

## 依据与验证边界

本轮核对 Context7 官方 Scrapy `/scrapy/scrapy` 与 Procrastinate `/websites/procrastinate_readthedocs_io_en_stable` 文档，并查看上文链接的 Scrapy 公开源码。latest/master 是可变资料，不是生产依赖锁；M0 必须固定实际 Python/Scrapy/reactor/Procrastinate/psycopg/解析器版本并输出有效配置，不沿用未经集成验收的 release 快照。

许可核查入口保留为实际拟采用组件：[Scrapy BSD-3-Clause](https://github.com/scrapy/scrapy/blob/master/LICENSE)、[Procrastinate MIT](https://github.com/procrastinate-org/procrastinate/blob/main/LICENSE.md)、[Parsel BSD](https://github.com/scrapy/parsel/blob/master/LICENSE)、[lxml](https://github.com/lxml/lxml/blob/master/LICENSE.txt)、[pypdf](https://github.com/py-pdf/pypdf/blob/main/LICENSE)。最终安装还需检查传递/原生依赖与部署许可；本轮未安装、测量性能或出具供应链验收。

必须实证的风险是：归档顺序/异常传播、HTML/PDF 应用层复现、缓存归因、文件/DB 故障窗口、reactor/子进程/heartbeat、Procrastinate 事务与 stalled 恢复、旧任务 fencing、真实来源覆盖与重跑成本。参见 [Plan 风险](../plans/v1-information-pipeline.md#11-必须通过真实-poc-验证的风险) 与 [阶段验收](../plans/v1-information-pipeline.md#9-m0-至-m3-实施顺序)。文档检查不证明这些能力已实现；M0 失败先修正窄接口与合同，不默认恢复独立 Fetcher 或引入另一个复杂框架。
