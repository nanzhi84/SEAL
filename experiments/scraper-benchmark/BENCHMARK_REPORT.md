# SEAL 采集框架：同网址配对实测报告

> **已完成一轮真实的相同 URL 采集，不再用 Scrapy 网站抓取对比 Firecrawl 合成上传。**
> 冻结 16 个 URL；取得 D、E 的 **4 个双边同网址结果**。相同业务内容经独立标注核验后可比较，但覆盖有限，不能宣布总体赢家。
> 本轮 **18 次客户端 HTTP/API 请求、9 credits**；包含预检中断产生的请求，没有重置预算。云端源站内部子请求按用户批准的口径记 UNKNOWN。

## 用户选型决定（报告完成后）

**用户已根据本报告决定继续使用 Scrapy，后续以 Scrapy 为主采集框架。**

这是用户的工程选型，不是新增实验结果。原有内容评分、Firecrawl 的已验证能力和证据局限保持不变，不改写成“Scrapy 全面胜出”。机器可读记录见 `selection_decision.json`。本次只记录决定，未修改生产代码、依赖或既有设计文档。

## 1. 证据、环境和判定范围

主工件目录：[`results/paired-live-20261008T095127Z/`](results/paired-live-20261008T095127Z/)。

- `protocol.json`：执行前冻结的完整同 URL/功能清单；未随评分修改。
- `ledger.json`：真实 HTTP/API 请求前预留、响应状态、计时、费用预留；SDK 自动重试为 0。
- `run.json`：全部 16 个案例及双边状态，包括阻断和未执行。
- `scrapy/`：真实 Scrapy Response 字节、状态和下载统计。
- `firecrawl_cloud/`、`http/`：官方 SDK Document、实际 HTTP 响应、调用选项；不是模拟返回。
- `oracle.json`：直接审阅原始 HTML 标注的字段、表单、iframe 和八条有序内容。
- `parsed/`、`quality.json`：双方输出、同一个冻结 Recipe 的离线执行和独立评分。
- `execution_acceptance.json`：执行合同 **41 PASS / 0 FAIL**，不代表所有网站或内容合同通过。
- `interrupted_*`、`executed_source*`：控制器中断及恢复前后代码，不删除失败记录。
- `redactions.json`：403 页面回显的供应商出口 IP 已脱敏，保留原始与脱敏哈希。

环境：macOS arm64、Python 3.14.6、Scrapy 2.19.0、firecrawl-py 4.49.3、trafilatura 2.3.1；没有安装新依赖，没有修改 SEAL 生产代码、数据库或正式设计文档。

### “可比较”分层，而不是一个含混的成功率

| 层次 | 本轮结果 |
|---|---|
| 相同 URL、同轮次的双方成功获取操作 | **4 对**：D_home、D_company、E_shell、E_iframe |
| 独立核对相同业务内容后可评分的快照 | **4 对**；字段、表单、父页身份和名单逐项有证据 |
| 配对开始时间差 | **3.684–20.175 秒**，均小于冻结的 300 秒 |
| 云端缓存年龄独立可核验 | **0 对**；`cache_state`、`cached_at` 未返回 |
| 全套 16 URL 功能验收、全站覆盖或统计性能排名 | **不成立** |

实际发送 `max_age=0`、`store_in_cache=false`，Scrapy 关闭 HTTP cache。它们证明客户端请求了新抓取，不能补造供应商未提供的缓存时间。**本报告比较本次返回的内容，不将其冒充严格新鲜度或框架内核性能证明。**

## 2. 两方使用的同一候选集合及完整结局

| 来源 | 双方冻结的同一集合 | 本轮结局 |
|---|---|---|
| A 最高法 | 2 页列表 + 同样 5 篇指导案例 | robots 返回 200 HTML 软错误；保守阻断双方，共 7 URL 未采集 |
| B 采购网 | 主页 + 官方链接中的真实采购公告 | Firecrawl 先访问主页，API 200 但源站状态 **403**；立即停止双方，公告未采集 |
| C NPPA | 同一原始 PDF | robots 的 302 目标未通过 origin/HTTPS 审查；双方未采集 |
| D Companies House | 主页 + TESCO PLC 同一公司页 | robots 404；双方各完成 2 个 URL，内容可评分 |
| E 国家统计局 | 同一父页 + 同一 iframe 子页 | robots 404；双方各完成 2 个 URL，内容可评分 |
| H 最高检 | 同一列表 + 同一批次详情 | robots 302 的后续地址触发请求规范化校验；第二次发送前阻断，资格 UNKNOWN |
| F / G | 双方不进入运行白名单 | 历史 412 访问限制；没有重试 |

**未改换 URL 来补足成功样本。** B 已选的是 `/cggg/` 采购公告，不再用旧新闻顶替，但访问限制使这项需求仍未验收。A/C/H 的本轮状态是实验政策门禁，不是解析器失败，也不是站点明确禁止采集的法律结论。

H 的控制器中断发生于任何内容页抓取之前。已保留那 8 次请求、0 credits 的记录；恢复只继续未访问的 B/D/E，复用本轮已有的政策结果，没有重查 H、没有重复内容请求、没有重置额度。

## 3. 同功能结果

### 3.1 公司结构化字段：9 对 9，不拿纯文本故意判输

同一 URL：

```text
https://find-and-update.company-information.service.gov.uk/company/00445790
```

| 同一九字段合同 | Scrapy | Firecrawl |
|---|---|---|
| 能完成任务的实际方案 | 既有逐源 Python Recipe | 官方 Scrape 原生 JSON Schema 格式 |
| 独立标注准确性 | **9/9** | **9/9** |
| 申报截止日期与账户期末 | 正确：截止 26 August 2027，不误取 26 February 2027 | 同样正确 |
| 同一个 Python Recipe 解析双方 raw 内容 | **9/9** | **9/9** |
| 共享 Recipe 的字段 XPath/quote | **9/9** 解析到对应快照 | **9/9** 解析到对应快照 |
| 框架原生字段级证据 | 不宣称 Scrapy 原生提供 | 原生 JSON 未提供这些 XPath/quote |
| Firecrawl 实际 credits | — | **5**：此次 JSON 抽取整次调用的实际值 |

九字段为公司名、编号、注册地址、状态、类型、成立日期、业务分类、曾用名、下次账目申报截止日期。两次当前 raw 内容均逐项验证了同一标注，而不是只检查两个提取结果相等。

原协议的“最少代码”组保留了 Scrapy + trafilatura 的纯文本结果，但**纯文本不等于九字段对象，不把缺少 JSON 格式记成 Scrapy 0/9**。同功能比较使用其真实结构化 Recipe；“共享 Recipe”组单独给出。所增加的逐源代码和 Firecrawl JSON 的额外 credits 都没有隐藏。

可得结论：**Firecrawl 在这个样本中确实减少了维护九个字段选择器的需要。** 一次 JSON 输出正确不证明未来页面变化、模型变化时长期都正确；不能推算长期维护工时。

### 3.2 搜索入口：仅识别合同，不冒充搜索流程

同一主页 `https://find-and-update.company-information.service.gov.uk/`。

双方都保留了 `GET /search` 和输入参数 `q`。通过同一小型 HTML 属性适配器，表单 action、参数和 method 均正确。这个适配器属于实验业务层，不算任一框架原生“搜索发现”。

公司 URL 是双方同样预选的目标。**没有执行搜索提交、结果页翻页或从搜索结果动态调度公司详情。**

### 3.3 iframe 父页：自动取得内容与保留来源结构不是同一件事

同一 URL：

```text
https://www.stats.gov.cn/xxgk/list4.html
```

| 同一父页的可观察行为 | Scrapy 原始响应 | Firecrawl 返回 raw_html |
|---|---|---|
| 原来的两个 iframe `src` | **2/2** 保留 | **0/2**，iframe 标签已不在返回 DOM 中 |
| 父页返回内容中包含子页八条人员/日期 | **0/8**，仍是外壳 | **8/8**，子页内容已出现在返回内容中 |
| 冻结的同一个 iframe Recipe | 正确发现两个目标 | 找不到 iframe，来源结构合同失败 |

原生 `links` 中还包含子页的 `index.html#`。去掉 fragment 后，可恢复 **1/2 个原 iframe 目标资源**；footer iframe 的地址没有恢复。这里仅做标准的 fragment 去除，没有假设目录 URL 与 index.html 等价。因此不能将标签缺失误报为“完全找不到子页地址”。

这是两个同时成立的结果：**Firecrawl 带来了直接取得子页内容的便利；但其 `raw_html` 不是保证不变的源站响应字节，不能无条件替代 Scrapy Response 给既有 DOM Recipe 使用。** 不将它表述为“Firecrawl 不会处理 iframe”，也不把 Scrapy 的 200 外壳算正文成功。

未取得 Firecrawl 内部浏览器/请求 trace；只确认上述返回内容变化，不臆测服务内部具体算法。

### 3.4 同一 iframe 子页：通用提取与逐源 Recipe 分开

双方都直接请求：

```text
https://www.stats.gov.cn/xxgk/zzjgxx/gjtjjjld/index.html
```

独立标注是原始 HTML 中 **8 条按顺序排列的职务/姓名、日期及详情链接**，不是另一方的解析输出。

| 相同内容合同 | Scrapy | Firecrawl |
|---|---|---|
| 通用正文输出 | 当前 trafilatura 配置返回空，**0/8 条姓名+日期** | 原生清洗输出保留 **8/8**，顺序正确 |
| 同一个既有 Python Recipe | **8/8**，顺序正确 | **8/8**，顺序正确 |
| 默认标题 | `Document` | `Document` |
| 从业务 `ColumnName` 得到“局领导、总师信息” | 未实现 | 未实现 |
| 通用正文是否只含八条核心内容 | 否，空结果 | 否，另有导航/分页文字 |

所以此处可比较的准确说法是：**Firecrawl 通用清洗优于本次 trafilatura 参数组合；Scrapy 加逐源 Recipe 也能完整提取。** 不能由此说 Scrapy 抓不到该内容，或推断所有法律长文也有同样结果。

同一旧 Recipe 的正文分别为 190 与 1137 个非空白字符：Firecrawl 返回的 DOM 带入了额外脚本/分页等文本，产生不同内容哈希；其原生清洗后为 212 个字符。没有为了这轮评分修改 Recipe 或抹掉差异。

## 4. 原件、Replay、版本和发现边界

- 双方各四个快照的冻结 Recipe/属性适配器离线执行两次：**8/8 确定性一致**。这是同一份留档输入的 Replay，不是重新调用云端八次。
- 公司字段经过共同 Recipe 后的内容 hash 一致；iframe 子页 hash 不一致。可重放不等于不同获取器的表示语义一致。
- 父页 hash 恰好相同，却仍丢失 iframe 字段：旧实验 `content_hash` 仅覆盖标题/日期/正文，**不能作为链接/iframe 合同的完整性证明**。
- 本轮按固定 URL 队列调度。父子关系可离线验证，但**没有实现真实发现驱动的调度器**；A 的旧 40/40 链接结果不能填入本轮。
- 没有实测周期性变化、消失检测、严格同文件名 A/B 去噪或完整 Revision 流程。旧合成结论继续单列。
- 实验仅保存公开源内容和诊断所需元数据；凭据未写入工件。原件来源是 Scrapy Response 或供应商返回内容，不宣称后者是源站 wire bytes。

## 5. 请求、费用与耗时

| 计量项 | 本轮实际 |
|---|---:|
| 共享 robots GET | 6 |
| Scrapy 内容页 GET | 4 |
| Firecrawl Scrape API | 5（含源站 403 的 B_home） |
| Firecrawl 额度查询 | 3（含中断后结算） |
| 客户端 HTTP/API 总计 | **18 / 上限 80** |
| Firecrawl credits | **9 / 上限 30** |
| 余额 | **1396 → 1387** |
| API 文档 `credits_used` 合计 | **9**，与余额差一致 |
| 全部客户端实际开始的最小间隔 | **3.193 秒** |
| 云端源站子请求/内部间隔 | **UNKNOWN，用户已批准此口径** |

B_home 的 API 200 并不代表内容成功：其源站 403 仍消耗了 **1 credit**。其他调用分别为主页 1、公司 JSON 5、E 父页 1、E 子页 1。

仅使用额度查询和 Scrape，没有购买、充值或订阅变更操作。免费账户/不自动付费以前述操作者确认及本次授权为前提；余额 API 本身不证明 billing 设置。没有将历史未知请求量假装清零，本轮独立预算与历史账本分开。

### 保留测量，不做不成立的速度排行

| 同一 URL | Scrapy download_latency（秒） | Firecrawl API HTTP 往返（秒） |
|---|---:|---:|
| D_home | 1.130 | 5.730 |
| D_company | 1.007 | 6.094 |
| E_shell | 0.293 | 9.437 |
| E_iframe | 0.224 | 17.163 |

这两列**计时范围不同**：前者是 Scrapy 下载延迟，后者包含云端清洗/渲染，D_company 还包含 JSON 提取。统一的控制器记录另外保留了 worker 启动耗时；不能据此宣称 Scrapy 内核快某个倍数。

`cloud_fetch.elapsed_s` 包含发送前约 3 秒的限速等待；上表使用真实发送层 `ledger.elapsed_s`，避免把等待混进响应耗时。Scrapy worker 的约 90 MB RSS 不包含离线解析进程；云端内存未知，不比较容量或美元总成本。

## 6. 五个选型问题的当前答案

1. **Firecrawl 能否替代 Scrapy？** 对 D 的结构化字段和 E 的内容取得已有真实支持证据，但 DOM 原件合同存在差异；法律正文、真实公告、PDF 同网址对照缺失，不能作全量替代结论。
2. **还需什么 SEAL 业务层？** 身份、原件持久化、字段合同/证据、Revision、失败处置、完整性检查和 Agent 隔离。原生 JSON 正确并不自动生成这些能力。
3. **Scrapy 的额外实现成本？** 本轮能看到逐源字段/列表 Recipe 的必要性：通用文本提取并不覆盖全部功能。Firecrawl 在 D 省去选择器，在 E 能直接取得子页内容；但不同场景中仍需业务适配。没有实测长期维护工时。
4. **是否应默认混合？** 当前证据不足以证明默认双引擎的运维收益。可以按明确功能合同选择方案，但不能把访问被拒后的换引擎重试当作混合策略。
5. **哪个更适合 Agent 修复？** 两方都有可重复的留档解析；D 的 Recipe 可以复用，E 的 DOM 差异会破坏依赖 iframe 标签的 Recipe。适配边界和验收很重要；没有真实 Agent 修复成功率或安全隔离实测，不能宣称某框架天然胜出。

**实验结论不变：没有证据宣布总体赢家。工程选型已由用户决定：继续使用 Scrapy。** 不恢复此前把有限实验解读为 Scrapy 总体优先的建议，也不因实验覆盖有限而否认用户已经作出的选择。

## 7. 可重复生成的验收与完整性证据

从仓库根目录执行，下面全部离线、无需 Key、不消耗 credits：

```bash
cd experiments/scraper-benchmark
.venv/bin/python paired_acceptance.py results/paired-live-20261008T095127Z
.venv/bin/python paired_quality.py results/paired-live-20261008T095127Z
.venv/bin/python paired_audit.py
.venv/bin/python summarize.py
node ~/.pi/agent/skills/answer-me-with-html/scripts/am.mjs render \
  results/overview.md -o results/benchmark-overview.html --no-open
```

数据前提：保留整个本轮目录、`oracle.json`、固定 Recipe 版本和锁定依赖。`paired_quality.py` 禁止 socket 连接。正文评分有真实失败项，不以 grader 的 exit 0 冒充全部业务合同成功。

现场命令为 `paired_live.py --campaign paired-live-20261008T095127Z`；中断后仅使用一次受限的 `--resume-policy-interruption`，沿用原账本。两版代码已留档。当前在线门禁已关闭，禁止重新生成 ID 来重置这次授权；未来联网需新的明确范围/预算与新目录。

`results/artifact_manifest.json` 覆盖新旧证据，可以按 README 的命令核对 SHA-256。可视化为 [`results/benchmark-overview.html`](results/benchmark-overview.html)。

## 8. 保留的历史与剩余缺口

- 旧的真实留档、本地 HTTP/浏览器、PDF 独立验收仍有效，但只作为独立能力证据。
- 旧本地 **46/46** 未在本轮重跑；旧云合成 Parse 的 **6 API / 4 credits** 不计入本轮 18/9。
- 旧报告与本轮之前的配对计划状态保存在 `results/history/`，不删除曾经的混杂或错误建议。
- 当前优先缺口是解决 A/C/H 的政策资格后，完成**同网址法律正文与 PDF**对照；不是再用合成上传替代它们。
- 采购公告、附件下载、真实搜索遍历、发现驱动分页、真实站点 Playwright 组、OCR、Agent 沙箱及长期修订仍未验收。
