---
kind: adr
lang: zh
---

# ADR-0003：Runtime、Evaluation、Management 三平面逻辑解耦

- 日期：2026-10-09
- 状态：接受；V1.1 实施与验收结果见 Plan 第 9 节
- 修订：[ADR-0002](0002-python-recipes-minimal-v1.md) 中 Trial/Gold、审核启用和固定发布门禁的职责；保留可信 Python、Scrapy、Procrastinate、归档和模块化单体决策
- 规范合同：[V1.1 Plan](../plans/v1.1-runtime.md)；派生概览：[Generated](../generated/v1.1-runtime.html)

## 背景

V1 已实现 Trial、少量 Gold Comparison、Review、Activation 和发布门禁。它们把执行可用性、样本校验和治理决定放在同一条执行链上。候选只能经 Trial 运行，失败后的 needs_repair 又要求新的审核材料，增加了恢复与配置管理的耦合。

少量固定 Gold 可以证明几个已知例子的行为，却不能证明大规模结果的总体准确性、完整性和覆盖情况。样本若只来自成功输出，还会忽略漏采与失败结果。技术完成、JSON Schema 正确、字段定位匹配原文，都不是业务质量结论。工程合成验收仍必要，但不能作为数据审核机制。

## 决策

SEAL 采用 **Runtime、Evaluation、Management 三平面逻辑解耦架构**。

1. Runtime 执行可信 Recipe，调度、采集、原文归档、解析、持久化、诊断与恢复；保存 Run、Observation、Revision 和 ProcessingResult。只检查基础技术契约。
2. Evaluation 是未来必须建设的独立模块，针对具体候选 Binding 的真实运行结果与不可变原文评估准确性、完整性、覆盖和异常，形成可追溯证据及结论。V1.1 不实现。
3. Management 管 Source、不可变 RecipeVersion、Binding 及版本选择；未来根据 Evaluation 证据与治理规则决定正式激活或发布。V1.1 仅保留 Runtime 所需的配置、默认 Binding 选择和操作控制，不建设 Review、Approval、Activation Gate 或管理平台。

依赖方向：Management 配置 Runtime；Evaluation 读取 Runtime 的稳定运行数据；未来 Management 消费 Evaluation 结论。Runtime 不调用评估，不判断审核资格。三个平面不等于三个微服务，不要求把 Python 模块化单体强行拆开。

## 为什么保留 Binding

RecipeVersion 是不可变代码与环境版本，不属于单个 Source。Binding 固定 Source、RecipeVersion、参数和执行配置，表达一次运行的确定上下文。

同一个 RecipeVersion 可服务多个 Source，而参数、来源版式和数据分布不同；未来评估证据必须绑定确切 Binding，不能从一个来源外推到另一个。Management 未来可独立决定每个 Binding 的正式启用。当前任何合法 Binding 都可通过普通 Run 运行，不必先成为 Source 默认值。

## 为什么只建设 Runtime，并彻底删除 Trial

可靠的归档、版本血缘、幂等和恢复是未来评估的事实基础。现在实施评估平台与审核流程会扩大范围，且无法凭少量固定预期建立可信质量结论。

Trial 没有独立执行语义：它只是审核前置路径。保留一个精简版、改叫 Preflight 或 Validation Run，仍会维持两种执行资格并诱使 Runtime 承担质量职责。因此删除 Trial 模式、CLI、专属流程、Gold 比较及审核依赖，不保留兼容执行层。在线候选与默认 Binding 共用 Runtime；Replay 仅为历史输入解析，不作为审核门禁。

## 未来小样本人工核对

Evaluation 将从具体 Binding/Run 的实际结果总体选样，关联原始来源 URL、不可变原文、解析 JSON、Observation 和执行版本。人工逐字段对照原文，并核对可能遗漏的文档与失败项。

分层随机抽样可覆盖来源、时间、类型和解析分支；风险导向抽样用于异常或高风险子集。评估需保存总体范围、抽样依据、核对结果和不确定性。固定少量 Gold 的通过率不能替代这一结论。本次不新增评估表、抽样服务或审核界面；现有数据关联足以作为未来设计输入。

## 对代码、数据、出口与 Agent 的影响

- 删除 governance 中 Review/Gold/Activation/withdraw 和 publish 的质量/发布业务链；技术完成检查与运行导出独立保留。
- 不再写正式发布事件，不再要求 needs_repair 经人工复核才能运行。默认 Binding 选择仅是调度配置；generation 继续负责操作并发，不代表批准代次。
- JSON 出口是运行结果，标记未评估。V1 发布 JSON 消费者必须显式迁移，不能把新出口当作已审核数据源。
- 不覆盖 V1 文档、历史 ADR、已应用 schema 或验收记录。新增迁移保留历史 Trial、审核 payload、发布记录、结果、修订和原文；旧任务不再恢复执行。数据升级与回退条件以 Plan 为准。
- 工程测试继续验证采集、解析和恢复的外部契约，移除的仅是 Trial/Gold 业务审核测试。
- 未来 Agent 可以生成或修改候选 Recipe，其系统修改权限严格限定 Recipe。代码执行安全与数据质量评估是两件事；可信团队执行假设不能直接扩大到未经审查的 Agent Python。安全准入须另立决策，本次不建沙箱或 Agent 生命周期框架。

## 取舍与后果

Runtime 更小，配置与故障修复不需要伪造审核材料。代价是 V1.1 不提供“已审核/可正式发布”保证；下游只能消费执行事实或自行等待未来治理能力。技术错误仍阻止 Run 完整成功，但部分有效结果与原文保留可供后续评估。

不选择保留 Trial、内嵌简化 Evaluation 或新建三套服务：前两者延续职责混淆，后者增加当前无需承担的基础设施成本。

## 验证与重新评估条件

验收重点为无 Trial 的候选 Binding 完整运行、历史输入 Replay、迁移保留数据，以及真实 Worker 的有界恢复、幂等和 fencing。命令、失败方式和证据见 Plan 第 8–9 节。

需要正式数据消费承诺、规模化人工核对或非可信 Recipe 执行时，分别建设 Evaluation、Management 治理或新的安全边界；不得把本 ADR 的未来规划描述为 V1.1 已交付能力。
