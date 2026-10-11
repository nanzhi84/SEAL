# 统一文档表示与采集解析流水线（Issue #10）

## 目标与边界

对显式启用 `document_ir` 的 Source，在获准归档的 200 GET 与授权公开 POST 响应之后、业务 Record 之前保存可独立查询的统一文档表示。旧 Source 与旧 `seal_document` 业务结果保持原合同；本功能的新身份称 `seal_content_document`，以免混淆。采集范围、robots、请求预算、重试、归档、Replay 仍由现有 Runtime 与 Scrapy 负责。解析允许简化，但不可把未取得的内容写成成功结果。

## 数据流及职责

`InputMiddleware` 在归档成功、登记 Run 输入后调用共享解析器与持久化器。解析器优先使用实际响应的 Content-Type 和可识别的 HTML 内容，必要时按 URL 后缀回退，识别 HTML、文本型 PDF、DOCX、XLS/XLSX、CSV、TXT、JSON、图片；HTML 按响应头或 Meta 字符集解码，TXT/CSV/JSON 首版要求 UTF-8。可信 Recipe 可在请求 meta 中指定 `seal_document_content_css`，必须恰好选择一个 HTML 内容节点，不得改变归档字节。输出同一种 `document`，其有序 `children` 只允许 `article`、`image`、`table`、`json`、`document_ref`，允许空 HTML 的 `children=[]`。`article` 为 UTF-8 文本；表格行是原生 JSON 标量的二维数组。HTML 列表页即使只有链接也要保存；错误页拒绝，解析失败关联实际快照且不能阻止 Recipe 发现链接。同 Source 范围内的 HTML 链接按页面 `<base href>` 解析并形成引用身份，包括无扩展名下载链接；是否成功采集由目标版本另行证明。

身份按 Source、namespace、最终 URL、HTTP 方法和 POST 请求体摘要唯一；HTML 引用默认指向目标 GET 身份。`seal_content_document` 可先作为未解析目标占位；`document_ref` 同时在不可变版本的 children 与关系表中保留，关系表的目标必须有外键身份。目标未下载、解析失败时返回 `pending`，不能声称已解析；成功采集后相同身份上的引用可解析。共享目标不复制内容。版本记录实际 snapshot、observation、body hash、来源格式、Binding/RecipeVersion 与解析器版本；同原文不同解析规则能生成不同版本。业务 Record 的处理身份还包括实际文档版本，避免重新解析后复用旧证据。重复输入以处理键和内容 hash 幂等，当前版本只在完整在线 Run 接受时推进。Replay 重新解析归档输入且不联网。错误仍保留原文，报告明确诊断。

业务 Record 仍遵循 Issue #6 的 field mapping、Schema、null 与 Locator 合同。启用本功能时，Record 必须在其每个实际输入已有成功文档版本后才能保存，并冻结 `document_version_ids`；不开启业务提取的 Recipe 只保存文档。历史 `seal_document`、Result、Record 和导出不迁移或重写。

## 失败方式与边界条件（先于实现与行为测试冻结）

- 归档失败：不解析、不写文档。下载 4xx/5xx：保留现有 Run 失败行为。robots 与 Sitemap 属于调度输入，不生成内容文档。
- 损坏或不支持的格式、扫描 PDF、超限表格、TXT/CSV/JSON 的无效 UTF-8、HTML 错误页：无成功版本；诊断不泄露正文。链接发现与解析独立。
- 不合法节点、越域或私有 URL、错误引用身份、错误 Source/Run/快照归属：事务拒绝，无半成品成功版本。
- 尚未下载、解析失败或重复发现的引用：目标 FK 身份可查询，`resolved=false`；重复目标只一份身份。
- JSON 的 `null`、标量、数组与对象均可保存；CSV 空单元格保留；XLS/XLSX 每工作表一个 table。图片只能引用已有归档 snapshot，不能假称 OCR。
- 解析器规则更新、同输入重复 Collect、Recheck、Replay：证据链和版本区别可查询；失败 Run 不提升当前指针。

## 实施与验收

1. 新增增量迁移、五节点校验和通用解析器；复用 pypdf、python-docx、xlrd，XLSX 使用 openpyxl，图片完整性使用 Pillow 校验。
2. 在已归档输入处接线；完成持久化、引用外键、inspect/export；将业务 Record 输入冻结到文档版本。
3. 隔离 PostgreSQL 与 loopback HTTP 的 CLI 端到端验收，覆盖格式、列表发现、引用、错误、文档独立保存、业务 Record、重复采集和零网络 Replay。工件包含命令、环境前提、输入摘要、CLI 回执、断言、文档 JSON 与原文 hash；仅使用合成公开数据。
4. PR 对最新 head 做代码审核、完整 CI 与工件核验后合并。生产来源的业务覆盖和质量不由合成验收推断。
