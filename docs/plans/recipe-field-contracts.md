---
kind: plan
lang: zh
---

# Recipe 提取、字段映射与任务 Schema

依据 [Issue #6](https://github.com/nanzhi84/SEAL/issues/6)，基线 main `fb5733a`。
唯一派生物：[Generated](../generated/recipe-field-contracts.html)。

## 目标与边界

网站原文 → 可信 Python Recipe 提取 → 可选显式改名/规范化 → 当前任务 Schema → Record。
Runtime 不含业务 key、选择器或全局字段全集；网络、Archive、Locator、Binding、Record
及 Replay 使用现有链路。无字段配置的 Recipe 保留原行为；无 mapping 的任务复用提取
Schema。字段缺失、解析失败或值不唯一输出 null；不新增字段状态、原因、错误码或缺失表。

## 数据和配置合同

Recipe manifest 保持 family/entrypoint/params_schema，允许以下增量：

- extraction_schema：必须有独立 `$id` 与显式 `$schema`，闭合 object 的 properties 定义本任务的 key。
  声明的 key 必须全部输出，默认用类型联合允许 null。required 只表示 key 存在；任务可通过类型禁止
  少数字段为 null。支持 2020-12 / 2019-09；远程引用与未知 dialect 拒绝，不联网取 Schema。
  `$id` 必须是完整合法 URI；包括中间或尾部空格、换行的 URN 都在 pack 明确拒绝。
  复用 JSON Schema 官方支持的 URI format checker 与最小成熟格式依赖，不自行解析 URI。
  依赖固定 rfc3986-validator 0.1.1；原生检查返回 Match，要求其覆盖完整输入，补上库中 `$`
  锚点误收末尾换行的边界。仅用于 Schema 身份，不启用全部 format extras 或修改普通 URL。
- data_schema：可选；无改名与转换时复用 extraction_schema。纯改名的简单闭合合同可派生
  properties/required，并以源 `$id` 加 `:mapped` 作为派生 ID；复杂约束及语义转换必须显式声明。
- field_mapping：源 key → 目标 key，未映射 key 保留。未知源、空目标、重复目标以及保留 key
  碰撞在 pack 拒绝。两份显式 Schema 的 key 与映射结果必须一致。
- field_transforms：源 key → `{entrypoint: module:function, params: {...}, params_schema: {...}}`，
  仅可信冻结 bundle 内的函数；有参数时必须显式声明参数 JSON Schema（同样显式 dialect、
  URI `$id`、闭合 object），并在 pack/bind 前验证严格 JSON、key、类型与约束。无参数规则
  不需额外 Schema。函数定义/签名不合法、prefix/日期格式等参数类型错误是配置错误，pack 拒绝。
  规则 Schema 应与可信函数的参数语义一致；不推断注解或把函数配置异常当成值失败。字段解析异常仅允许
  ValueError/TypeError/OverflowError 转成 null，其他异常沿用 Run 错误；非 null 错误类型拒绝。
- Record 增加可选 data_schema_id，与有效入库合同 `$id` 精确相等；外层 schema_version 继续使用
  现有 record.v1 等外层标识，避免把业务 Schema 的 URN 与外层类型混为一谈。
- 非 null 取值 Locator 可声明 `parser: {entrypoint, params, params_schema}`；Runtime 从归档读取原值，再执行冻结
  Python 函数并严格比较 JSON 值。用于日期→年份等可复算推导，不实现 DSL。

Schema、mapping、Python 函数与参数都在现有 recipe_version 内容摘要中，Binding 引用此版本；
不增加第二份合同或重复摘要。提取/映射/入库数据皆先检查严格 JSON，限制尺寸与嵌套。
新字段合同的 data_schema_id 参与 Record content_hash，Schema 语义 ID 变化即生成新版本；
旧无此字段的 Record 继续原 hash 算法，不改历史版本或 SQL。

## 职责、数据流与兼容

`fields.py` 负责合同与配置校验、显式改名和规范化；`recipes.py` pack 校验并在 Crawl 加载冻结
函数；`record_validation.py` 检查提取 JSON/Schema 和非 null 原文证据，再执行 mapping、转换及
内部 Schema 校验；`records.py` 继续现有不可变候选/版本持久化。源值、源 Locator、原文值、
目标 key 与转换规则写入现有 candidate 的 field_evidence；天然 null 无字段旁路证据。
规范化失败后的 null 保留此前非 null 提取值与证据。export 沿现有 candidate/result_evidence
输出内部 data 和证据；普通旧导出不更名，无 SQL 迁移。

历史 Record 和 export 可读，旧不可变对象不修改。同 Runtime 内重新 pack Schema/Recipe/mapping
后，旧 Binding Replay 应复现原值、Locator 和零网络。升级 Runtime 后旧 bundle 的严格
`environment_drift` 防线仍拒绝；重 pack 同源码并创建新 Binding 可对旧 Run.inputs 零网络重处理。
精确旧版本需旧 checkout/venv 与升级前 DB 副本；旧 worker 不连接新 DB，不增加兼容绕过层。

## 失败方式和边界（实现前）

pack 拒绝无效 Schema、dialect、远程引用、未知 callable/参数和所有 mapping 碰撞；运行拒绝
key 省略、data_schema_id 不匹配、非 null 类型错误、缺少非 null Locator、定位值不符及不可复算
转换。不会把配置错误吞为 null。天然 null、可靠解析失败、规范化失败与多值正常入库；数组
完整保留。无整页或网络失败不生成假成功 Record。无 Schema 的历史 Recipe 继续原 Locator 合同。

## 实施与验收

1. 提交本 Plan、Generated 和 CLI/PG/HTTP 外部行为 E2E，先执行 RED。
2. 实现字段边界与越南可信 Recipe，复用 Record/Archive/Replay，无新状态表。
3. E2E 使用隔离 loopback 两个不同提取来源，共享语义显式映射与无 mapping 的差异化任务；
   验证 null、多值、纯重命名、类型、key、碰撞、规范化和独立原文复算。
4. 越南 48/2026/QĐ-TTg 公开详情真实样本保留三个精确 key（含空格/冒号），机关、日期→年份
   从 raw HTML 独立复算。领域优先详情明确值，无证据保存 null；真实领域检索以公开表单试查，
   不从标题猜测，不以首屏缺少某文档认定全领域缺失。
5. 更新合同后旧 Binding Replay、旧无配置 Recipe 导出不变；运行项目 Ruff/构建及现有相关 E2E。

可重跑：`uv sync --frozen`，Python 3.12 / PostgreSQL 17+ 的 initdb/pg_ctl 在 PATH；
`uv run --frozen python experiments/field-contracts/acceptance.py --output artifacts/fields`。
实时越南：同命令增加 `--live-vietnam`，只访问授权公开页面，通过真实 Runtime 公开表单 POST
获取年份/领域归属。回执只记录非敏感状态/公开标签/docid/正文摘要，不保留隐藏表单参数。
完整 raw 输入仍由 Archive 保留。
工件包括命令、环境/代码摘要、CLI 回执、expected/actual、提取 JSON、data、export、Locator、
独立复算、零网络账本。CI 只发布摘要，不发布真实站 raw/body；全站覆盖、PDF发现/OCR/LLM/UI
均不在本次范围，Schema 通过不表示字段准确、语义或覆盖完整。

## 一手参考

- [JSON Schema 对象与 required](https://json-schema.org/understanding-json-schema/reference/object)：
  required 与 null 分别由 key 和类型约束表达，核对日期 2026-10-11。
- [Dialect 声明](https://json-schema.org/understanding-json-schema/reference/schema)：显式 `$schema`。
- [越南政府文件系统](https://vanban.chinhphu.vn/)：实测详情机关/日期，领域筛选是公开 ASP.NET
  表单提交，2026-10-11 分类 6 + 年份 2026 + 文号 48/2026/QĐ-TTg 没有返回 docid；该范围
  不足以证明所有领域，当前领域 null。

## 越南公开表单的最小受控补充

首轮仅使用详情 GET 与镜像归属列表不能完成真实领域 fallback，因此增加 Source 明确 opt-in
`public_html_forms`，不改变现有 `public_post_rules` 或一般 POST 权限。实测官方表单 40 个
成功控件，body 114184 bytes，VIEWSTATE 92568 / EVENTVALIDATION 12992 bytes；删掉状态后
虽然 HTTP 200，但分类重新为 0，不能据此认定筛选成功。

规则精确冻结 `url`、`parent_url`、`form_xpath`、`fields` 成功控件全集、`select_fields`、
真实 `submit: {name,value}` 与 `max_body_bytes: 131072`。Source/guard 只接受本 Run/epoch
已归档父页，从父 HTML 以同一个 Scrapy FormRequest 重建默认成功控件。hidden/text/checkbox
默认值逐值保真；只有声明 select 可以使用父页真实 options 中的值，认证 Cookie、额外字段、
伪造父证据、改变隐藏状态、改变 action、未知 option 和超界 body 必须在请求前拒绝。

职责位于 Source config / `html_forms.py` / RequestGuard，Recipe 只对显式指定类别与年份发起
有界公开筛选，随后以 selected option 与 docid 归属为字段证据，不枚举全类别或新增表单 DSL。
首次公开父页、POST筛选页、详情页同属现有 Archive/Observation/Replay；回执只保存请求
body hash，不复制隐藏状态。实际 category 6 / 2026 → doc219249 → Chính sách xã hội 的
非 null 领域必须通过真实 CLI路径验收；doc219914缺少可靠归属时保持null。

先提交 `forms_acceptance.py` 的 CLI RED，再实现 guard。E2E 验证原 guard 拒绝 20KB状态，
opt-in 后完整输入得到业务 Record；8个边界全部拒绝且服务器未收到POST；Replay零网络。

Scrapy 2.19 对 FormRequest 发出弃用提示，当前有意复用其原生默认成功控件解析，以保证 Recipe
与 guard 重建算法一致。迁移条件是 Scrapy 移除该 API 或本项目统一采用官方继任库；届时同时
替换两处并重跑公开表单完整 E2E，不维护两套解析规则。

## 已执行验收与复跑入口

实现代码 `a85bfce` 的冻结环境完成字段 CI 158/158；独立 verifier 不读取 assertions.json，
从 Archive SHA256、原始 JSON/HTML、冻结 Schema 与规则复算 16 个导出 Record、35 个字段
证据，并确认 3 次 Replay 无 Runtime 网络请求。它还独立检查无效参数合同的明确拒绝回执。
配置误收 RED 位于 `artifacts/fields-config-red2`：非法 prefix=123 在实现前错误 pack 成功；
前置行为提交与后续实现提交保留在 Git 历史。

实时命令增加 `--live-vietnam --only-vietnam`，2026-10-11 执行 86/86；真实 Run complete，
errors=[]，3 GET + 2 POST，共 5 个归档输入。335/2026/NĐ-CP 的机关为 Chính phủ、年份
为2026、领域为 `["Chính sách xã hội"]`；48/2026/QĐ-TTg 的机关为 Thủ tướng Chính phủ、
年份为2026、领域为null。年份同时保留详情日期推导与筛选列表精确docid归属；领域有值时
保留 selected option 与精确docid归属，null不外推为不存在。独立复算 7 个导出 Record、
24 个字段证据及 2 次零网络 Replay 通过，仍不声明全站或全部领域覆盖。

完整复跑：`uv run --frozen python experiments/field-contracts/ci.py --output artifacts/ci-fields`；
实时复跑：`uv run --frozen python experiments/field-contracts/acceptance.py --output artifacts/fields-live --live-vietnam --only-vietnam`，
随后 `uv run --frozen python experiments/field-contracts/verify.py artifacts/fields-live --only-vietnam`。
每次使用新工件目录，隔离 PostgreSQL 数据与 Archive，执行期间不编辑 engine。实际 head、
环境、代码摘要与 expected/actual 保存在各目录 manifest/receipts/assertions 与独立复算摘要。
CI `fields` suite 仅上传 `artifacts/ci-public/fields.json` 元数据；最终 PR head 的完整 CI 仍需
全部 core/offline/seeded/fields 通过。本地 Ruff、构建与本 Plan 的 seiso 检查已通过。
