---
kind: plan
lang: zh
---

# 全局不可变 Snapshot 与手动发布（Issue #23）

业务合同来自 [Issue #23](https://github.com/nanzhi84/SEAL/issues/23)，
[唯一可视化](../generated/global-snapshots.html) 随本计划更新。
本合同替换 [结果服务 Plan](results-api.md) 的内部当前版本、水位分页与重启撤权行为；
HTTP 路由、认证、字段白名单、归档检查和资源限制继续沿用。
当前状态：Issue #23 历史验收411 PASS / 0 FAIL；Issue #28 实现完成，
fresh 独立端到端验收122 PASS / 0 FAIL（2026-10-16），均未部署生产。

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

## Issue #28：双指纹与历史版本覆盖设计

本次需求来自 [Issue #28](https://github.com/nanzhi84/SEAL/issues/28)。以下为目标合同，
验收结果在本 Plan 末尾记录；不把 Issue #23 的历史通过数计入本次。

职责与依赖保持 `configuration / runs → parsing → resources`，以及独立的
`CLI → snapshot overrides 边界 → snapshots → SQL / archive`；采集不依赖快照。
`parsing` 负责纯身份计算及本机解析运行时清单，`resources` 独占事务内升版判断，
CLI 读取有界 JSON 并校验重复键、UUID 和重复资源，snapshots 在一致视图校验归属并一次插入最终映射。

来源输入保持既有 `body_hash`：HTTP 是响应原始字节，rendered_dom 是实际交给解析器的
UTF-8 DOM，二者均不是结果 JSON 哈希；PDF/下载原件的 body_hash 同时是 archive_ref。
现有采集不下载 HTML 链接指向的附件，也不把浏览器子资源独立交给解析器；PDF 原件就是
解析输入。若 SDK 结果有额外下载输入，确定性摘要同时纳入 envelope 中去重排序的附件
CAS 引用，剔除与 body_hash 相同的主原件，防止漏掉独立附件。HTML/TXT 同时纳入实际解码
并提交给解析器的输入文字摘要及实际解析基地址，避免原字节相同但响应字符集或基地址
改变时漏升版；TXT 先安全转义
并包装为 pre 文档，解决 SDK 将普通文本误判为缺少 body 的阻断。保持 body_hash 与原文对象
键的既有区别，不新增同义来源列。

新 Run 在创建前冻结解析器清单；重试及 crawl 子进程启动校验当前清单，漂移明确失败，
不得混用部署前后的解析器。清单包含 Crawl4AI、相关解析依赖、显式 SEAL 适配语义修订、
输出合同、PDF native/OCR text contract、OCR 引擎及已配置语言模型身份。
仅有效输出参数参与当前 Observation 指纹：HTML 的 css_selector / word_count_threshold，
DOM 的 wait_until，PDF 的相关文字合同及 OCR languages / dpi。调度深度、周期、并发、
延迟、超时和安全预算不参与。显式适配语义修订须在改变解析输出的实现修改时更新。
失败观测仍保留 run 清单、parser_fingerprint 与 error 阶段；未成功不产生版本。

版本指纹以 canonical JSON（sort_keys、无空格、UTF-8、禁止 NaN）的 SHA-256 计算，
载荷含算法版本、具名来源摘要和 parser_fingerprint；仅与当前 Version 比较，
A→B→A 保留三次演进。即使 Markdown 相同，只要来源或解析器指纹变化就升版。
去掉 PDF 独立判定分支，PDF 文字合同直接进入同一解析器指纹。
Observation / Version / current pointer 仍由同一事务、Resource 锁和 Run fence 保护，
delivery_key、sequence 和全局 cursor 提交锁保持原合同。

追加迁移0014：Run.parser_manifest、Observation.parser_fingerprint / change_reason，
Version.parser_fingerprint / version_fingerprint；Version.parser_config 保存有效解析清单，
framework_version 来自冻结清单。历史字段保持 NULL，表示 legacy/unknown，不伪造身份；
下一次成功观测仅产生一次 legacy_transition，随后稳定观测 unchanged。
历史 Run 尚未尝试可在首次启动冻结清单；已经尝试但缺身份的旧 Run 明确失败，须新建 Run。
唯一约束 `version(observation_id)` 添加前检查冲突，发现冲突报告并停止，不删除证据。
历史 Snapshot 与公开 HTTP 白名单不变，新增字段仅内部 SQL/运行回执可见。

```sh
seal snapshot create --version-overrides work/snapshot-overrides.json
```

配置仅接受 `{"overrides":[{"resource_id":"<uuid>","version_id":"<uuid>"}]}`。
最多10000条、文件最多2 MiB；重复JSON键、额外字段、非规范UUID、重复resource_id拒绝。
内部 create 接受规范化列表并再次校验。无覆盖沿用默认最新成功版本；覆盖只替换指定
资源，所有 Source 仍由同一 REPEATABLE READ 视图冻结。先校验 Resource、Version、
归属及选定结果/附件可读性，再一次 INSERT 最终映射并 sealed；任何失败回滚，不留下草稿。
不 UPDATE 默认映射，不放松 guard，不改变 current_version_id 或 publication。

升级执行 `seal init`；生产升级先停 worker / serve 并按运维要求备份。
日常隔离库不要求额外备份。回滚可保留0014追加列及约束，但旧程序不能保证解析器升版
合同，须暂停采集；旧 sealed Snapshot 仍可按原有只读路径消费与显式发布。

验收先列失败边界，由 fresh 子 agent 在 tests/ 独立编写端到端测试，使用隔离数据库、
对象存储与可控来源，覆盖 issue 全部矩阵、升级、重试、并发、错误原子性及实际 API 内容。
运行数据及无敏感工件分别放 work/ 与 artifacts/；CI 继续只做静态检查。

### PR #29 两项 P2 的修复合同（2026-10-16）

审查来源：[实际浏览器运行时身份](https://github.com/nanzhi84/SEAL/pull/29#discussion_r4290732956)
及 [PDF 排除 HTML 配置](https://github.com/nanzhi84/SEAL/pull/29#discussion_r4290732964)。
本段是本次修复目标，后文122项为此前历史结果，不代表这两项已被覆盖。

当前锁定 Crawl4AI 0.9.5 的普通 BrowserAdapter 实际选择 PlaywrightAdapter，
`use_undetected=false`，由 Playwright 启动 headless Chromium；Patchright 是旧安装
命令使用的工具，不是当前执行驱动。保持现有执行后端，显式复用同一驱动选择；清单应
冻结实际驱动包身份及实际 headless-shell 安装的 revision / 内容身份，不能只记录Python
Playwright版本，也不能把未使用的Patchright升级当作渲染语义。安装位置变化不应升版，
同一revision原位替换有效运行时必须可检出；运行时缺失或探测失败保留失败证据，不能提交
新的成功版本。探测应有界，不启动用户浏览器，不在数据库/工件中保存私有环境或凭据。

只在 rendered_dom Run 探测浏览器并参与该输入模式的指纹及重试检查；HTTP/PDF 不读取
或依赖浏览器安装。PDF Observation 的有效指纹仅保留通用合同和PDF/native/OCR相关
语义，排除 HTML 的 css_selector / word_count_threshold 及 HTML 解析依赖；同一HTTP
Source 内HTML观察仍对其有效配置变化升版。原有 PDF text contract / OCR dpi 等实际
语义变化仍升版。清单算法更新应可识别；既有冻结Run不静默改写，应明确拒绝旧语义重试，
历史Version与Snapshot保持原样，下次新成功观察允许一次清单过渡，无额外数据库迁移。

fresh 子agent先独立按业务目标复现两个缺口，再验收修复；隔离浏览器安装/部署副本位于
work/，不改用户全局浏览器或真实数据。修复PR必须等connector完成对最终head的审查，
处理全部反馈后才能合并，Ruff CI成功不能替代这一条件。

#### 两项 P2 的独立验收（2026-10-16，PR #30）

fresh 子agent只依据Issue #28和两条审查目标独立编写业务用例，未接收实现设计；
基线从 `git archive 1e17b21 src` 冻结。同一测试字节哈希在旧版 **33/40 PASS**，
7个预期失败完整复现：安装身份变化不升版/无追溯/重试静默切换，以及两个HTML参数分别
导致native和真实OCR PDF伪升版。基础设施错误为NULL，不把预期失败算作修复通过。

修复版 **40/40 PASS**；既有主流程96、legacy升级9、历史冲突4、真实OCR13全部重跑
通过，共 **162 PASS / 0 FAIL**。新用例额外验证：未选Patchright包变化无影响，所选
Playwright包变化可追溯；浏览器包/安装变化不污染混合HTTP/PDF Source；HTML仍按配置
升版，真实PDF text/OCR dpi仍升版；缺失浏览器不推进成功指针。

真实启动证据确认 Playwright 1.63.0 的 Node driver 启动headless-shell revision1243 /
Chromium153.0.8010.12。A/B变更只在work/安装副本替换启动launcher身份，再exec真实
Chromium，原引擎版本相同；这验证安装身份漂移，不声称已测试第二个Chromium发行版。
全局浏览器未修改；每组独立PostgreSQL17/MinIO临时容器与bucket，容器已清理。
新测试复用既有隔离工具，运行前提与上次相同，额外要求本次实测的macOS/APFS clone
`cp -cR`及Arial字体；Linux/Windows复跑和生产性能未验收。

```sh
uv sync --frozen
uv run --no-sync python -m playwright install chromium
uv run --no-sync python tests/runtime_review_acceptance.py
uv run --no-sync python tests/issue28_acceptance.py
uv run --no-sync python tests/ocr_version_fingerprints.py
```

重现旧版缺口：先将 `git archive 1e17b21 src` 解包到
`work/runtime-review-baseline-src/`，再为新测试添加
`--source-root work/runtime-review-baseline-src --label baseline`。
该命令预期非零退出并报告上述7项失败；其余回归命令预期成功。

工件临时生成、不入库；新检出按命令重新生成，不依赖旧工件存在：

- `artifacts/runtime-review-verdict/suite.json`：162项汇总、源码/测试/依赖及全部工件哈希。
- `artifacts/runtime-review-baseline-frozen-20261016T062734-266354/`：冻结旧版40项与7项失败。
- `artifacts/runtime-review-fixed-20261016T062748-7b4cc7/`：40项修复断言、真实进程父驱动、
  安装A/B、CLI回执、Run/Version与混合PDF/native/OCR证据。
- `artifacts/issue28-20261016T062748-8de22f/suite.json` 及对应legacy目录：109项回归。
- `artifacts/issue28-ocr-20261016T062748-bacb28/`：13项真实OCR回归。

所有最终运行源码与测试稳定。受测src/含迁移的集合SHA-256（既有Issue28算法）为
`f499ddacaba87bcd9a44e6dbdd83b6367879c5dfc520c3817cbd9c11b64f6743`；
新用例的相对src目录算法及各文件哈希见suite.json。主agent复算源码、测试、锁及全部
索引工件一致，并重跑敏感扫描：63文件、24个私有随机凭据及能力令牌/凭据模式0匹配。
后续提交只补入同版测试与本验收记录，不改变受测实现。

本地src Ruff/format、seiso、diff检查通过，无新增依赖环。
[修复PR #30](https://github.com/nanzhi84/SEAL/pull/30) 的最终head connector审查完成且无
未处理问题后才能合并，实际审查结论以该PR原始记录为准；此前PR #29的122项历史结果
不能代替本次审查。生产部署/业务库升级、第二个浏览器发行版与生产负载未验证。

### Issue #28 最终独立验收（2026-10-16）

主 agent 实现与 fresh 验收分离，最终 **122 PASS / 0 FAIL**：

| 运行组 | 通过数 | 实际覆盖 |
| --- | ---: | --- |
| 主套件 | 96 | 首次/不变/来源/解析器/两者变化、同值输出、运行噪声、A→B→A；真实 HTTP/TXT/DOM/PDF、字符集与解析基地址、附件输入；失败事务回滚、固定清单重试、全局 cursor；多来源快照、历史覆盖、无效配置/归档原子失败、并发冻结、真实 API 正文/分页/附件 |
| legacy 升级 | 9 | 真实基线采集后升级0014，保留来源事实与历史快照，NULL身份一次过渡，旧快照仍可消费和发布 |
| 历史创建者冲突 | 4 | 同 Observation 的冲突历史明确报错、全部事实与 schema 原样保留，不删证据 |
| 真实扫描 PDF OCR | 13 | 无文字层输入经 Tesseract 真实识别；144→192 DPI 产生新指纹/版本，稳定及超时/预算噪声不反复升版；实际快照/API 消费 |

复跑前提：Python 3.12（实测3.12.13）、Docker、PostgreSQL 17 镜像与兼容客户端工具、
可用 Tesseract 及 eng 模型（实测5.5.3），允许安装 Crawl4AI Chromium。MinIO 使用仓库
`seal-minio:RELEASE.2025-10-15T17-29-55Z` 镜像，不存在时测试自动按 compose 构建。
legacy 测试从 Git 读取 `ba9db3a2c006d26b16d04f1262508a4045500d07` 的真实源码，
浅克隆须先获取该 Git 对象。全部来源均为合成数据及环回 HTTP，容器使用 tmpfs、随机端口
与私有随机凭据，独立 bucket；测试清理容器，不连接现有业务库或根目录 MinIO 数据。

```sh
uv sync --frozen
uv run --no-sync patchright install chromium
uv run --no-sync python tests/issue28_acceptance.py
uv run --no-sync python tests/ocr_version_fingerprints.py
```

主套件命令自动追加 legacy 成功/冲突两组运行；OCR 单独运行，避免将没有执行的
分支算作验收。每次复跑生成新的 work/ 和 artifacts/ 运行目录，历史证据不能冒充新结果。

本次工件（临时生成、不入库）如下，保留路径和哈希供本机核对；新检出须复跑生成：

- `artifacts/issue28-20261016T033720-087944/suite.json`：主套件和两组升级的109项汇总。
- 同目录 `environment.json`、`commands.json`、`assertions.json`、`matrix.json`、
  `snapshots.json`、`consumption.json`、`concurrency.json`、
  `incremental-cursor-consumption.json`、`retries.json`、`storage-atomicity.json`：
  命令、环境、清单、Observation/Version/Snapshot ID 与真实内容消费对照。
- `artifacts/issue28-legacy-20261016T033845-d7301b/` 与
  `artifacts/issue28-legacy-conflict-20261016T033852-c7a15a/`：历史迁移事实与失败证据。
- `artifacts/issue28-ocr-20261016T033858-dda4cc/ocr-evidence.json` 及 `result.json`：
  真扫描件、引擎/语言模型身份、两版完整结果及13项断言。
- 主目录 `final-sensitive-evidence-audit.json`：105个历史及最终工件、18个已知私有随机
  凭据精确扫描与能力令牌/凭据模式均0匹配。附件能力令牌已脱敏，正文和SHA保留。

全部最终运行 `source_stable=true`、`tests_and_dependencies_stable=true`。
受测源码提交 `5c0876e038a4d344fadcfaeb786e08fbd997534b`，src/含迁移的集合SHA-256为
`fcf006efb8400350dbd6abf87b0d1b28d29bbcac63ded2a9c8de5aa4b974241a`。
各脚本、pyproject与uv.lock哈希保存在环境工件，主 agent 现场复算一致；后续仅提交这些
既有测试与验收文档，不改受测源码。独立验收未发现阻塞合并项；架构检查没有新增环，
原有 completion/runs 延迟导入关系保留；Ruff、格式、seiso与diff检查通过，临时容器已清理。

未验证：生产部署/业务库升级、真实站点全量、真实生产负载、多语言 OCR 切换、跨主机并发
与断电回收。隔离验收不构成全站覆盖或业务事实质量认证。

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

历史验收记录的工件根目录为 `artifacts/issue23-20261014-150910-8244e0/`。
2026-10-15 核对时，该目录及以下文件在当前工作区均不存在，因此保留文件清单供追溯，
不再提供失效链接；下述历史结果和哈希未在本次重新核验。工件属于不入库的临时产物，
按上方命令复跑会生成新运行目录，不能重建原运行证据或冒充原哈希：
汇总（`summary.json`）、
独立审计（`audit.json`）、
全部断言（`assertions.json`）、
命令回执（`commands.json`）、
环境与文件哈希（`environment.json`）、
最终SQL映射（`final-sql-mappings.json`）、
并发创建（`consistent-concurrent-create.json`）、
发布中HTTP对照（`http-during-publication.json`）、
发布记录（`publication-records.json`）、
升级（`upgrade.json`）、
队列Worker（`queued-worker.json`）。
各 `sql-api-*.json` 保存固定版本、完整公开内容及附件SHA对照。

28个源码、13个迁移、13个测试/辅助输入的受测哈希与最终工作区相同；凭据和不透明
令牌匹配扫描均为0；两个临时容器均已清理。此前失败轮次仅为测试夹具错误，保留历史
工件，不改写原结果。
验收汇总SHA-256：`95168fe9e847510c57dcfbd7f0796fa66d7a08660ca35677d17536e4b896a2ea`；
独立审计SHA-256：`cb216bc2ac7e367795cfbd8f15ff20a6e22a543c55cd211fddaf63e31ca1f3ca`。

本地Ruff、格式、diff检查与seiso全部通过；架构检查未发现新循环依赖或采集→发布依赖。
生产部署、公网来源可用性及生产负载未验证；TTL使用真实签名的过期令牌测试，未等待
完整1小时/24小时壁钟。快照内容仍需人工业务核对，不保证全站覆盖或质量。

## 运行示例：从公开来源采集到 API 查询

以下示例在仓库根目录执行，演示公开来源抓取、存储、人工发布和只读 API 查询。
来源及资源数沿用历史示例，不是本次隔离验收结果；重跑需核对当前站点和运行回执。
全局快照冻结全部已入库来源，本示例只给四个来源的调用许可。

```text
sources/dd-xxx.yaml → seal collect (抓取与解析)
  → PostgreSQL (元数据与版本) + MinIO (结果 JSON 与附件)
  → seal snapshot create → inspect → publish (人工发布全局集合)
  → seal serve (只读 API) → /sources 发现来源 → /results 查询内容
```

### 1. 选择验证来源

选用 4 个采用轻量 HTTP 模式（无需浏览器环境）的已跑通来源作为示例：

| Source / 来源地图                                                          | 公开入口                                                           | 配置文件                           | 历史成功资源数 |
| :------------------------------------------------------------------------- | :----------------------------------------------------------------- | :--------------------------------- | -------------: |
| [dd-017 · 最高法指导案例](../reference/尽职调查核验入口地图.md#dd-017)   | [指导案例](https://www.court.gov.cn/shenpan/gengduo/77.html)       | [dd-017.yaml](../../sources/dd-017.yaml) |            547 |
| [dd-040 · 最高检公报](../reference/尽职调查核验入口地图.md#dd-040)       | [最高检公报](https://www.spp.gov.cn/spp/gjgb/index.shtml)          | [dd-040.yaml](../../sources/dd-040.yaml) |             31 |
| [dd-041 · 最高检指导性案例](../reference/尽职调查核验入口地图.md#dd-041) | [指导性案例](https://www.spp.gov.cn/spp/jczdal/index.shtml)        | [dd-041.yaml](../../sources/dd-041.yaml) |             31 |
| [dd-448 · 电影备案公示](../reference/尽职调查核验入口地图.md#dd-448)     | [电影备案立项公示](https://www.chinafilm.gov.cn/xxgk/gsxx/dybalx/) | [dd-448.yaml](../../sources/dd-448.yaml) |             16 |

> 注：成功资源数为历史实测参考值，重跑结果受站点当前状态与配置预算影响。

### 2. 准备本地存储与运行环境

启动本地 PostgreSQL 与 MinIO 容器，并初始化数据库 schema：

```bash
uv sync --frozen
uv run --no-sync python -m seal.local_storage

# 导出数据库与 S3 配置
set -a
source .env.local
set +a

# 初始化 schema 并创建临时目录
uv run --no-sync seal init
mkdir -p work/readme-demo artifacts/readme-demo
```

> 注：对象读取统一走 S3 API，不要直接操作根目录 `minio/`。

### 3. 执行采集与入库验证

在前台执行采集任务，回执 JSON 保存至 `artifacts/readme-demo/`：

```bash
set -o pipefail
uv run --no-sync seal collect sources/dd-017.yaml | tee artifacts/readme-demo/dd-017-run.json
uv run --no-sync seal collect sources/dd-040.yaml | tee artifacts/readme-demo/dd-040-run.json
uv run --no-sync seal collect sources/dd-041.yaml | tee artifacts/readme-demo/dd-041-run.json
uv run --no-sync seal collect sources/dd-448.yaml | tee artifacts/readme-demo/dd-448-run.json
```

核验关系库状态，并通过 S3 API 真实拉取结果对象以验证连通性与 SHA-256 CAS：

```bash
uv run --no-sync python - <<'PY' | tee artifacts/readme-demo/storage-check.json
import json
from seal.db import connect
from seal.objects import Objects

ids = ["dd-017", "dd-040", "dd-041", "dd-448"]
with connect() as conn:
    conn.execute("SET TRANSACTION READ ONLY")
    rows = conn.execute("""
        SELECT s.id,
          (SELECT status FROM seal.run WHERE source_id=s.id
           ORDER BY created_at DESC LIMIT 1) AS latest_run_status,
          (SELECT count(*) FROM seal.frontier f
           JOIN seal.resource r ON r.frontier_id=f.id
           WHERE f.source_id=s.id AND r.current_version_id IS NOT NULL)
           AS successful_resources
        FROM seal.source s WHERE s.id=ANY(%s) ORDER BY s.id
    """, (ids,)).fetchall()
    sample = conn.execute("""
        SELECT r.url, v.result_ref FROM seal.frontier f
        JOIN seal.resource r ON r.frontier_id=f.id
        JOIN seal.version v ON v.id=r.current_version_id
        WHERE f.source_id='dd-017' ORDER BY r.id LIMIT 1
    """).fetchone()

assert {row["id"] for row in rows} == set(ids), "source_not_registered"
assert all(row["latest_run_status"] == "complete" for row in rows), "latest_run_not_complete"
assert all(row["successful_resources"] > 0 for row in rows), "source_has_no_successful_content"
assert sample is not None, "no_successful_version"

# 通过 S3 API 读取对象并复算校验 SHA-256
raw = Objects().get(sample["result_ref"])
archive = json.loads(raw)
assert isinstance(archive["result"], dict), "invalid_result_archive"
print(json.dumps({"sources": rows, "archive_sample": {
    "url": sample["url"], "result_ref": sample["result_ref"],
    "bytes": len(raw), "archive_verified": True
}}, ensure_ascii=False, indent=2))
PY
```

### 4. 创建、核对和手动发布

完成采集与存储核验后冻结全局集合；create 不影响对外内容。inspect 分页核对来源、
资源和固定 Version，操作者自行判断内容是否适合发布。尚无发布指针时 API 返回503。

```bash
uv run --no-sync seal snapshot create | tee artifacts/readme-demo/snapshot.json
demo_snapshot_id=$(uv run --no-sync python -c 'import json; print(json.load(open("artifacts/readme-demo/snapshot.json"))["snapshot_id"])')
uv run --no-sync seal snapshot inspect "$demo_snapshot_id" --limit 100
# 按 next_after / source_next_after 继续核对剩余映射，再显式发布
uv run --no-sync seal snapshot publish "$demo_snapshot_id"
```

再次采集或创建快照不会改变对外集合；必须再次 publish 才更新。回切使用同一 publish
命令传入旧 Snapshot ID。内部成功资源数、complete 与存储核验不代表全站覆盖或业务质量。

### 5. 生成配置并启动 API 服务

`seal serve` 是只读 HTTP 服务。生成测试授权配置与 Bearer 请求头后启动服务：

```bash
uv run --no-sync python - <<'PY'
import hashlib, json, os, secrets
from pathlib import Path
from cryptography.fernet import Fernet

root = Path("work/readme-demo")
root.mkdir(parents=True, exist_ok=True)
key = secrets.token_urlsafe(32)
config = {
    "public_base_url": "http://127.0.0.1:8090",
    "token_key": Fernet.generate_key().decode(),
    "callers": [{
        "id": "demo",
        "key_sha256": hashlib.sha256(key.encode()).hexdigest(),
        "sources": ["dd-017", "dd-040", "dd-041", "dd-448"]
    }]
}
for name, body in (
    ("api-config.local.json", json.dumps(config, indent=2)),
    ("headers.local.txt", "Authorization: Bearer " + key + "\n")
):
    path = root / name
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        f.write(body)
    path.chmod(0o600)
PY

# 在当前终端启动服务（保持运行）
uv run --no-sync seal serve \
  --config work/readme-demo/api-config.local.json --host 127.0.0.1 --port 8090
```

### 6. 客户端查询与结果消费

另开一个终端进入项目根目录，调用 API 查询可用来源及解析内容：

```bash
mkdir -p artifacts/readme-demo

# 1. 发现已发布且获授权的来源
curl --fail-with-body --silent --show-error http://127.0.0.1:8090/sources \
  --header @work/readme-demo/headers.local.txt \
  --output artifacts/readme-demo/sources.json
cat artifacts/readme-demo/sources.json

# 2. 按来源入口 URL 查询已发布 Snapshot 的固定成功内容（取前 5 条）
query_source() {
  curl --fail-with-body --silent --show-error --get http://127.0.0.1:8090/results \
    --header @work/readme-demo/headers.local.txt \
    --data-urlencode "source_url=$2" --data-urlencode 'limit=5' \
    --dump-header "artifacts/readme-demo/$1-headers.txt" \
    --output "artifacts/readme-demo/$1-results.json"
}

query_source dd-017 'https://www.court.gov.cn/shenpan/gengduo/77.html'
query_source dd-040 'https://www.spp.gov.cn/spp/gjgb/index.shtml'
query_source dd-041 'https://www.spp.gov.cn/spp/jczdal/index.shtml'
query_source dd-448 'https://www.chinafilm.gov.cn/xxgk/gsxx/dybalx/'

cat artifacts/readme-demo/dd-017-results.json
```

- `/sources` 返回快照内获当前授权的来源 ID 及冻结入口；
- `/results` 返回规范化内容数组（包含 `markdown`、`metadata`、`attachments` 等），响应头 `Link` 包含下一页游标。

#### 停止服务与清理

在服务终端按 `Ctrl+C` 停止 API。停止本地存储容器（保留数据）：

```bash
docker compose --env-file .env.local down
```

## API 对外接口

以下接口参考保留 main 的调用说明，并与本 Plan 的发布、分页和授权合同同步。初始化或升级后，须先显式 create → inspect → publish；服务不会自动发布。


SEAL 提供轻量只读 HTTP 内容服务（`seal serve`），供下游发现获授权的来源入口（`GET /sources`）、基于快照冻结入口查询已发布的成功内容（`GET /results`）以及下载原始附件（`GET /attachments/{attachment_id}`）。服务仅消费已有 SQL 与 S3 归档对象，不触发实时采集，不产生额外数据副本。

### 1. 服务启动与认证

```bash
# 启动结果服务（默认仅监听本地环回）
seal serve --config <api-config.json> --host 127.0.0.1 --port 8080
```

- **认证方式**：HTTP Bearer Token（`Authorization: Bearer <API_KEY>`）。
- **权限模型**：服务端通过常数时间比对调用方 `key_sha256` 摘要，严格限定调用方可访问的 Source 白名单（如 `["dd-017"]`）。
- **配置与安全**：服务配置详情参见[结果服务 Plan](results-api.md)。响应默认包含 `Cache-Control: no-store` 与 `X-Content-Type-Options: nosniff`。

### 2. 接口定义

#### 2.1 发现可用来源 (`GET /sources`)

查询已发布 Snapshot 中当前调用方获授权且无入口歧义的来源列表，供下游获取合法的 `source_url` 参数。

- **请求方式**：`GET /sources`
- **请求参数**：无（不接受任何 Query 参数，传入参数将返回 `400 invalid_parameters`）。
- **请求头**：携带 `Authorization: Bearer <API_KEY>`。
- **响应格式**：`application/json`，顶级为 JSON 数组，按来源 `id`（C 字节序）升序排序：

  | 字段         | 类型          | 说明                                                    |
  | ------------ | ------------- | ------------------------------------------------------- |
  | `id`         | string        | 来源稳定编号（如`dd-017`）                              |
  | `entry_urls` | array[string] | 该快照冻结且无歧义的入口 URL 列表（按登记顺序去重） |

- **响应示例**：

  ```json
  [
    {
      "id": "dd-017",
      "entry_urls": ["https://www.court.gov.cn/shenpan/gengduo/77.html"]
    }
  ]
  ```

- **特性与边界**：
  - 纯数据库只读查询，不产生网站访问或 S3 读取。
  - 严格排除跨来源重复登记的歧义入口（避免下游请求 `/results` 时触发 `source_ambiguous`）。
  - 已有发布快照时，若调用方未授权任何来源、来源未包含于快照或所有入口均存在歧义，返回空数组 `[]`。尚无发布快照则返回 `503 snapshot_not_published`。

#### 2.2 查询成功内容列表 (`GET /results`)

按已发布 Snapshot 的冻结来源入口查询固定成功 Version，支持游标分页；内部采集更新不会改变返回集合。

- **请求参数**（Query Params）：

  | 参数         | 类型   | 必填 | 说明                                                                        |
  | ------------ | ------ | ---- | --------------------------------------------------------------------------- |
  | `source_url` | string | 是   | 来源登记入口 URL（严格匹配所选 Snapshot 冻结的 `entry_urls`）                        |
  | `limit`      | int    | 否   | 每页返回资源数，默认`20`，范围 `1–100`                                      |
  | `cursor`     | string | 否   | 分页游标，从上一页响应头`Link` 中获取（Fernet 认证加密令牌，有效期 1 小时） |

- **响应格式**：`application/json`，顶级为 JSON 数组。字段遵循严格白名单：

  | 字段          | 类型   | 说明                                                                                            |
  | ------------- | ------ | ----------------------------------------------------------------------------------------------- |
  | `url`         | string | 快照冻结的 Resource URL                                                                                |
  | `markdown`    | string | 提取的 Markdown 正文（无则为空字符串）                                                          |
  | `metadata`    | object | 元数据，包含`title`、`author`、`description`、`keywords`（均为 `string \| null`）               |
  | `links`       | object | `internal` 与 `external` 链接数组（每项含 `href`、`text`、`title`）                             |
  | `media`       | object | `images`、`videos`、`audios` 媒体数组（每项含 `src`、`alt`、`desc`、`type`、`format`、`width`） |
  | `tables`      | array  | 表格数组（每项含`headers`、`rows`、`caption`、`summary`）                                       |
  | `attachments` | array  | 原始下载附件列表，每项含`url`（绝对下载链接）、`name`（`null`）、`media_type`（`null`）         |

- **分页响应头**：
  若存在下一页，响应头包含标准 `Link` 标头；最后一页无此标头：

  ```http
  Link: <http://127.0.0.1:8080/results?source_url=...&limit=20&cursor=...>; rel="next"
  ```

#### 2.3 下载原始附件 (`GET /attachments/{attachment_id}`)

下载采集过程中获取的原始二进制附件（如 PDF、文档等）。

- **路径参数**：`attachment_id`（由 `/results` 响应的 `attachments[].url` 路径中提供，为 Fernet 认证加密令牌，有效期 24 小时，绑定 Snapshot、调用方、来源、Resource、Version 与附件索引）。
- **请求头**：需携带与调用方相同的 `Authorization: Bearer <API_KEY>`。
- **响应格式**：`application/octet-stream` 二进制流（响应头 `Content-Disposition: attachment; filename="attachment"`），下载时实时复算 SHA-256 校验。

### 3. 错误响应与状态码

错误响应统一为 JSON 对象：`{"error": "<error_code>"}`。

| HTTP 状态码 | 错误代码 (`error`)                                                                                 | 触发场景                                                            |
| ----------- | -------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------- |
| 400         | `invalid_parameters` / `invalid_cursor`                                                            | 参数缺失、格式非法、包含多余参数、超界或游标篡改/过期               |
| 401         | `unauthenticated`                                                                                  | 未提供 Bearer Token 或 Token 无效（附带`WWW-Authenticate: Bearer`） |
| 403         | `source_forbidden`                                                                                 | 调用方无权访问该来源                                                |
| 404         | `source_not_found` / `attachment_not_found` / `not_found`                                          | 来源未登记、附件令牌失效或路由不存在                                |
| 409         | `source_ambiguous`                                                                                 | 入口 URL 匹配到多个来源                                             |
| 500         | `archive_corrupt` / `archive_invalid` / `archive_missing` / `archive_too_large` / `internal_error` | 归档对象损坏、字段不合法、对象缺失或超出大小限制                    |
| 503         | `snapshot_not_published` / `invalid_api_config` / `database_unavailable` / `object_storage_unavailable` / `request_timeout` / `server_busy`          | 尚未发布、热配置无效、数据库/S3 不可用、请求超时或并发工作线程已满                        |

旧游标和附件在有效期内跨发布及服务重启继续绑定原快照；每个请求重新读取当前调用许可，撤权立即生效。

### 4. 调用示例

```bash
# 1. 发现可用来源及登记入口
curl -s "http://127.0.0.1:8080/sources" \
  -H "Authorization: Bearer your-api-key"

# 2. 查询内容列表（使用上一步获取的 entry_urls 之一）
curl -s -G "http://127.0.0.1:8080/results" \
  -H "Authorization: Bearer your-api-key" \
  --data-urlencode "source_url=https://example.com/portal" \
  --data-urlencode "limit=20" \
  -D -

# 3. 下载附件（使用返回的附件 URL）
curl -s "http://127.0.0.1:8080/attachments/<encrypted_attachment_id>" \
  -H "Authorization: Bearer your-api-key" \
  -O -J
```

采集附件可配置上限扩大至 128 MiB 后，发布时原件验证使用相同上限；API 默认 50 MiB 保持不变，大附件需显式配置更大预算。真实用例与验证见 [dd-492 Plan](dd-492-pdf-harvest.md)。
