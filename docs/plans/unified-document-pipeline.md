---
kind: plan
lang: zh
---

# 统一文档中间表示与采集解析流水线

依据 [Issue #10](https://github.com/nanzhi84/SEAL/issues/10)，衔接已实现的
[Issue #6 字段合同](recipe-field-contracts.md)。唯一派生物：
[Generated](../generated/unified-document-pipeline.html)。

## 1. 目标、职责与兼容边界

URL → Scrapy 下载 → 原文归档 → Recipe 回调 → 通用解析 → 校验与保存统一文档 →
可选业务提取 → #6 映射、规范化与 Schema → Record。链接发现继续使用现有 Scrapy
生命周期、Discovery、范围、robots、去重、预算和恢复能力；列表页保存与详情发现
分别执行，不以提取出业务正文或生成 Record 为发现链接的前提。

Source 显式选择 `output_schema: document.v1` 并使用文档 Recipe 后进入新路径；
`record.v1`、`generic_document.v1` 和历史 Recipe 保持原入口与导出合同。
统一文档保存不要求业务提取。Runtime 仍是模块化单体，复用 PostgreSQL、Archive、
Snapshot、Observation、Run、Binding、RecipeVersion 与现有资源身份；不增加独立爬虫、
网络服务、OCR、浏览器或业务字段全集。

原文按现行归档合同先保存响应 bytes 和抓取证据，再进入回调。完整原文保留，正文清理、
表格整理和布局简化可以损失信息；结构合法、来源可追溯和业务内容准确分别验证。
技术执行成功及 Schema 通过均不构成内容准确、全站覆盖或业务质量审批，出口继续标记
`quality_status: not_evaluated`。

## 2. 最小文档和五种内容节点

每个 URL 资源对应同一种文档表示，根为 `type: document`，内容是有序 `children`。
根下只允许以下五种节点；附件和列表页使用同一模型，没有 `group` / `data` 节点，
不将正文段落、表格行/单元格或 JSON 字段包装成文档节点。

| 节点 | 合同 | 首版表示约定 |
| --- | --- | --- |
| `article` | `text`、`format: text` | 一份文档最多一个整篇正文；UTF-8 Unicode 纯文本，段落在同一个字符串中 |
| `image` | `url`；可选 `body_hash`、`archive_member`、`caption` | 图片资源引用；内嵌图片指向原容器与成员，未下载网页图片只有 URL |
| `table` | `name`、`rows`；可选 `columns` | 矩形二维严格 JSON 标量；合并覆盖格为 null；名称保留工作表名或解析生成名称 |
| `json` | `value` | 完整合法 JSON，包括对象、数组、字符串、数值、布尔及 null |
| `document_ref` | `document_id`、`url`、`relation`；可选 `name` | relation 为 attachment/link；外键指向另一资源的同类型文档身份；保存时补全外键 |

`table.rows` 不允许嵌套对象或数组；非有限数值、未知节点/字段、非法 JSON 和无效引用
在保存前拒绝。通用附件引用使用 `relation: attachment`。同一目标允许被多份文档引用，
关系图允许重复发现和共享，读取时不递归复制目标内容。文档内容次序由冻结解析规则决定。

```json
{
  "type": "document",
  "children": [
    {"type": "article", "format": "text", "text": "管理办法全文……"},
    {"type": "table", "name": "名单", "columns": ["名称", "所在地"],
     "rows": [["甲单位", "北京"], ["乙单位", "上海"]]},
    {"type": "document_ref", "document_id": "<目标文档身份>",
     "url": "https://example.org/attachment.xlsx", "relation": "attachment"}
  ]
}
```

以上展示内容合同；保存对象还包含身份、Schema、解析规则、原文和实际输入关联。

## 3. 身份、版本、引用与查询

沿用 `seal_document` 的 Source / namespace / URL 身份与原文 Revision 链。URL 处理
复用当前安全序列化规则，保留合法查询表示，不另造业务主键、不重排参数。重复采集同一
资源复用身份；原文字节变化继续产生原文版本。新增不可变文档表示版本及 Run emission，
区分资源身份、原文版本、解析内容版本和本次执行实际使用的版本。

解析版本冻结输入 Snapshot/body、options、解析代码及依赖摘要、RecipeVersion 与 Binding；
同原文使用不同解析规则仍可区分。处理去重键为文档身份、body hash、Binding、parser digest、
options 及影响解释的 Snapshot encoding/response_type/Content-Type，不包含 Observation；
相同输入/规则可复用版本。表示版本保存首次实际原文关联，
emission 保留本次 Run、
Observation、Binding 和输入证据；不同 Run 使用相同内容也不丢失运行血缘。

`document_ref` 保存时先确保目标有同 Source / namespace 的稳定身份，再验证 URL 与
外键对应。目标尚未成功下载时是 `not_fetched`；同 namespace 的 document.v1 Run 中
已归档 200 输入、产生实际解析/校验失败且没有有效表示版本时为 `parse_failed`；目标有
accepted 或有效 staged 版本时为 `parsed`，并返回
`target_version_id`。引用状态是查询时视图，标记 `state_scope: query_time`，不写入不可变
内容；staged 版本也可能来自失败 Run，不能以 parsed 推断目标 Run 已 complete。
引用存在不表示下载或解析成功，查询与导出区分这些状态，
不会将未知/失败目标伪装成成功文档。失败引用返回实际诊断 `reason`；下载 404 仍为
not_fetched，具体请求失败从 Run Discovery 查看。历史仅归档的 raw 不能推断 parse_failed。
错误 Source、URL 与外键组合拒绝，共享目标只存一份。

`inspect document` 查询身份、当前版本和引用状态，`inspect document-version` 查询精确
不可变解析版本。`export SOURCE_ID --documents --run RUN_ID` 导出该次 Run 报告中每个身份
选定的文档 output；全部已保存版本与 emissions 可通过 inspect 查看。
`export SOURCE_ID --documents` 使用最近成功在线视图。只有 `complete` 在线 Run 推进
当前指针；失败 Run 保留已经归档和保存的工件，可按 Run 查看，不覆盖既有成功视图。
Replay 不推进在线指针。默认业务导出的旧 `documents` / `records` 字段不改名；增量提供
`parsed_documents` 和 `unavailable_documents`。`--documents` 单独返回统一文档的
`documents` / `unavailable`。历史对象不重写，也不自动补生成新文档。

## 4. 解析接口与实际格式范围

共享接口为 `parse_document(snapshot, body, *, options=None)`，返回
`{format, children}`；`parser_version()` 对 Python patch 版本、解析模块、复用的
attachment/fallback/core 代码和实际解析依赖版本生成摘要。接口只读冻结输入，不联网。
options 只允许 `body_css`、
`title_css`、`format` 和 `csv_delimiter`；未知键、非法格式或参数在使用前拒绝。
选择器和来源范围由可信 Recipe 提供，默认格式识别综合已归档 Content-Type、URL 和内容。

| 输入 | 实际输出 | 限制与信息损失 |
| --- | --- | --- |
| HTML 详情、列表 | 全文 `article`，图片引用、二维表格、附件 `document_ref` | HTML 清理与站点选择器决定范围；媒体顺序保留，整篇 article 放在正文首个位置；不执行 JavaScript |
| 文本 PDF | 一个 article；可取得的 XObject 图片引用 | 使用 pypdf；文本层必需，无 OCR、可靠表格识别或布局还原 |
| DOCX | 一个 article、普通表格、内嵌图片引用 | python-docx；图引用原容器/ZIP member；嵌套表文本展平；页眉/页脚/注释文本追加 article |
| XLS | 每个工作表一个同级 table | 使用 xlrd；保留工作表名，复用现有单元格转换 |
| XLSX | 每个工作表一个同级 table | 使用 openpyxl；公式保存公式字符串，不计算；合并覆盖格为 null |
| CSV | 整份文件一个 table | csv 实际二维，默认逗号或显式 delimiter；不齐行补 null；不把全文伪装成表格 |
| TXT | 一个 article | 严格按 Snapshot encoding、Content-Type charset 或 UTF-8 BOM 默认解码 |
| JSON | 一个 json | 对象、数组及所有标量/null 均走同一入口，不推断业务字段 |
| 图片 | 一个 image | 校验栅格图像或 SVG 根，关联原图 hash；SVG 不执行、不读取外部实体；无 OCR |

HTML 图片未下载时保留 URL 引用；内嵌 DOCX/PDF 图片指向已归档容器和成员，不将解析出的
图片 bytes 伪装成独立抓取 Snapshot。PDF `archive_member` 表达页内 XObject。
DOC、扫描 PDF、损坏容器和未支持格式明确失败，已经获准归档的原文和诊断保留；
不生成假正文，不以模型能表示某内容推断解析器已支持。
原始输入及 ZIP 展开限制为 64 MiB，表格矩形总计最多 500,000 个单元格；Source 下载预算
仍是前置边界。PDF 加密、无文本层；DOCX altChunk/object/subDoc；损坏图片与非法 JSON
均有明确诊断。JSON 重复对象 key、非有限数及数值溢出拒绝。

## 5. 保存顺序与可选业务提取

Recipe 回调调用同步 `save_document(response, context, options=None)`：读取本 Run/epoch
已归档输入，解析并校验，再持久化
文档表示版本与 emission，返回可引用的已保存结果。业务提取只能在保存成功后执行。
保存层核对 Snapshot、Observation、Source、Binding、Run 和原文摘要的实际归属，并从
冻结输入复算解析输出；不能只因内容树满足 Schema 就接受伪造的正文或引用。

不开启提取时流程到文档保存完成；开启时可信 Recipe 从已保存内容和必要的关联文档取值，
继续使用 #6 的 extraction_schema、field_mapping、field_transforms、data_schema 和
字段证据校验。缺失或可靠解析失败的业务字段仍为 null，不新增字段级状态/原因。

仅保存文档时，可 `yield document_item(response, options)` 交给现有 Pipeline。
[示例 Recipe](../../recipes/documents/recipe.py) 默认 `extract_business: false`；开启后
先同步 save，再对恰有一个 title/body 选择器命中的 HTML 详情生成可空字段 Record。
导航页仍保存文档，不生成伪业务详情。

Record 增加 `document_versions`，记录业务取值实际使用的所有已保存文档版本；跨文档
取值保留全部输入，不只保存当前 URL。新 `kind: document` Locator 定位已保存版本中的
值，Runtime 从原文及冻结解析规则重新生成内容再复算 Locator，随后继续执行 #6 的映射
与规范化。适用的原 HTML/PDF/JSON 等 Locator 保持原校验路径；正文清理后的值不会
强制套用与原始 bytes 文本相等的旧规则。新关联参与新 Record 版本身份，旧无关联的
Record 保持旧 hash 与导出。

每份 `document_versions` 引用包含 `document_id`、`document_version_id`、`snapshot_id`、
`observation_id`，必须是本 Run/attempt 已保存 emission，document.v1 Record 的全部实际
输入均需覆盖。document Locator 形如
`{kind: document, version_id: <版本>, pointer: /children/0/text}`，可选 `snapshot_id`
定位关联输入。JSON Pointer 指向未加入数据库 ID 的解析内容，生成的 document_id 不能作为
业务字段原文证据；Locator 引用的版本必须出现在 Record 的 document_versions 中。

## 6. 失败、恢复和兼容义务

| 场景 | 外部行为 |
| --- | --- |
| 下载、范围/robots、预算或归档失败 | 沿用现有 errors、有限 retry 和 Run failed；未归档输入不能解析 |
| 损坏文件、扫描 PDF、未支持格式 | 保留原文和明确解析诊断，不保存成功表示版本 |
| 未知节点、非法 JSON、错误引用、跨 Run 输入 | 保存拒绝，诊断进入既有运行错误链；不能生成业务 Record |
| 文档成功、业务提取或 Schema 失败 | 文档版本与原文保留，本次 Run failed，旧成功指针不推进 |
| 目标未下载或解析失败 | 来源文档关系可保存；目标状态明确，不能宣称目标成功 |
| 相同 URL、不同解析规则 | 原文身份不变，解析版本按规则/环境与结果区分 |
| Replay 缺失输入或环境漂移 | 明确 `replay_miss` / `environment_drift`，不联网补齐或绕过摘要校验 |

新增 `0008_unified_documents.sql`，增加 `seal_document_version`、
`seal_document_emission`、`seal_document_reference` 及现有文档的当前表示版本外键；
引用目标为 `seal_document` 实际 SQL 外键。迁移不修改已应用 SQL、历史原文和旧 Record。
升级前停 Worker、备份数据库与 Archive，再 `seal db migrate`，重新 pack Recipe 并建
Binding。当前环境重处理旧归档可使用新 Binding 零网络 Replay；精确旧环境执行仍需旧
checkout/venv 和配套数据库副本。保存了摘要不代表打包旧解析引擎；document Locator
在解析代码或依赖 digest 变化后明确 `document_parser_version_mismatch`，只有受记录的
相同环境能复算旧版本。新 Binding 可用旧归档生成新解析版本。旧 worker 不连接升级后的库，
不增加兼容绕过层。
Recheck、attempt fencing、Run/Source generation 与幂等保存继续复用现有机制。
完成事务核对最新原文 body 仍与本次保存表示相同，不一致明确 `stale_document_input`，
拒绝把已有解析结果挂到变化后的原文。

## 7. 实施顺序与验收合同

1. 冻结本 Plan、五节点、解析/保存接口、身份版本、引用状态和失败方式；用真实 CLI/HTTP/PG
   路径先列外部断言与失败场景，保存实现前 RED，不用镜像实现的单元测试替代。
2. 适配已有 HTML、PDF、XLS、DOCX、TXT、JSON，补齐 CSV 实际二维与 XLSX；固定解析依赖。
3. 接入 archive-first 回调和同步保存；提供独立 inspect/export 与目标外键状态。
4. 衔接可选字段合同、所有输入版本和 document Locator；接入 Recheck/零网络 Replay。
5. 隔离新数据库与私有 Archive 执行新路径和现有回归，独立核对原文/解析内容，再核对精确
   PR head 的完整 CI。只有全部要求检查通过才执行已获授权的合并并核实 main。

验收覆盖 HTML 正文/列表、PDF、DOCX、XLS/XLSX、CSV、TXT、JSON 对象/数组/标量/null、
图片；确认只有五种节点，多表同级、真实二维、不拆正文。覆盖未下载/失败/共享引用、列表
继续发现、无业务提取、字段映射/null/类型、跨文档输入、无效节点/引用/配置、归档失败、
解析规则更新、重复 Collect、Recheck、失败 Run 不推进指针和零网络 Replay。
历史 record/generic 路径使用现有 E2E，保留旧导出和迁移/Replay 证据。

## 8. 可重复工件与运行前提

环境为 `.python-version` 的 Python 3.12.13、`uv.lock` 冻结依赖和 PostgreSQL 17；
`initdb`、`pg_ctl`、`psql` 等 PG 工具在 PATH。验收脚本覆盖业务 DSN，创建独立临时 PG、
仅 loopback 的合成 HTTP 站点和新私有工件目录；运行前设置 `umask 077`。
验收工件应保存实际命令、环境、源码/配置摘要、CLI 回执、expected/actual、文档 JSON、
关系状态、业务结果、原文摘要/复算及网络账本；CI 仅上传脱敏元数据。

```sh
umask 077
uv sync --frozen
uv run --frozen python experiments/document-pipeline/ci.py --output work/ci-documents-new
uv run --frozen python experiments/document-pipeline/verify.py work/ci-documents-new/acceptance
uv run --frozen seal inspect document DOCUMENT_ID
uv run --frozen seal inspect document-version DOCUMENT_VERSION_ID
uv run --frozen seal export SOURCE_ID --documents --run RUN_ID --output documents.json
uvx --from seiso==0.3.0 seiso check README.md CHANGELOG.md docs/reference/source-adaptation.md docs/plans/unified-document-pipeline.md
```

各次运行的 `summary.json` 及 `artifacts/ci-public/*.json` 保存实际断言计数、退出码、
独立 oracle 与代码/环境摘要；文档 CI 的原始输入、文档 JSON、关系与回执保存在输出目录
的 `acceptance/`。独立 verifier 不使用 assertions 的 PASS 作为值正确性的 oracle，
从归档 bytes、固定 fixture 值、内容摘要、外键和 Replay 账本核对结果。

## 9. 已执行验证与合并门槛（2026-10-11）

产品代码冻结为 `788c80112636e6991de9b336970967dd6b327f3c`，四套旧路径回归的
44 个源码与锁文件摘要与此受测代码一致；后续验收/文档提交不改产品实现。

| 集合 | 实际结果 | 复跑入口 |
| --- | --- | --- |
| core CLI/PG/Worker | PASS，509/509 断言 | v1.2-acceptance/ci.py --group core |
| offline 迁移、URL、Recheck | PASS，329/329 断言 | v1.2-acceptance/ci.py --group offline |
| seeded 发现、持久化、边界、Replay | PASS，421/421 断言及 75/75 测试 | v1.3-acceptance/ci.py |
| #6 字段、映射、Schema 与证据 | PASS，175/175 断言 | field-contracts/ci.py |
| 新统一解析器边界 | PASS，9/9 测试 | document-pipeline/ci.py 中 parser suite |
| 统一文档 CLI/HTTP/PG 与独立 oracle | PASS，277/277 断言、原文独立复算通过 | document-pipeline/ci.py |

最终本地合计 1711/1711 断言与 84/84 测试，全部通过；其中旧路径 1434 断言和 75 测试，
文档路径 277 断言和 9 解析器测试，各计一次。完整 documents CI 验证九种格式、15 个资源、
五节点、解析规则变更、共享/未下载/失败引用、同业务值但文档版本变化、嵌套 Locator
关联拒绝、Recheck、跨资源全部输入关联以及实际 `recipes/documents` 入口。

独立 oracle 验证 163 个工件、6 个验收脚本摘要、9 种格式、15 份文档、21 个节点、
8 个原文值、2 个实际外键、
2 个跨资源输入及 7 次零网络 Replay，不读取 assertions 的 PASS 作为值正确性的依据。
该轮私有工件位于 `work/document-ci-final/`，`summary.json` 的 SHA-256 为
`75250317194044028e460ee2d97a406fcd65766e5dc72dced85882348f6715d6`；
摘要包含源码 Hash，原文和数据库工件不入 Git。专项实验已纳入完整 CI，不重复累计。

完整复跑使用第 8 节前提，每次选择新目录：

```sh
uv run --frozen python experiments/v1.2-acceptance/ci.py --group core --output work/ci-core-new
uv run --frozen python experiments/v1.2-acceptance/ci.py --group offline --output work/ci-offline-new
uv run --frozen python experiments/v1.3-acceptance/ci.py --output work/ci-seeded-new
uv run --frozen python experiments/field-contracts/ci.py --output work/ci-fields-new
uv run --frozen python experiments/document-pipeline/ci.py --output work/ci-documents-new
```

seiso 与 Generated 本地链接校验通过；Ruff 检查与格式检查属于 lint 门槛。
独立审查覆盖迁移、真实外键、
不可变版本、输入归属、失败指针、Replay 隔离及 Record 关联。
[PR #12](https://github.com/nanzhi84/SEAL/pull/12) 的合并门槛为精确最终 head 的 lint、core、
offline、seeded、fields、documents 六项远程检查全部成功；合并后核对 merge SHA 与 main。
未通过的早期验收工件保持原样，最终结论使用相同冻结产品代码和新目录的结果。
外网样本、全站覆盖、生产规模、OCR、复杂表格质量及业务 Evaluation 不属于隔离合成
验收的结论，[Source 适配参考第 9 节台账](../reference/source-adaptation.md#9-source-适配与审计总表)
不因本次技术验收自动扩展。
