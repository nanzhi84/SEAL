---
kind: reference
lang: zh
---

# SEAL 全链路执行串联图

本文档记录 SEAL 0.2.0（Document-Only Pipeline）从 CLI 命令调用、配置与不可变绑定、进程隔离与 Scrapy 抓取归档，到多格式统一解析与数据库持久化的全链路纯文本串联图。

---

## 1. 全链路端到端串联图

```text
[CLI 入口 / 外部调度]
cli.py: main()
   │
   └─► cli.py: dispatch(args)
          │
          ├─► seal init ──► migration.py: initialize() ──► PostgreSQL Schema 初始化与升级
          │
          ├─► seal worker ──► queue.py: worker(once) ──► 消费 Procrastinate 持久队列任务
          │      │
          │      ├─► queue.py: recover_stalled() ──► 启动前自愈僵死任务与未入队任务
          │      │      └─► queue.py: recover_unqueued() ──► 咨询锁防竞争，补发现场未入队 Run
          │      │
          │      └─► procrastinate.App: run_worker(queues=["seal", "seal-control"])
          │             ├─► [seal-control 队列 (Cron: * * * * *)]
          │             │      └─► queue.py: recover_stalled() ──► 巡检心跳超 30s 僵死任务并触发重试
          │             │
          │             └─► [seal 业务队列]
          │                    └─► queue.py: crawl_job(run_id) ──► runs.py: execute_run(run_id)
          │
          └─► seal collect / recheck (source.yaml)
                 │
                 ├─► migration.py: require_current() ──► 校验数据库迁移已是最新版本
                 │
                 ├─► [阶段 1: 配置解析与不可变绑定]
                 │   configuration.py: prepare_run(source_path, mode, enqueue)
                 │      │
                 │      ├─► config.py: load_file() ──► Pydantic 校验 CollectionConfig (预算/域名/种子/公开范围)
                 │      ├─► recipes.py: pack_recipe(recipe_path)
                 │      │      ├─► 静态 AST 检查 (禁止包含 requests/urllib/socket/subprocess 等外部网络库)
                 │      │      └─► 打包 Spider 代码并计算 recipe_version ──► 写入本地 .seal/packages/ 对象库
                 │      ├─► recipes.py: load_bundle() ──► jsonschema.validate(params, params_schema)
                 │      ├─► core.py: digest() ──► 计算 config+params+recipe_version 的 SHA-256 fingerprint
                 │      ├─► db.py: connect() ──► INSERT INTO seal.source (自动注册/更新 generation)
                 │      ├─► db.py: connect() ──► INSERT INTO seal.binding (不可变绑定记录，Trigger 保护)
                 │      └─► runs.py: create_run(binding_id, mode)
                 │             ├─► 检查源状态: 校验 source.paused 与 cooldown_until 冷却限制
                 │             ├─► 种子生成分流:
                 │             │      ├─► 若为 collect 模式: seeds = source.config.entry_urls
                 │             │      └─► 若为 recheck 模式: recheck.py: plan_recheck() (规划存量文档探测种子与配额)
                 │             ├─► 递增 source.run_seq
                 │             └─► INSERT INTO seal.run (status='pending', 写入 seeds, deadline, recheck_plan)
                 │
                 ├─► 若指定 --enqueue ──► queue.py: enqueue(c, run_id) ──► 事务内 defer_job 原子入队，返回 pending 回执
                 │
                 └─► 本地前台执行: runs.py: execute_run(run_id, progress=ProgressPanel)
                        │
                        ├─► db.py: pg_try_advisory_lock() ──► 获取会话级咨询锁 (防同一 Run 多端并发抢占)
                        │
                        └─► runs.py: _execute_attempt(run_id, progress) [尝试循环，支持 retryable 瞬态故障重试]
                               │
                               ├─► runs.py: begin_attempt()
                               │      ├─► 递增 attempt_epoch，归档上一轮 inputs 与 errors 历史
                               │      └─► UPDATE seal.run SET status='running', attempt_epoch=epoch
                               │
                               ├─► subprocess.Popen 独立进程 ──► [阶段 2: Scrapy 隔离沙箱抓取、归档与实时解析]
                               │   │  命令: python -m seal.crawl <run_id> <epoch> <parent_pid>
                               │   │  配置: stdout/stderr=DEVNULL (杜绝源数据泄露), start_new_session=True (独立进程组)
                               │   │
                               │   └─► crawl.py: main()
                               │          │
                               │          ├─► crawl.py: watch_parent(parent_pid) (后台守护线程，父进程消亡则 killpg 强制自杀防孤儿)
                               │          ├─► runs.py: run_context(run_id, epoch) ──► 从 DB 聚合读取只读 Run + Binding 配置快照
                               │          ├─► recipes.py: import_recipe(recipe_version) ──► 动态加载 Spider 类
                               │          ├─► 安全与合规门禁: 检查 seeded_robots_required，禁止 recipe.custom_settings 覆盖系统策略
                               │          ├─► crawl.py: settings_for(context) ──► 装配 Scrapy 中间件、Pipeline 与 Extension
                               │          │
                               │          └─► Scrapy CrawlerProcess.crawl(recipe) 启动 Twisted Reactor
                               │                 │
                               │                 ├─► [Downloader Middlewares 请求发出链路 (Request 阶段，从小到大)]
                               │                 │      ├─► archive.py: RequestGuard (40) ──► URL 规范化、Scope 检查、防 SSRF、敏感参数脱敏
                               │                 │      ├─► robots.py: ArchivedRobotsMiddleware (45) ──► 拦截并检查 robots.txt，归档规则原文
                               │                 │      ├─► inputs.py: InputMiddleware (575) ──► 前置上下文准备
                               │                 │      └─► archive.py: ResponseArchiveMiddleware (605) ──► 发出网络请求并等待响应
                               │                 │
                               │                 ├─► [Spider Middlewares 拓扑发现链路]
                               │                 │      └─► discovery.py: DiscoverySpiderMiddleware (200)
                               │                 │             ├─► 拦截 Spider yield 的新请求 Request
                               │                 │             └─► discovery.py: ResourceFingerprinter ──► 规范化过滤去重与深度控制
                               │                 │
                               │                 ├─► [Item Pipeline 数据过滤链路]
                               │                 │      └─► items.py: ItemPipeline (100)
                               │                 │             └─► 丢弃非 diagnostic 的数据 Item (Runtime 独占内容解析权)
                               │                 │
                               │                 ├─► [Downloader Middlewares 响应流入链路 (Response 阶段，从大到小)]
                               │                 │      │
                               │                 │      ├─► 1. 响应体物理归档落盘:
                               │                 │      │   archive.py: ResponseArchiveMiddleware (605)
                               │                 │      │      ├─► 计算 response.body 的 SHA-256 哈希
                               │                 │      │      ├─► core.py: Objects().put() ──► 写入本地 .seal/ 磁盘对象库 (CAS)
                               │                 │      │      └─► INSERT INTO seal.fetch_observation (记录网络观测证据与 chain_id)
                               │                 │      │
                               │                 │      └─► 2. 实时文档解析与暂存:
                               │                 │          inputs.py: InputMiddleware (575) ──► process_content()
                               │                 │             │
                               │                 │             ├─► inputs.py: register_input() ──► 写入 seal.run.inputs 追踪血缘
                               │                 │             │
                               │                 │             └─► 若 is_content: documents.py: stage_document(context, entry, options)
                               │                 │                    │
                               │                 │                    ├─► documents.py: parse_snapshot(snapshot_id, observation_id, options)
                               │                 │                    │      ├─► 从磁盘 CAS 取出原始二进制: Objects().get(body_hash)
                               │                 │                    │      │
                               │                 │                    │      └─► document_parser.py: parse_document(snapshot, body, options)
                               │                 │                    │             ├─► 纯函数多格式格式识别与清洗 (HTML/PDF/Office/CSV/TXT/JSON/Img)
                               │                 │                    │             └─► 统一投影为 5 类 AST 节点: [article, image, table, json, document_ref]
                               │                 │                    │
                               │                 │                    ├─► documents.py: validate_content() ──► 严格校验 AST 契约
                               │                 │                    ├─► documents.py: _identity() ──► 解析 document_ref 外链并绑定 target_document_id
                               │                 │                    ├─► 计算 processing_key ──► 纯函数确定性断言与版本去重
                               │                 │                    ├─► INSERT INTO seal.document_version (仅生成新版本时写入 AST 内容)
                               │                 │                    └─► INSERT INTO seal.document_emission (记录发射并标注 change_kind)
                               │                 │
                               │                 └─► [Extension 阶段收敛链路]
                               │                        └─► crawl.py: Completion (100)
                               │                               └─► 捕获 spider_closed 信号
                               │                                      ├─► 收集状态与性能指标 stats (剔除敏感 URL/Body)
                               │                                      └─► UPDATE seal.run SET status='finishing', report=...
                               │
                               ├─► [主进程 Supervisor 巡检与看护]
                               │   │  每 200ms 执行无锁快照轮询 (while process.poll() is None):
                               │   │  ├─► 超时熔断 (单调时钟 > budget.seconds + 15 或达到 deadline)
                               │   │  ├─► 源状态核验 (检查 Source 是否被管理员暂停或冷却)
                               │   │  └─► progress.update(run_id) ──► 驱动 TTY 实时终端面板
                               │   │
                               │   └─► finally 进程组强制回收:
                               │          └─► 若子进程未退出: killpg(SIGTERM) ──等待3s──> killpg(SIGKILL)
                               │
                               └─► 子进程退出 (Exit 0 触发 process.poll() != None) ──► [阶段 3: 收尾完整性审计与终态提交]
                                      │
                                      └─► 主进程状态机感知 status='finishing' ──► completion.py: finish_run(run_id, epoch)
                                             │
                                             ├─► db.py: locked_run() 加行级锁，db.py: fenced() 检查代际与配置栅栏
                                             │
                                             ├─► 抓取完整性审计:
                                             │      ├─► 种子入口 coverage 校验 (未全部访问则记录 unresolved_seed)
                                             │      ├─► 观测链路审计 (检查 4xx/5xx/429/截断重定向，豁免 robots/可选条目)
                                             │      ├─► 抓取终态审计 (确认 Scrapy 属于 finished 正常结束)
                                             │      └─► CAS 完整性哈希校验 (验证磁盘 .seal 中 body_hash 文件完整性)
                                             │
                                             ├─► documents.py: finish_documents(c, source, run)
                                             │      ├─► 聚合本轮 seal.document_emission 发射记录
                                             │      ├─► 校验文档版本冲突 (document_identity_conflict) 与过期输入 (stale_document_input)
                                             │      └─► 冻结生成合法文档指针 outputs 列表
                                             │
                                             ├─► 文档发射覆盖审计: 检查所有 is_content 记录是否均已发射 (防解析遗漏 document_parse_missing)
                                             │
                                             ├─► completion.py: completion_status() ──► 评估终态: complete / retryable / failed
                                             ├─► manifest.py: terminal_report() ──► 生成包含审计证据的终端报告
                                             │
                                             ├─► [分支 A: status == 'complete' 成功提交]
                                             │      ├─► documents.py: accept_documents(c, source, run, outputs)
                                             │      │      └─► 原子推进最新指针: UPDATE seal.document SET latest_document_version = ...
                                             │      └─► recheck.py: complete_dates() ──► 推进存量文档 next_check 复查时间与重置失败计数
                                             │
                                             ├─► [分支 B: status != 'complete' 失败/可重试处理]
                                             │      └─► recheck.py: failed_dates() ──► 记录文档失败次数与指数退避重试时间
                                             │
                                             ├─► UPDATE seal.run SET status=%s, report=%s, errors=%s, completed_at=now()
                                             │
                                             └─► cli.py: main()
                                                    ├─► stdout 输出单份最终 JSON 回执 (receipt)
                                                    └─► 退出码返回: 0 成功 / 1 业务失败 / 2 参数错误 / 130 用户中断
```

---

## 2. 核心模块与代码职责对应表

| 模块阶段 | 对应文件 | 核心职责与关键符号 |
| :--- | :--- | :--- |
| **CLI 调度** | [src/seal/cli.py](file:///Users/yoryon/SEAL/src/seal/cli.py) | [main](file:///Users/yoryon/SEAL/src/seal/cli.py#L69)、[dispatch](file:///Users/yoryon/SEAL/src/seal/cli.py#L33)：解析 4 个操作员命令并输出 JSON 回执 |
| **配置与绑定** | [src/seal/configuration.py](file:///Users/yoryon/SEAL/src/seal/configuration.py) | [prepare_run](file:///Users/yoryon/SEAL/src/seal/configuration.py#L13)：加载 Source、校验 Recipe 参数、生成唯一 Binding 与 Run |
| **Recipe 管理** | [src/seal/recipes.py](file:///Users/yoryon/SEAL/src/seal/recipes.py) | [pack_recipe](file:///Users/yoryon/SEAL/src/seal/recipes.py#L49)、[import_recipe](file:///Users/yoryon/SEAL/src/seal/recipes.py#L117)：静态 AST 检查、打包 Bundle 及动态导入 Spider |
| **生命周期监管** | [src/seal/runs.py](file:///Users/yoryon/SEAL/src/seal/runs.py) | [execute_run](file:///Users/yoryon/SEAL/src/seal/runs.py#L247)、[_execute_attempt](file:///Users/yoryon/SEAL/src/seal/runs.py#L154)：咨询锁加锁、子进程拉起与心跳熔断 |
| **抓取沙箱** | [src/seal/crawl.py](file:///Users/yoryon/SEAL/src/seal/crawl.py) | [settings_for](file:///Users/yoryon/SEAL/src/seal/crawl.py#L20)、[Completion](file:///Users/yoryon/SEAL/src/seal/crawl.py#L70)：装配 Scrapy 中间件、捕获关闭信号并更新状态 |
| **网络归档** | [src/seal/archive.py](file:///Users/yoryon/SEAL/src/seal/archive.py) | [RequestGuard](file:///Users/yoryon/SEAL/src/seal/archive.py#L218)、[ResponseArchiveMiddleware](file:///Users/yoryon/SEAL/src/seal/archive.py#L248)：作用域边界拦截与响应体 CAS 磁盘归档 |
| **发现与去重** | [src/seal/discovery.py](file:///Users/yoryon/SEAL/src/seal/discovery.py) | [ResourceFingerprinter](file:///Users/yoryon/SEAL/src/seal/discovery.py#L15)：请求规范化去重与预算控制 |
| **格式解析器** | [src/seal/document_parser.py](file:///Users/yoryon/SEAL/src/seal/document_parser.py) | [parse_document](file:///Users/yoryon/SEAL/src/seal/document_parser.py)：纯函数式将多格式字节流投影为 5 类统一 AST 节点 |
| **文档版本管理** | [src/seal/documents.py](file:///Users/yoryon/SEAL/src/seal/documents.py) | [validate_content](file:///Users/yoryon/SEAL/src/seal/documents.py#L11)、[stage_document](file:///Users/yoryon/SEAL/src/seal/documents.py#L139)：AST 结构校验、`document_ref` 绑定与版本写入 |
| **终态审计** | [src/seal/completion.py](file:///Users/yoryon/SEAL/src/seal/completion.py) | [finish_run](file:///Users/yoryon/SEAL/src/seal/completion.py#L26)、[completion_status](file:///Users/yoryon/SEAL/src/seal/completion.py#L11)：种子覆盖审计、错误分类及重试判定 |
