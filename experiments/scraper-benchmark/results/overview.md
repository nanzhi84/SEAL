---
title: SEAL 同网址配对实测
subtitle: 用户已选择继续使用 Scrapy · 实验证据与局限保留
lang: zh
theme: paper
---
## A 已确认的选型
```callout ok 继续使用 Scrapy
用户已根据报告决定继续使用 Scrapy。
后续以 Scrapy 为主采集框架。
这是工程选型，不是“Scrapy 全面胜出”的实验结论。
```

同一份 16 URL 清单取得 4 个双边内容对照。
法律正文、公告和 PDF 仍缺配对证据。
原始评分与证据局限保持不变。

## B 同功能结果
| 相同任务 | Scrapy 方案 | Firecrawl 方案 |
|---|---|---|
| 公司九字段 | Python Recipe 9/9 | 原生 JSON 9/9 |
| 同一 iframe 子页通用正文 | trafilatura 0/8 | 原生清洗 8/8 |
| 子页用相同 Python Recipe | 8/8 | 8/8 |
| 父页保留原 iframe 标签 | 2/2 | 0/2 |
| 父页含子页内容 | 0/8，外壳 | 8/8，已含子页内容 |

表单 action、参数和 method 均可保留。
未执行实际搜索遍历。

## C 两个不能混淆的判断
Firecrawl 取得 iframe 内容更省事。
但其 raw_html 没有保留原来的 iframe 标签。
依赖原 DOM 的 Recipe 因此会失效。
原生 links 去掉 fragment 后能恢复 1/2 个目标地址。
不能把标签丢失误报为子页地址完全丢失。

两方都能重放自己的快照。
这不意味着相同 Recipe 跨引擎结果总相同。
字段证据、身份和 Revision 仍属业务层。

## D 没有删掉失败来源
| 来源 | 本轮状态 |
|---|---|
| A | robots 软错误，双方阻断 |
| B | 云端源站 403，停止双方 |
| C/H | robots 重定向资格未解决 |
| D/E | 4 个同网址内容对照 |
| F/G | 既有限制，未请求 |

全部候选和双边状态都留在矩阵中。
不换网址补成功样本，不用旧成绩填格。

## E 实际预算
```limits
客户端 HTTP/API | 18 / 80 | 次
Firecrawl credits | 9 / 30 | credits
```
18 次包含预检中断，没有重置预算。
余额从 1396 降到 1387。
B 的 403 仍消耗 1 credit。
最小客户端开始间隔为 3.193 秒。
云端源站内部请求数记 UNKNOWN。

## F 比较资格与复现
4 对返回内容通过同一独立标注核验。
发送间隔为 3.684–20.175 秒。
已请求禁用缓存，但云端未返回缓存年龄。
因此不做严格新鲜度或速度排名。

执行合同为 41 PASS / 0 FAIL。
内容评分仍保留失败，不能用断言数掩盖它们。
离线复现用 paired_acceptance.py 和 paired_quality.py。
完整报告见 BENCHMARK_REPORT.md。
