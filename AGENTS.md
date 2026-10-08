# SEAL Agent Rules

文档使用 seiso 进行管理。

## 设计计划文档

- 每次设计计划只包含一个 Plan（`docs/plans/`）和一个 Generated（`docs/generated/`），可选零个或多个 ADR（`docs/adr/`）。
- Plan 集中承载目标、架构、数据与配置合同、实施阶段及验收要求；不再拆成独立的 Recipe、验收或其他配套计划文档。
- Generated 是该 Plan 的可视化派生物，不另建说明文档；ADR 仅记录确有必要单独保留的架构决策与取舍。
- 不为设计计划额外创建 README 或索引页。更新现有计划时同步更新其 Generated 和相关链接。