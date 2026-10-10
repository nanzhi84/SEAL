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

手动 Collect 复用 Run API、有限重试及持久 PostgreSQL / 内容寻址归档。
正常队列耗尽仅表示已生成请求完成；预算、深度、query 和政策限制须保留原因。
不存在网站内容 Ground Truth 时，运行计数不转化为覆盖率。

## 后果

无需新服务、Scheduler、Frontier、独立解析框架或业务存储模型。
任意搜索空间和需浏览器交互的来源不会自动枚举；缺少可靠解析器的资源留存原文并报告。
robots 辅助请求增加请求与归档计数，查询必须明确这些计数的不同口径。
旧绑定环境摘要继续不可变；升级需要新 RecipeVersion 与 Binding。
