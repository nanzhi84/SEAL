---
kind: plan
lang: zh
---

# 全局不可变 Snapshot 与手动发布（Issue #23）

业务合同来自 [Issue #23](https://github.com/nanzhi84/SEAL/issues/23)，
[唯一可视化](../generated/global-snapshots.html) 随本计划更新。
本合同替换 [结果服务 Plan](results-api.md) 的内部当前版本、水位分页与重启撤权行为；
HTTP 路由、认证、字段白名单、归档检查和资源限制继续沿用。
当前状态：实现完成，fresh 独立端到端验收411 PASS / 0 FAIL；未部署生产。

## 目标、职责与数据流

```text
collect / recheck / worker → Observation → Version → Resource.current_version_id
  → 操作者 snapshot create → 冻结 Source 入口 + Resource URL + 固定 Version 映射
  → snapshot inspect → 人工核对 → snapshot publish
  → 单一 published_snapshot_id → HTTP 当前许可 → 固定 Snapshot → Version.result_ref
  → S3 完整 CrawlResult / 原始附件 → 原有白名单内容 / 下载字节
```

采集链路不依赖发布模块。Snapshot 只存关系及必要来源元数据，不复制 SDK JSON 或附件。
快照不是采集恢复 checkpoint，也不证明覆盖完整性或业务质量。
init、升级、采集、复查、worker、serve 均不创建或发布快照。
本次不提供删除、对象 GC、自动发布、逐来源发布或质量审核。

## 数据与事务合同

追加 `0013_global_snapshots.sql`，不更改历史 SQL。
历史 schema 并无 `seal.snapshot` 表；旧 snapshot_id 是原文引用字段，保留历史记录。
若外部自建同名表造成冲突，迁移整笔回滚，禁止覆盖。

- `seal.snapshot` 保存 UUID、创建时间、来源/资源/版本数量及 sealed 标志。
- `seal.snapshot_source` 固定 Source id、Frontier 归属与 entry_urls，包括暂无成功版本的来源。
- `seal.snapshot_item` 固定 Source、Frontier、Resource、Version 与公开 Resource URL。
- 复合外键验证来源→Frontier→Resource→Version 的真实归属，阻止映射错配及引用被删除。
- `seal.publication` 只有一个 singleton 行，初始 published_snapshot_id 为 NULL。
- `seal.snapshot_publication` 以递增 sequence 保存切换前后 ID 和实际切换时间。

创建在单一 REPEATABLE READ 事务中执行，不锁采集资源、不等待在途 Run。
固定首个数据库视图中的全部来源和已提交 current_version_id；未成功资源不入映射，
最近复查失败仍采用之前成功版本。事务内先写映射和统计再 sealed，事务外从未暴露草稿。
数据库延迟约束禁止未 sealed 快照提交；sealed 后标题、来源、映射和发布记录禁止增删改。
映射写入与封存通过快照行锁串行，统计与实际映射必须一致。任何失败回滚整个创建。

发布先校验快照已封存及其结果和附件可读且哈希正确，只读取既有对象。
然后锁唯一 publication 行，原子更新指针并追加日志；相同 ID 幂等，无重复日志。
日志时间在取得行锁后生成，sequence 与真实切换顺序一致。并发发布串行化；
无效 ID、坏对象、数据库或 S3 故障均不改指针。归档此后仍可能损坏，API 每次独立检查。
校验不构成业务质量验收；只能由显式 publish 命令切换。空快照无引用对象，无归档读取。

## CLI 与有界输出

```sh
seal snapshot create
seal snapshot list --limit 20 --after '<上一页最后 ID>'
seal snapshot inspect '<snapshot_id>' --limit 20 --after '<上一页最后 Resource ID>'
seal snapshot publish '<snapshot_id>'
```

create 返回 snapshot_id、时间与三个统计，不隐含发布。
list 按 id 的 C 字节序分页，inspect 按 Resource.id 分页；limit 为1–100，默认20。
inspect 来源同样独立分页（`--source-after`），固定映射与来源入口供人工核对。
返回 published_snapshot_id、items/sources 及 next_after/source_next_after，不输出完整 Source
配置、凭据或原始结果。来源入口总大小和单页 JSON 设上限；不存在/格式非法 ID 明确报错。
publish 返回 before_snapshot_id、published_snapshot_id、changed 与 published_at。
重复发布不产生采集、快照、映射、对象副本或额外切换日志。

## HTTP、分页、附件与当前权限

新请求在同一只读事务读取一次发布指针。无发布时，认证通过后的三个数据接口统一503
snapshot_not_published，不回退内部数据。发布空快照后 /sources 和 /results 返回正常空集合；
空快照中未登记入口仍404 source_not_found。已有但无成功资源的来源列出入口且结果为 []。
歧义判定只用所选快照的全部入口，/sources 排除歧义入口，/results 为409 source_ambiguous。

/sources 使用 Snapshot 来源，/results 使用固定映射；每次许可来自当前私有配置。
服务每个请求重读并严格校验配置，仅允许 callers 热更新；token_key、服务URL、限额等
启动合同变化需重启，未重启时503 invalid_api_config。配置原子替换即可撤权，
无来源许可403；当前 Key 被移除则401。无效配置拒绝请求，不继续用旧许可。

游标 Fernet 认证加密绑定用途、Snapshot、调用方、Source、最后 Resource.id 与首次
issued_at；3600秒期限不因翻页延长。后续翻页解析快照内原入口及映射，不重读当前指针
选择内容；发布或回切后仍沿原快照，权限实时验证。跨调用方、入口、快照篡改拒绝。

附件令牌绑定用途、Snapshot、调用方、Source、Resource、Version、附件索引；有效86400秒。
下载只认该快照真实成员，实时授权并复算结果及附件哈希；切换发布不使旧合法链接失效。
不加入新的公开响应字段。旧内部水位游标与旧附件令牌不兼容，无双读或 shim。

## 升级、保留与回滚

0012 的已有内部内容、来源、历史表、对象均保留。0013 是追加迁移，可对有内容库执行
`seal init`；0012 及更早的归档合同升级仍保留原空内容约束。
升级后指针为空，操作者须 create → inspect → publish 才对外提供内容。
部署前停服务及 worker，升级程序与 schema 后再启动；服务启动不代做迁移或发布。
生产升级按现有运维要求备份；日常隔离库不强制额外备份。
回滚先停止新服务；可回退旧代码并保留0013追加表和对象（旧程序只检查0012已登记）。
如需恢复升级前 schema，使用运维保存的升级前副本；不得删除 Snapshot 引用对象或版本。
旧 API 会暴露内部当前版本，回滚前须确认这一发布语义变化。

## 实施与验收

先完成本合同，再实现迁移与操作模块、CLI、API 查询/令牌和实时许可，更新 README 两图、
CHANGELOG 和本可视化。按 [AGENTS](../../AGENTS.md) 由完全 fresh 子 agent 只根据业务目标
在根 tests/ 编写独立验收，隔离 PostgreSQL17 与 MinIO，通过真实 CLI、HTTP、SQL 和 S3。
失败边界在测试代码前冻结；主 agent 不补单元测试。

验收覆盖 Issue 九项：初始化/升级不发布；A创建与发布；内部变化和来源漂移；B与回切；
历史游标/附件；即时撤权和篡改；一致创建与并发/幂等/失败发布；空/歧义/坏归档；
SQL、HTTP、原始附件字节及无复制/无采集写入对照。
根 work/ 与 artifacts/ 保存命令、前提、断言、映射、发布日志、对象清单与哈希，均不入库。
静态检查使用 CI Ruff 命令和 `seiso check`。生产部署与负载不在本次验收范围。

### 最终独立验收（2026-10-14）

完全 fresh 子 agent 依据业务目标独立编写验收；最终完整运行411 PASS / 0 FAIL、退出0。
使用真实 CLI 采集 HTML/PDF、暂停中的采集、queued collect → worker --once，以及
SELECT/默认只读数据库与仅 S3 Get/List 的 HTTP 服务；同时用合成持久输入覆盖全部边界。
升级使用前一版 `994c8adfebbe9facdd3e74a964f89d32e91fe74d` 的实际 init/collect，
再以本版 init 升级，逐行对照历史字段、S3对象和原始PDF字节，不以手造旧 schema 代替。

```sh
uv sync --frozen
uv run --no-sync python tests/issue23_e2e.py
uvx --from ruff==0.16.10 ruff check src
uvx --from ruff==0.16.10 ruff format --check src
seiso check
```

环境与数据前提：Python3.12、Docker、`postgres:17-alpine`、
`seal-minio:RELEASE.2025-10-15T17-29-55Z` 及
`minio/mc@sha256:a7fe349ef4bd8521fb8497f55c6042871b2ae640607cf99d9bede5e9bdf11727`。
Git对象库须包含上述旧提交；数据、端口与凭据自动随机生成，不读取现有本地库、桶或
root minio/。当前测试源码入库，运行数据和工件不入库；复跑生成新运行名。

| 业务合同 | 实际结果 |
| --- | --- |
| 初始化/升级，无发布边界 | 不自动创建或发布，三个接口503；空快照及空来源有确定结果 |
| A创建、发布、内部变化与B创建 | A来源/版本/附件与SQL及S3一致；变化、新来源、失败复查和B创建均不改变A |
| B发布及回切A | 新请求对应固定B，回切恢复A；内部较新版本仍保留 |
| 原分页/附件、实时权限与篡改 | 原A分页集合无漏项/重复，原字节跨B及回切保持；即时撤权及跨调用方/成员篡改拒绝 |
| 并发一致性与幂等 | 实际在途采集不等待整轮；8次来源入口/双版本事务并发创建一致；12次并发发布日志与指针一致，无请求混合A/B |
| 异常保护 | 无效ID、对象损坏/缺失与S3停机发布失败，原指针和日志不变；API坏对象报错且不回退内部版本 |
| 无采集/对象副作用 | 创建/发布无S3副本；HTTP全业务表与对象清单前后相同；队列Worker新增成功Version但不创建/发布快照 |

最终工件根目录为 `artifacts/issue23-20261014-150910-8244e0/`：
[汇总](../../artifacts/issue23-20261014-150910-8244e0/summary.json)、
[独立审计](../../artifacts/issue23-20261014-150910-8244e0/audit.json)、
[全部断言](../../artifacts/issue23-20261014-150910-8244e0/assertions.json)、
[命令回执](../../artifacts/issue23-20261014-150910-8244e0/commands.json)、
[环境与文件哈希](../../artifacts/issue23-20261014-150910-8244e0/environment.json)、
[最终SQL映射](../../artifacts/issue23-20261014-150910-8244e0/final-sql-mappings.json)、
[并发创建](../../artifacts/issue23-20261014-150910-8244e0/consistent-concurrent-create.json)、
[发布中HTTP对照](../../artifacts/issue23-20261014-150910-8244e0/http-during-publication.json)、
[发布记录](../../artifacts/issue23-20261014-150910-8244e0/publication-records.json)、
[升级](../../artifacts/issue23-20261014-150910-8244e0/upgrade.json)、
[队列Worker](../../artifacts/issue23-20261014-150910-8244e0/queued-worker.json)。
各 `sql-api-*.json` 保存固定版本、完整公开内容及附件SHA对照。

28个源码、13个迁移、13个测试/辅助输入的受测哈希与最终工作区相同；凭据和不透明
令牌匹配扫描均为0；两个临时容器均已清理。此前失败轮次仅为测试夹具错误，保留历史
工件，不改写原结果。
验收汇总SHA-256：`95168fe9e847510c57dcfbd7f0796fa66d7a08660ca35677d17536e4b896a2ea`；
独立审计SHA-256：`cb216bc2ac7e367795cfbd8f15ff20a6e22a543c55cd211fddaf63e31ca1f3ca`。

本地Ruff、格式、diff检查与seiso全部通过；架构检查未发现新循环依赖或采集→发布依赖。
生产部署、公网来源可用性及生产负载未验证；TTL使用真实签名的过期令牌测试，未等待
完整1小时/24小时壁钟。快照内容仍需人工业务核对，不保证全站覆盖或质量。
