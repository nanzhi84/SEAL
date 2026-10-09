# SEAL V1.1 Runtime 小样本验收执行 Prompt

你是正在 SEAL 仓库内工作的资深 Python 测试与架构工程师。请直接开展**针对已实现 V1.1 Runtime 的小样本验收**，必要时编写轻量回归测试并修复经复现的缺陷。不要只输出计划。

## 目标及边界

- 验证 Source、RecipeVersion、Binding、Run、Observation、BodyBlob、Revision、ProcessingResult 在现有实现中的**实际执行契约**，以及归档、回放、技术错误处理、幂等和并发恢复。
- 这批 Golden Set 仅是确定性的**工程测试 fixtures 和 oracle**，不构建 Evaluation 模块，不做总体正确率/完整率推断，不生成质量评分、发布审批、Activation，也不恢复 V1 Trial。
- 保持三平面逻辑边界和当前技术栈。不要添加新服务、事件总线、沙箱、Agent 或测试框架。优先复用现有 pytest、acceptance 脚本、Scrapy 和 PostgreSQL/Procrastinate 测试设施。尽量不改变生产代码；如发现 P0 缺陷，仅做最小范围可验证修复。保持 V1 的历史设计与验收资料不变。

## 环境识别与基线

1. 只读审查 `plans/v1.1-runtime.md`、ADR-0003、数据库迁移、现有 CLI、测试结构、`scripts/acceptance.sh` 和相关配置。识别实际可用的 `seal run`、`seal export`、`source select`、replay 入口、已有测试数据库隔离方式。文档路径以仓库实际位置为准，禁止凭示例猜测 CLI 参数或 Schema。
2. 先运行仓库已有与 V1.1 相关的最小验收集合作为基线，记录实际结果与耗时，避免重新复制现有 M3 断言。
3. 使用我提供的 `seal-v1.1-runtime-golden-fixtures` 目录（若放在仓库外，先读取其 `README.md`、`golden_expected.json`、`runtime_cases.json`）。先执行 `python verify_pack.py`。它只校验 fixture 包，不代表 SEAL 通过验收。
4. 所有有副作用的测试只连接**独立临时 PostgreSQL 数据库/Schema、独立队列、独立归档目录**，不得使用或清空开发和生产数据。fixture HTTP 服务限定 127.0.0.1。

## Golden Set 工程回归

1. 使用 fixtures 中的 8 份已人工确定答案的 HTML（`G01` 至 `G08`）构造最小可信测试 Recipe 或复用适配的现有 Recipe；测试代码放在已有测试目录。不要将测试专用 Recipe 当成生产来源。按实际 JSON Schema 建立**显式字段映射**，比较 JSON 的核心语义字段（标题、发布日期、更新时间、文号、正文），保留原始期望值且不以系统输出反向生成期望。
2. G03 的发布时间为 `2025-08-01`，更新时间为 `2026-09-20`，不得互换。G05 缺失发布日期，若项目 Schema 要求非空，则应为明确的负向测试，禁止修改 Schema 来制造通过。G06 要验证 UTF-8、HTML 实体与中文标点。对正文只做显式且合理的空白规范化，不容忍丢段、错字或字段张冠李戴。
3. 完成 `runtime_cases.json` 中的 R01 至 R12。优先检查：列表两页发现四条详情；同一 `/changing` URL 上 A→B→A 对应三次有效顺序修订但只有两份不同原文；相同正文不会新增 Revision；gzip 解压后原文 sha256 对齐；503 不伪造正常 JSON；失败先归档原文；Replay 期间没有网络；候选 Binding 可独立运行且不改变默认 Binding；同步和入队结果语义一致。
4. 使用真实 CLI、Scrapy、PostgreSQL、Worker 的端到端路径验证关键用例。不能只直接调用解析函数就标记完整 Runtime 通过。允许单元层辅助定位，但要标记测试层级。对 Worker 强杀与竞争场景优先复用现有 acceptance 的故障注入，不添加新服务。
5. 每个 case 真实判断 `PASS`、`FAIL`、`UNVERIFIED`、`NOT_APPLICABLE`，附证据、失败原因与重现命令。无法运行必须标记 `UNVERIFIED`，禁止跳过后计入 PASS；原始预期与实际 Schema 不兼容时说明原因并将不适用条件明确记录。

## 小规模真实来源冒烟测试（可达且允许时）

挑选 2 至 3 个现有或允许采集的公开来源，覆盖简单 HTML、分页、公告详情，必要时包括 PDF/附件。每个来源限制采集范围，例如最多 10 至 20 个详情、少量重复运行；尊重网站访问政策与频率限制，不绕过登录和反爬、不修改生产 Source。记录入口、最终 URL、HTTP 状态、归档情况、版本血缘、失败类别、解析产物和运行耗时。站点不稳定或无法访问时如实记录，不把网络冒烟测试放进必过 CI。

## 完成与交付

1. 输出一份简短的 `artifacts/acceptance/v1.1-smoke/report.md`，包含测试范围、现有基线、Golden 通过数、Runtime 契约通过数、真实来源冒烟结果、P0/P1 风险和明确的未验证项。
2. 生成可复现命令及必要机器可读的 case 结果文件，确认 `quality_status=not_evaluated`，禁止把 Runtime PASS 描述成内容准确率或业务发布批准。
3. 如果修改了生产代码，逐项写出复现故障、最小修复、回归证据与变更文件。不要为了测试方便修改 V1.1 的职责边界或加重基础设施。
4. 最后给出 V1.1 是否可作为**可靠执行内核**继续开发的结论，并按优先级列出进入 V1.2 前必须解决的问题。若缺少证据，应明确作出“证据不足”判断。
