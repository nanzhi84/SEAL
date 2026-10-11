---
kind: reference
lang: zh
---

# V1.3 Review 后八个原始 Seed 的实际观察

源码冻结于 `7b44235ae40ddeb4c192ea86d0c8885363dfb6c9`。开始时间 2026-10-10T21:07:58.255053+08:00，结束时间 2026-10-10T21:09:09.412484+08:00（Asia/Singapore）。

精确 Seed 主机的 `/` 范围，每站 30 请求 / 90 秒、并发 1、间隔 1 秒；继承平台代理和 TLS 校验，没有绕过网络政策或缩小 Scope。

| Source | Run | 结果 | 政策原因 |
| --- | --- | --- | --- |
| dd-017 | `0d171dd8-7679-4e01-b52d-9513339c24a7` | partial / blocked | `robots_unavailable` |
| dd-041 | `9222ee97-33c8-42b7-81ac-85fea960d503` | partial / blocked | `robots_unavailable` |
| dd-250 | `35df8c85-080f-40e9-9e2c-059e3702ddf1` | partial / blocked | `robots_unavailable` |
| dd-468 | `69c838a9-7e24-420f-81ea-0e6669822fca` | partial / blocked | `robots_policy_http_denied` |
| dd-102 | `f2f9c587-4331-45bb-8fda-4056e010f32f` | partial / blocked | `robots_unavailable` |
| dd-247 | `4467b170-ee10-4699-b272-12feb6a10d4f` | partial / blocked | `robots_unavailable` |
| dd-357 | `35d79bad-f52c-463e-b291-92b2aba2e14e` | partial / blocked | `robots_policy_http_denied` |
| dd-009 | `c6c1399c-1623-4565-bcd2-929427b554bd` | partial / blocked | `robots_unavailable` |

共 8 Run、32 Discovery（24 有父 URL）、8 HTTP 尝试、0 Record、2 个代理拒绝 Snapshot。dd-009 另有 1 个原生 `duplicate_request` 候选。

6 个 HTTPS 政策请求遇到代理 TunnelError；dd-468、dd-357 的 HTTP 政策请求得到代理 `403 Domain forbidden`。后者使用 `robots_policy_http_denied`，不表示来源站点规则明确拒绝。两份归档原文的大小和哈希已核验，原文仅留私有归档。

157 项只读 PostgreSQL / 归档独立断言通过。未取得上游正文，真实模板、分页、API、附件、iframe 和搜索适配仍未验证；不以运行计数推断全站覆盖。

完整 Seed、Scope、Binding、Recipe、Run、Discovery、原因与本地证据哈希见 [本轮 JSON](review-live.json)。[首轮 JSON](live-final.json) 保持原样。
