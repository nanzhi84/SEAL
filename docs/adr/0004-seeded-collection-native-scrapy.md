---
kind: adr
lang: zh
---

# ADR-0004：Seeded Collection 复用原生 Scrapy 与既有 Recipe

- 日期：2026-10-10
- 状态：接受
- 合同：[V1.3 Plan](../plans/v1.3-seeded-site-collection.md)

## 背景

V1.2 Discovery 已记录 Recipe 发出的 Request，但不会自动扩大人工冻结样本。
业务要求从入口发现公开内容、采用通用 HTML 兜底，并将结果长期保存。
原生 robots 中间件的辅助下载绕过 Scheduler，失败时默认放行，需显式接入现有证据层。

## 决策

采用普通 Scrapy Spider、LinkExtractor 和 Sitemap 解析；所有内容请求进入现有
Scheduler、指纹去重、Scope guard、归档、Record 与完成事务。Discovery 只记录证据。
遍历与解析策略放在同一可信 Recipe 和冻结参数中；Specific 优先，无匹配时 Fallback，
已匹配的解析错误保持失败。通用提取函数由 Runtime 注册并通过 Locator 复算。

seeded Recipe 要求 robots 开启，以原生 RobotsTxtMiddleware 的最小扩展归档政策请求，
只对精确同主机 `/robots.txt` 放宽业务路径检查。政策 404/410 视为不存在，
访问拒绝、暂时失败和不可信政策结果禁止业务下载；预算与地址约束仍适用。
Replay 只使用冻结输入，不发起新的政策或内容网络请求。

Recheck 以冻结计划为边界，关闭首页、Sitemap、普通链接、iframe 和隐式附件扩展；
robots 辅助政策请求继续由 Runtime 决定。明确列入计划的附件目标可以解析，
通用 Recipe 不承诺联动获取说明页面或其他辅助内容。

同一 Crawl 的同一 `(origin, robots Snapshot)` 政策只主动发现一次 Sitemap 声明；
首次候选的父输入是 robots Snapshot / Observation。gzip Sitemap 解析采用受控
解压，大小上限为冻结 Source 的 `response_bytes`，原始下载表示先归档且不改写。
HTTP Content-Encoding 由原生下载器解码时，归档保持现有的应用响应表示合同。

partial Replay 冻结原 attempt 中无 Snapshot 的终态拒绝候选，按请求指纹、角色和
父 Snapshot 精确匹配，复用原原因并保持 partial。新请求、歧义和缺失归档仍严格失败。
已成功消费的同指纹或同 transport chain 输入属于归档正证据，即使早期重试失败的
指纹不同，也不得用负证据掩盖丢失映射。合同存入不可变归档对象，现有 Run report
和 manifest 保存引用；不新增存储模型，也不修改原 Run 或 Discovery。

`robots_denied` 仅表示成功解析的规则拒绝；政策获取的 HTTP 401/403 使用
`robots_policy_http_denied`，连接、代理或解析失败使用 `robots_unavailable`。
后者记录政策事件的终态原因，已有预算或归档拒绝保持其原原因。

所有子请求保留父输入，最终 Content-Type 决定动态附件类型；跨主机附件仍要求显式
路径范围。原生去重产生一个实际下载，Record 的 supplementary_inputs 保留该次
下载的父证据，其他引用关系保留在 Discovery。XLSX 识别为附件但不承诺解析，
保存原文并报告 `unsupported_content_type`。

手动 Collect 复用 Run API、有限重试及持久 PostgreSQL / 内容寻址归档。
正常队列耗尽仅表示已生成请求完成；预算、深度、query 和政策限制须保留原因。
不存在网站内容 Ground Truth 时，运行计数不转化为覆盖率。

## 后果

无需新服务、Scheduler、Frontier、独立解析框架或业务存储模型。
任意搜索空间和需浏览器交互的来源不会自动枚举；缺少可靠解析器的资源留存原文并报告。
robots 辅助请求增加请求与归档计数，查询必须明确这些计数的不同口径。
旧绑定环境摘要继续不可变；升级需要新 RecipeVersion 与 Binding。
