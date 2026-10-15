---
kind: changelog
lang: zh
---

# 版本记录

## 未发布 / Crawl4AI 0.9.5 — 2026-10-15

依赖从 0.9.4 固定升级至 0.9.5，接入上游 PR #2320 的 PDF 并行页码竞争修复，
SEAL 文字层 processor 同步改为显式接收页码，恢复 SDK 原生页级并行，移除顺序处理覆写；
随后恢复 SDK 原生文字转换，移除单页 `<pre>` 覆写，业务清洗由下游负责。
合同更新为 `crawl4ai_0_9_5_native_v1`，同输入切换合同新增不可变 Version，旧 Snapshot 不变。
接受 SDK 逐页重建 reader，不实施 reader 复用优化。无数据库迁移；回滚须同时还原依赖锁与接口适配。
独立验收：2001 页 CLI 14/14 PASS，SDK 并行 25 页重复 6 次 PASS；补充 CLI 23/27 PASS，
4 个 PDF 注解链接丢失在升级前 0.9.4 基线完全复现，属既有 scraping 层限制，未在本次修复；由 [SEAL #26](https://github.com/nanzhi84/SEAL/issues/26)
跟踪用户提交的 [Crawl4AI #2374](https://github.com/unclecode/crawl4ai/issues/2374)。上述 2001 页为恢复并行前验收。
恢复原生并行后 fresh CLI 验收 37 PASS、4 个既有上游链接失败；覆盖 20 页正文/页序、重复版本、
空白/损坏与 S3 哈希，不声称性能或超过 2000 页验收。

恢复原生文字转换独立验收 36/36 PASS：五页最终 HTML/Markdown、合同切换版本、重复采集、
空白/损坏与原件归档；2001 页运行取消，该边界及生产未验证。

## 未发布 / dd-492 历史文献 PDF 采集 — 2026-10-14

登记越南政府文献门户 `dd-492`，用户授权该来源关闭 robots，并恢复官方 PDF 附件范围。
新增来源专用 ASP.NET 分页发现和可恢复的分批采集入口；列表/详情发现原文只作为临时
验收证据，不另写 MinIO HTML，不伪造 CrawlResult。PDF 原件、完整 SDK 结果与版本引用
继续走现有 SQL/S3 合同。默认遍历到历史末页，本次按用户要求只验收十个列表页全部 PDF。

深度 0 的多入口采集复用 SDK 有界并发。PDF 按实际页面数处理，避免默认页数上限
截断；原始文字层保留数字、章节编号与范围，显式文字合同改变生成新不可变版本，旧 Snapshot 不变。
无文字层、损坏/加密与 SDK 漏页明确失败，不使用 OCR。真实附件超过50MiB，
Source/发布验证/API可配置原件上限统一128MiB，Source默认10MiB、API默认50MiB保留。
该来源最终显式配置128MiB/300秒；HTTP读取遵守Source总超时，去除SDK隐式30秒读限制。
旧验收state继续使用冻结配置；汇总区分最新原件归档、正文错误及预算/网络失败。无迁移或新依赖。
当前验收范围、命令、断点与故障行为见[唯一 Plan](docs/plans/dd-492-pdf-harvest.md)
及[同步可视化](docs/generated/dd-492-pdf-harvest.html)；结果以实际工件为准。
回滚来源配置和新增脚本即可撤销采集方式，不删除已有 SQL/S3 归档。

## 未发布 / 全局不可变 Snapshot 与手动发布（Issue #23）— 2026-10-14

新增 `seal snapshot create / list / inspect / publish`。创建在数据库一致性事务冻结
全局 Source 入口、Resource URL 与当前成功 Version，封存后映射不可变；不等待在途
Run 完成、不复制结果或附件，也不隐含发布。人工 publish 验证已有对象后以单一行锁
原子切换全局指针并记录切换前后 ID、顺序与时间；支持回切、幂等与失败保留原指针。

三个只读接口改为只读已发布 Snapshot，无发布时503 snapshot_not_published。
新请求固定当前快照，已有分页和附件绑定原 Snapshot 与调用方，在期限内跨发布继续
读取原集合。权限每请求从私有配置热读取，原子替换 callers 可即时撤权，包括旧令牌；
其他启动配置变更须重启。HTTP 路由、响应字段白名单及大小/超时/归档检查保留。

追加0013，不改历史SQL；0012有内容库可直接 `seal init` 升级，保留内部数据和S3对象。
更早归档合同升级仍受原空内容限制。初始化、升级及各采集/服务命令均不自动创建或发布。
升级后须显式 create → inspect → publish 才开始对外服务，旧水位游标/附件令牌失效。
回退程序可保留追加表与对象，但旧API会直接暴露内部当前版本，须评估发布语义变化。
本次不提供删除、GC、自动发布、逐来源发布或质量审核。合同与验收见
[唯一 Plan](docs/plans/global-snapshots.md) 与[同步可视化](docs/generated/global-snapshots.html)。
fresh 独立端到端验收411 PASS / 0 FAIL，涵盖实际CLI/HTTP/SQL/S3、暂停中的采集、
8次并发一致创建、12次并发发布、实时撤权、坏归档保护、队列Worker无自动发布，及
前版994c8ad实际采集后升级保留。源码/迁移/测试哈希一致，工件扫描与容器清理通过。
生产部署、负载及完整TTL壁钟等待尚未验证；已测试真实签名过期令牌。

## 未发布 / 只读结果服务（Issue #22）— 2026-10-14

新增 `GET /sources` 来源发现：按同一 Bearer 权限列出已入库来源的 id 与可唯一匹配的
entry_urls，供调用方继续请求 `/results`；包含空来源，排除歧义入口。只读数据库，
不读取网站或对象存储，不新增迁移。来源发现的独立端到端验收68 PASS / 0 FAIL。
真实本地19源、20入口全部分页验收92 PASS / 0 FAIL：87页，7538个唯一成功资源，
无接口失败；数据库与对象清单前后无变化。完整响应及复跑入口见唯一Plan。

新增 `seal serve`，通过 Bearer API Key 和明确的调用方来源许可提供 `/results` 与
`/attachments/{id}`。按登记入口查询当前成功版本，包括深度资源；最近复查失败仍返回
已有成功内容。固定内容字段白名单，附件只来自真实归档引用；不启动采集或重复存储结果。
分页使用首次请求的成功版本快照和认证加密游标，附件 ID 同样认证加密并重新验证归属。
SQL、S3和请求并发/字节/超时均有界，坏归档整页明确失败。无数据库迁移。
新增私有配置 `SEAL_API_CONFIG` 或 `--config`，复用数据库/S3配置，密钥不入库。
回滚停止服务并还原新增代码/配置。公开合同、复跑命令及验收状态见
[唯一 Plan](docs/plans/results-api.md) 与[同步可视化](docs/generated/results-api.html)。
独立隔离端到端验收161 PASS / 0 FAIL，105个真实HTTP请求，覆盖权限、归档内容、
快照分页、附件字节、故障/恢复、大小/并发/超时和只读无副作用。测试发现的排队期限
错误码竞态已修复。生产部署、HTTPS与生产负载尚未验证。

## 未发布 / robots 精确跳转许可与来源预算 — 2026-10-14

Source 增加可选 `robots_redirect_urls`，默认空列表。仅 robots 获取允许这些精确目标，
普通内容仍遵循原主机与路径范围；敏感 URL、非公网地址和 robots Disallow 校验保留。
该字段用于已核实的 robots.txt 首页/错误页跳转，不通过扩大业务路径来解除政策请求阻断。
政策目标返回 401/403 等状态仍停止内容获取。无数据库迁移；回滚配置与代码即可恢复旧行为。

九个部分成功来源将单轮请求上限调整为 10,000、时间上限为 3,600 秒；并发、深度与
响应大小上限保持原配置。增加预算不保证消除站点 404、内容拦截或格式限制。

## 未发布 / 最小 MinIO 归档合同（Issue #20）— 2026-10-14

新写入仅为完整 CrawlResult JSON 和原始 PDF/下载附件。停止独立 HTML/TXT/DOM、网页
截图/导出PDF/MHTML与运行manifest归档；显式关闭页面产物生成，运行统计、错误和配置
留PostgreSQL，CLI报告不含manifest_id。文本原始HTTP字节无法再从独立对象精确恢复，
body_hash改为输入指纹；只有完整原始附件有archive_ref，不以重编码HTML冒充原始bytes。

解析使用同次临时输入，PDF不再读正文对象或重新下载；并发同名附件使用独立临时文件。
每次SDK实际返回的成功/失败/unchanged结果绑定本次Observation；Version保留原始结果。
结果与全部附件读回验证后才提交引用。中断前已取得附件引用保留于checkpoint，不造结果；
新Run替换检查点前校验并将未交付证据保存回原Run.report，过滤已交付资源，不跨Run混附件。
无返回结果的失败Observation允许result_ref为空；attachment_refs持久保留已取得的完整附件列表。变化比较、A→B→A、复查与交付幂等保留。

追加0012_minimal_archive，只支持空旧内容库升级，不进行历史内容兼容、回填、对象迁移或
双写；删除旧result_migration和archive_import路径，已登记SQL checksum不变。升级沿用
停客户端和init --backup流程，回滚使用备份与旧代码。本地既有服务和生产未执行升级。
Fresh独立业务验收126 PASS / 0 FAIL，同源结果完整性和manifest工件复算7 PASS / 0 FAIL；
另真实CLI recheck并发同名长Unicode附件补验25 PASS / 0 FAIL，原件/结果/版本分别对应。
独立审核修复后无剩余阻塞。命令、前提、证据与未验证项见唯一Plan/Generated。

## 未发布 / CrawlResult 单一归档与结果指针 — 2026-10-13

完整 CrawlResult JSON 仅保存在 MinIO；Observation.result_ref 保存本次交付，Version.result_ref
保存产生版本时同一对象。复合外键约束版本与原始观察结果一致；unchanged 观察仍保留本次结果，
失败不推进版本，policy 无结果可空。移除 version.crawl_result 全量 JSONB 与 Run.report.crawl_results。
下游须由 SQL 取得 result_ref 再读取对象；不保留旧内部字段兼容层。

追加 0011_result_references，不改已应用迁移。已有数据库升级必须停止客户端并提供校验备份；
旧 Version JSON 写入并校验 MinIO，验证旧映射和附件后，事务内替换字段与重建视图。
保留身份、时间、序列、cursor，旧运行 manifest 不重写，缺失历史诊断不补造。
对象缺失、损坏或引用冲突时数据库迁移回滚，修复后可重试；可能残留未引用对象，不自动清理。
回滚需恢复升级前数据库并使用旧程序，保留配套对象归档。2026-10-14 已备份并升级现有本地数据库至0011；本地库与桶均为空，无既有引用可逐条核验。
独立隔离验收59 PASS / 0 FAIL，覆盖实际CLI、结果重建、旧库升级与故障回滚、实际备份还原；
工件与复跑命令见唯一Plan/Generated。0010为实际数据构造夹具，未执行历史checkout；
policy空结果与历史附件缺失回滚未验证。

## 未发布 / MinIO 本地对象存储 — 2026-10-13

原文、附件、二进制及运行结果归档改为通过 S3 API 保存；完整 SHA-256 为平铺对象键，
来源与资源版本关系仍在 PostgreSQL。条件写入避免覆盖，读取复算哈希，失败不创建成功版本。
移除内部文件 CAS 与 SEAL_ARCHIVE；新增 SEAL_S3_*，不做回退或双写。旧文件须显式运行
`seal.archive_import` 校验导入目标桶后再切换关联数据库；hash 与数据库 schema 不变，
SQL 迁移 ledger 不改。回滚前应恢复配套环境配置与数据库/对象备份，不能将 MinIO 内部目录
当作旧文件 CAS 使用。

移除空 local-data 目录，根 minio/ 为 MinIO 管理数据，work/ 为临时文件及本地测试数据库，
artifacts/ 为验收证据。新增 compose.yaml 与 seal.local_storage，服务仅监听 loopback，
凭据自动生成且不入库，应用账号限定桶读写无管理/删除权限。测试重置须同时重置对应数据库
和对象桶；不自动删除已有归档、数据库或历史工件。SDK 子进程缓存/log 另按 run/epoch
隔离到 work。修复本地 seal 账号的重复 init：按实际 search_path 查询队列表并返回实际
schema，不再固定 public，不迁移或移动既有队列表。OSS 实际部署兼容性未验证。

本轮独立隔离验收：S3 全链路72 PASS、同名账号重复初始化与worker14 PASS、真实DOM与
MinIO重启恢复18 PASS，均0 FAIL；部分权限断言复用。各组记录开始/结束一致的源码哈希。
原文、附件、SDK结果与SQL引用经过实际读取核验；既有生产数据未迁移。唯一Plan/Generated
保留复跑命令、工件路径和未验证范围，CI只执行src静态检查。

## 本地旧版产物清理 — 2026-10-13

按用户明确要求删除旧 Scrapy/Recipe/Document 链路的原文、导出、配置快照和历史验收数据，
清理旧隔离验收数据库及旧迁移夹具副本。历史章节中的数字保留为当时记录，已删除的本地
证据不能再用于复算。本轮 Crawl4AI 的 CrawlResult、原文和验收工件保留，数据哈希不变。

## 未发布 / CrawlResult 诊断留存 — 2026-10-13

所有本轮 SDK 成功、失败及 unchanged 的 CrawlResult 均归档至 CAS；Run.report.crawl_results
按 Observation ID 引用诊断，与观察同事务关联，重试保留首次交付。原文仍由 Observation 引用，
失败结果仍不推进 Version；policy 观察无 SDK 结果。无数据库迁移，既有 Run 不补造缺失诊断。
README 执行图与唯一 Plan/Generated 同步说明诊断出口及 SDK 异常字段可能缺失的边界。
robots 改用 Protego 的最长 Allow/Disallow 匹配，修复海仲 /data/upload/ 被宽 /data/ 误拒绝；
政策请求与业务请求使用相同 Source 身份。错误分类排除 SDK 源码上下文，避免 403 政策
不可用被误记为 robots_denied。政策拒绝仍不获取业务内容，JSON/XLS 解析能力未扩展。
SDK PDF 元数据日期经 Pydantic JSON 编码保留为 ISO 字符串，修复实际 PDF 的 datetime
导致原文下载后整轮中断；二进制仍先外置 CAS。
四个 playground 范围复现保存476份本轮SDK结果、238个资源；法院正文与海仲6PDF通过
独立原文核验。公募17JSON页、教育部2XLS已保存原文但未语义解析，不生成内容Version。
独立核验5009项中的2项能力缺口明确保留；诊断、robots与日期PDF真实CLI行为128项通过。
完整运行态端到端回归431项通过。Worker中断恢复验收纠正单次 --once 等待未来重试的
时序假设，保留精确版本断言并冻结有界重试/队列证据；独立仲裁最小积压场景44项通过。

## 0.3.0 / Crawl4AI 资源时间线（Issue #17）— 2026-10-13

Crawl4AI[pdf] 0.9.4 全线替换 Scrapy。保留 init、collect/recheck source.yaml、worker
及 --backup、--acknowledge-data-loss、--enqueue、--no-progress、--once；去除 Recipe
包/Binding 执行、自定义 Document AST、Revision/Emission/reference 新写入和旧glue。
Source YAML 改为 crawl4ai 配置，旧 recipe YAML 明确拒绝；JSON receipt.v3 用 config_hash
替换 binding_id，统计资源与观测。旧专用 POST/表单、Office/OCR/JSON/图片解析首版未接替。

Source:Frontier=1:1，资源按 frontier_id+URL 独立；响应获取阶段归档（HTTP 正文 bytes 或
明确 rendered_dom），Version 原生保存 CrawlResult。只比较当前成功版本的正文：相同不增
版本，A→B→A 连续版本，失败/阻拦不推进。CrawlResult 的下载与二进制工件存 CAS，不依赖
临时路径。旧 Run 的全轮原子文档发布改为各资源有效响应独立事务提交。

Source session lock、generation/epoch 和父子进程保护恢复；SDK checkpoint 补齐当前层
未交付窗口，以run/URL提交幂等。新轮重访入口，recheck 对到期已知 URL 批量复查。
下游使用 seal.current_resource_version、seal.version 和按 cursor 排序的 seal.version_delta。

新增0010_resource_timeline.sql，历史schema.sql/0002–0009字节和登记身份不变。
旧来源和历史内容表、归档继续可读；旧AST不伪造转换为CrawlResult，不双写。待执行旧任务
终止，需迁移 YAML 后新建 Run。0.2库升级停客户端并使用 init --backup PATH；更旧public
库经过历史0009仍需 --acknowledge-data-loss。回滚依赖旧代码、数据库与Archive备份恢复；
生产迁移另行授权。独立完整端到端427项PASS（0FAIL），覆盖合成HTTP/PDF/DOM、原文与版本、真实
取消/前台及Worker崩溃、旧任务栅栏、旧库备份实际恢复。公网适配与生产升级未验证。

文档同步：README 的执行图与 ER 图、唯一 Plan/Generated、AGENTS 目录规则及历史设计
导航均指向当前 Crawl4AI 合同。业务验收由独立子 agent 完成；项目静态/打包由主 agent
自验，CI由 GitHub Actions 执行。固定旧提交迁移补验另22项PASS，旧版本证据保留当时语义。

## 迁移文件整理 — 2026-10-12

将 schema.sql 移入 src/seal/migrations/，保持其内容、历史迁移登记名称和摘要不变。初始化仍先执行 schema.sql，再按编号执行后续迁移；已有库不重放已登记的迁移，无数据库结构变更。

## 验收方式调整 — 2026-10-12

移除固定测试套件、断言脚本、模拟来源及旧 benchmark 支撑代码。验收改由独立子 agent 按真实用户路径现场执行，范围、命令和证据保留在实验工件中；规则写入根目录 AGENTS.md。
CI 仅保留 Ruff 静态检查，业务验收单独记录。历史原文、冻结配置、结果及执行源码保持原样；不改变 Runtime 或数据库 Schema。

## 0.2.0 / Document-only（Issue #13）— 2026-10-12

所有成功内容响应统一进入 Archive → 格式 Parser → Document；来源发现与解析解耦。
Business、异构、越南法律和通用适配器保留网络发现/分页/公开表单，删除业务 Record 生成、
字段映射/Schema、细粒度 Locator。保留九格式、五节点、文档身份/版本/Emission 与原文血缘。
移除 Replay、Inspect/Export 与旧 CLI，改为 init、collect/recheck source.yaml、worker；
YAML 自动注册不可变绑定，--enqueue 可异步提交，周期采集交给外部 Cron。在线 Recheck、
有限重试、generation/attempt 防护和自动恢复保留。前台新增 stderr TTY rich.Live 只读面板。

新增 0009_document_only.sql，历史迁移不修改。Record/result 专属表实际 DROP；其余自建表、
索引、约束、函数与触发器迁入 seal 并去 seal_ 前缀，第三方 public.procrastinate_* 保持原样。
已有库必须停所有客户端、检查 init 清单并使用 --backup PATH --acknowledge-data-loss；
未知依赖会原子阻止升级。Record 历史有意删除，DROP 回滚依赖备份恢复；Document/Run/Archive
保留。外部 SQL 需改用 seal.<table>，不提供旧名兼容视图。结构化查询与导出仅用 SQL/COPY。

当前设计、验收范围与命令见 [Plan](docs/plans/unified-document-pipeline.md)。以下为历史版本记录，
其旧命令、Record/Replay 和验证结果不适用于 0.2.0。

## 统一文档流水线（Issue #10）— 2026-10-11

新增 Source `document.v1` 与 `recipes/documents`：响应先归档，再解析、校验和保存统一文档，
可独立查询/导出；业务提取为可选后续步骤，继续使用 #6 字段合同。统一文档只包含整篇
article、image 引用、二维 table、完整 json 值和 document_ref 外键五种内容节点。
复用 HTML、文本 PDF、XLS、DOCX、TXT、JSON，补齐真实 CSV 二维与 openpyxl XLSX；
图片关联原文容器/成员。PDF 不包含 OCR 或可靠表格识别，XLSX 公式不计算。

新增 `0008_unified_documents.sql`，沿用既有资源身份和原文 Revision，保存不可变解析版本、
实际 Run emission 与共享目标外键。`inspect document` / `inspect document-version` 和
`export --documents` 提供独立出口；引用查询区分未下载、解析失败和已解析。只有 complete
在线 Run 推进当前表示指针，失败保留原文和有效工件。Record 保存全部实际文档版本，
document Locator 从归档及冻结解析规则复算。旧 record/generic hash、对象和出口保持兼容。

升级前停止 Worker 并备份数据库/Archive，迁移后重新 pack Recipe 和建立 Binding。
解析代码/依赖变化后旧 document Locator 明确拒绝版本漂移；新 Binding 可零网络重解析
旧原文，精确旧版本仍使用配套旧环境。完整合同及验收入口见
[Plan](docs/plans/unified-document-pipeline.md) 和
[唯一可视化](docs/generated/unified-document-pipeline.html)。

## V1.3 / 0.1.3 — 2026-10-10

新增基于业务 Seed 的入口扩展、有界递归链接与 Sitemap 发现，复用原生 Scrapy
Scheduler、去重、既有 Discovery 与 Scope。seeded Recipe 要求 robots 开启；
政策请求归档、计入预算，失败关闭业务访问。旧 Source 默认 false 保持兼容。

Specific HTML 规则优先，无匹配时由同一 Recipe / Record 合同的 Fallback 提取正文。
新 HTML Locator 可复算标题、法律段落层级、编号、表格、日期 null、URL 和附件。
登录、导航、错误及无可靠正文的页面明确拒绝，不生成成功文档。

新增 `seal collect` 与 `runs.collect_source()`、当前 attempt 的运行计数和终止原因、
`inspect record` / `inspect snapshot`。存储、幂等、业务版本、Replay 和有限恢复
继续使用现有 PostgreSQL 与不可变原文归档。新增 `0006_seeded_discovery.sql`；
已应用迁移不修改，升级需停止旧 Worker、备份、迁移和新 RecipeVersion / Binding。

Review 修复：Recheck 关闭计划外递归；robots Sitemap 按政策版本每 Crawl 发现一次；
gzip Sitemap 有界解压并保留下载表示。partial Replay 冻结原拒绝证据，未知请求及
已成功归档但映射缺失的输入仍严格失败。robots 规则拒绝与政策获取失败分别审计；
动态附件使用最终 Content-Type 并保留父输入，XLSX 明确为未支持解析的格式。

新测试、实际长期留存演示及八个业务 Seed 的探索记录见
[V1.3 Plan](docs/plans/v1.3-seeded-site-collection.md)。网络政策拒绝与未知适配如实记录，
无站点 Ground Truth，不依据队列耗尽推断覆盖。历史工件保持原样。

## V1.2 / 0.1.2 — 2026-10-10

### 业务 Record 与异构来源扩展

- 增加业务 Record 模型、Discovery、多记录 JSON 提取、静态 iframe 处理与业务附件关联。
- 支持针对政企与公开平台的异构入口解析，为特定来源提供专用 API / 游标规则与结构化字段抽取。
- 归档合同调整：按新归档规范移除 Runtime 正文敏感性扫描，网络响应按原始 bytes 完整归档，URL/日志脱敏；归档前拒绝非空凭据字段（如 csrfToken/authToken）。归档目录要求私有权限（`umask 077`）。

### 任务与调度增强

- 新增增量迁移 `0003_records.sql`、`0004_discovery.sql`、`0005_recheck_plan.sql`，支持 Record、Discovery 与复查计划/退避元数据。
- 修复 JSON 稳定身份 Recheck、到期记录自动分批、失败重试日期以及重复 JSON 解析问题。
- 架构确立模块化单体：Scrapy + 可信 Python Recipe + Procrastinate + PostgreSQL + CAS 内容寻址归档。

### 升级与设计文档

- 升级需停止 Worker、备份数据库与归档，执行 `seal db migrate` 后重新 pack Recipe 并创建 Binding。
- 详细设计与验收参见 [V1.2 Plan](docs/plans/v1.2-heterogeneous-sources.md) 与 [唯一可视化](docs/generated/v1.2-heterogeneous-sources.html)。

## V1.1 / 0.1.1 — 2026-10-09

V1.1 交付独立 Runtime。产品版本称为 V1.1，Python 包版本为 `0.1.1`；沿用 Scrapy、可信 Python Recipe、Procrastinate、PostgreSQL 和本地原文归档。

### 执行与数据合同

- 候选 Binding 可直接同步运行或进入真实 Worker 队列，不必成为 Source 默认版本，也不会因运行而改变默认值。
- 删除 Trial、Gold Comparison、Review、Activation、发布门禁及相关 CLI；配置和默认 Binding 选择保留。
- 导出改为可追溯的运行结果。`complete` 表示技术执行完成，`partial` 保留技术校验通过的部分结果；二者均为 `quality_status=not_evaluated`。
- 保留独立 Observation、内容寻址原文、A→B→A 顺序修订、不可变 ProcessingResult，以及有限恢复、完成事务重入和过期任务 fencing。
- 离线 Replay 使用历史原文和明确的 Binding，不补发网络请求，不改写原始归档。

### 验收修复

- Locator 增加 `transform: date_iso` 与 `segments.separator`，支持日期规范化和原文片段拼接；保留定位凭据，严格复算并比较最终输出。旧 locator 不带新字段时保持原行为。
- 固定 `ROBOTSTXT_OBEY=False`，删除 robots 专属范围豁免和完成状态特判。普通业务 HTTP 错误仍参与执行状态，最高检不再被自动 robots 请求误标为 partial。
- 保留域名、路径和预算检查，拒绝非公网 IP 字面量及本地域名；loopback 仅供显式开启的隔离验收使用。

### 升级与兼容

运行 `seal db migrate` 应用 `0002_runtime.sql`；迁移前停止所有写入并备份数据库和归档。保留原 `schema.sql` 与历史 Run、Binding、Result、审核 payload、Revision 和原文；旧未完成任务终止，旧 Source 暂停。

升级后重新 `recipe pack`、`binding create`，再执行 Run/Replay 和默认版本选择。旧 Recipe 源码与归档可以复用，但旧引擎的 Binding 不绕过环境摘要校验。回退需要恢复配套备份。**旧发布 JSON 消费者必须适配新的运行数据出口，不能继续将其视为审核通过的数据。** 详细步骤见 [README](README.md) 和 [V1.1 Plan](docs/plans/v1.1-runtime.md)。

### 实际验证

全部测试使用独立数据库、队列和归档目录。最终受测 Runtime 与本次提交的代码摘要一致。

| 集合 | 实际结果 | 耗时 |
| --- | --- | --- |
| 既有 M3 回归，最终工作区复验 | 136/136 断言 PASS | 83.47 秒 |
| 独立 smoke | 36/36 Case、2182/2182 断言 PASS | 204.64 秒 |
| Golden / Runtime / 反向与边界 | 8/8、12/12、11/11 PASS | 已包含于 smoke |
| 历史兼容 / robots 与业务错误 | C01、X01 PASS | 已包含于 smoke |
| 最高法、最高检、Python 教程 | 各 10 条详情、两轮 complete、改版离线 Replay 通过 | 已包含于 smoke |

Golden 只覆盖当前 Schema 的 title/date/body；update_date/document_no 共 16 项字段检查为 NOT_APPLICABLE，不计 PASS。非法转换、错误定位和错误输出均被拒绝，CLI、Worker 与 Replay 使用一致校验。真实来源结果不构成质量、完整性或持续可用性承诺。

完整本地证据保留于 `artifacts/acceptance/v1.1-runtime-fixes/`，原 `v1.1-smoke/` 保持不变。工件按仓库规则不入 Git；下列 SHA-256 固定本次结果，不能用后来生成的结果替换本次记录：

- `m3-final/manifest.json`：`39356a6e6230c2a7e82a6c328e55f9e9f6283da5bcbe6df3ad0aed1e362807cb`
- `smoke/manifest.json`：`bd1f2fe4874fae9e46c3650b44ce822281700835640a22459e52e51dd4f409af`

复现环境为 Python 3.12.13、PostgreSQL 17.11 和 `uv.lock`；`initdb`、`pg_ctl`、`pg_dump`、`psql` 需在 PATH。

```sh
uv sync --frozen
./experiments/v1.1-runtime-acceptance/acceptance.sh --stage m3 --output artifacts/acceptance/v1.1-repeat/m3
./experiments/v1.1-runtime-acceptance/acceptance.sh --stage smoke --baseline artifacts/acceptance/v1.1-repeat/m3 --live --output artifacts/acceptance/v1.1-repeat/smoke
uv run --frozen python experiments/v1.1-runtime-acceptance/verify_artifacts.py artifacts/acceptance/v1.1-repeat/m3
uv run --frozen python experiments/v1.1-runtime-acceptance/verify_artifacts.py artifacts/acceptance/v1.1-repeat/smoke
```

C01 额外需要上一轮保留的 `artifacts/acceptance/v1.1-smoke/court-confirmed/` 数据库与归档；新 checkout 缺失时明确记为 UNVERIFIED，不能称为全量通过。省略 `--live` 可关闭外网测试；外网变化如实记录，不作为必须通过的 CI。

### 文档与工件整理

V1 Plan、历史 ADR、旧可视化及最终验收均保留审计用途。仅清理与仓库实验原件完全相同的重复输出，将早期开发验收和独有分析报告压缩到 `artifacts/archive/v1-development.tar.gz`；清理清单及摘要保留在 `artifacts/releases/v1.1-commit/cleanup.json`。不删除原始 Golden Expected，不改写历史验收结论。

### 未交付与未验证

Evaluation、Management 审核/正式发布和 Agent 均未实现。未验证真实 PDF、真实来源日期准确性、业务总体质量、生产规模、整机断电持久性、恶意 Python 隔离或 DNS 重绑定防护。V1.1 工程验收通过仅限本轮受测 Runtime 范围。

## V1 / 0.1.0 — 2026-10-09

初版提交 `b32184b3628302efcaf012f4b0cd006050bbb72c`：可信 Python Recipe、Scrapy 采集归档、Procrastinate 队列、版本血缘与恢复，以及当时的 Trial、人工审核、激活和发布流程。

[V1 Plan](docs/plans/v1-information-pipeline.md)、[ADR-0002](docs/adr/0002-python-recipes-minimal-v1.md) 与 [旧可视化](docs/generated/v1-overview.html) 保留原始合同；V1.1 的职责调整以 [ADR-0003](docs/adr/0003-runtime-evaluation-management.md) 为准。
