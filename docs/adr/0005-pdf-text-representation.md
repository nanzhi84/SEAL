# ADR 0005：PDF 文字表示合同

状态：采用，2026-10-14。关联 [dd-492 Plan](../plans/dd-492-pdf-harvest.md)。

真实法律 PDF 的 SDK 标题/列表格式化会删除数值165、章节3.3.2.5.4及范围连字符。仅保存本轮 Observation 而复用同字节旧 Version 会使下游继续读取有损正文；直接修改旧 Version 又破坏不可变 Snapshot。

采用原始文字层逐页 `<pre>`，在实际 SDK 结果 metadata 声明稳定 `pdf_text_contract=verbatim_v1`。现有 body_hash 继续只表示获取输入；若当前与新结果的显式 PDF 文字合同不同，成功解析追加新 Version 并推进内部当前指针。同合同、同输入复查保持 unchanged；不比较整个含时间戳的结果 JSON，不改变 HTTP/rendered_dom 比较边界。普通框架或配置变化不触发这一例外。

旧 Version 和已冻结 Snapshot 不变，原附件 CAS 可复用，无数据库迁移，无隐含发布。正文不做 OCR 或数学符号猜测；无效字体码元显式转义并记录诊断。回退处理器只影响未来采集，不能重写历史版本。

## 2026-10-15 决策更新

用户选择恢复 SDK 原生文字转换，将业务清洗交给下游。移除 `<pre>` 单页覆写，当前合同为 `crawl4ai_0_9_5_native_v1`；接受 SDK 标题、空白与连字符转换可能改写内容的取舍，原件可用于重新解析。上文 `verbatim_v1` 为历史决策。合同变化新增不可变 Version、旧 Snapshot 不变的机制继续有效；回滚须同步改变处理器与合同标识。
