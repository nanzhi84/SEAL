# SEAL V1：Scrapy vs Firecrawl 接力实验报告

> 执行日期：2026-10-08。范围：仅 `experiments/scraper-benchmark/`。
> **最新更新**：用户已提供有效 Key，并单独批准 6 次 API 调用 / 4 credits 的合成 HTML 实验。现已真实执行完毕，恰好消耗 4 credits，没有访问候选网站；密钥未写入工件。下文历史“缺 Key”仅描述先前阶段。最新结果见第 0 节及 `results/credential_update.json`。
> **结论性质：阶段性建议，不是完成了公平对照的最终选型。**
> Scrapy 路线已有真实网站留档样本；Firecrawl 已完成真实云端 Parse 与合成 HTML 对照，但没有同一组真实网站的 Scrape 结果。暂以 **Scrapy + pypdf + 可选 trafilatura** 作为实验基线；不据此宣布 Firecrawl 不能替代，也暂不引入双引擎生产链路。

## 0. 新增：已获授权的 Firecrawl 云端实测

时间：2026-10-08 08:58:37–08:59:02 UTC；全部调用经官方 Python SDK 发到官方 API。
实际命令（密钥只注入子进程环境，不含在命令参数或文件中）：

```bash
.venv/bin/python firecrawl_test.py fixtures --authorization approved-free-fixtures.local.json
.venv/bin/python cloud_acceptance.py results/firecrawl_fixture-20261008-approved1
.venv/bin/python summarize.py
```

- **6/6 API 响应 HTTP 200**：前后额度查询各 1 次，HTML Parse 共 4 次。
- **实际 4 credits**：每份返回 metadata 都报 1 credit；账户余额 1400 → 1396，与逐请求合计一致。
- **0 个候选网站请求、0 次重试、0 次重定向跟随**。请求发送前限额、只允许 credit-usage / parse 两条路径，最短发送间隔 **4.020 秒**。
- 免费账户及不自动付费由用户确认；余额 API 不证明充值设置。没有调用任何购买、充值或订阅变更接口。
- 4 次 Parse 端到端耗时 **1.015–1.249 秒**，中位 **1.066 秒**；不能和 Scrapy 的真实站点或带限速的本地批次横比。
- 四份内容都非空，每份 5 个独立检查的长正文片段全部保留；金额二十万元 → 三十万元的修改可见。
- 四份输出均去掉了页脚访问量、编辑、更新时间和版权；但还包含所在位置、字号和打印导航。
- 返回 `raw_html` 的 UTF-8 字节在本样本 **4/4 与输入相同**，不能外推成所有 Scrape 都保留服务器字节。
- 标题 metadata 包含网站后缀；发布日期在 Markdown 中，但 metadata 为 null。此配置不是 JSON/LLM 字段提取测试。
- **A 与 A-repeat 内容一致**；整个响应中的 scrape_id 不同属于观察元数据，不是正文不确定性。

### 必须披露的变量干扰

本轮分别上传了 `version_a.html / version_b.html / version_c.html`。服务用文件名作为虚拟来源地址，把字号按钮的 `href="#"` 展开为不同 URL。因此 A/B 直接 Content Hash 不同，**唯一 Markdown 差异是这两个派生链接**，不是页脚文字。

这意味着纯“只改页脚”的云端实验还混入了文件名变化。结果标为 **CONFOUNDED_BY_UPLOAD_FILENAME**，不能将它计成 Firecrawl 去噪失败。离线仅统一这些文件名链接后，A/B 完全一致、B/C 仍不同；这只是定位原因，**不是重新执行 API 的成绩**。

预算已用完，未追加请求。入口已修为固定 `filename='document.html'`，尚未再次联网验证。执行时的旧代码另存 `firecrawl_test.executed.py`，真实原始响应和初始判定均保留，避免把改后的代码说成已执行代码。

### Firecrawl 原件 + SEAL Recipe 的离线复验

将四份**真实返回的 raw_html**交给同一个 Python court Recipe，不增加 API 调用：

- 与 Scrapy 本地结果的业务 Content Hash **4/4 一致**。
- A/B 同一修订，B/C 不同修订，标题、日期和正文均可按固定 Recipe 提取。
- 这证明在该合成输入上可以复用 Recipe，不意味着 Firecrawl 原生已经提供这些字段证据，也不证明混合部署有额外收益。

新增工件集中在 `results/firecrawl_fixture-20261008-approved1/`：`http_ledger.json`、`ledger.json`、`http_response_02…05.json`、`fixture_*.json`、`cost.json`、`acceptance.json`。独立请求预算及结果由 `authorization.json` 留痕。

## 1. 执行环境、范围及限制

| 项目 | 实际值 |
|---|---|
| 平台 | macOS 27.0.1 / arm64 |
| Python | 3.14.6 |
| Scrapy | 2.19.0 |
| Firecrawl 官方 Python SDK | firecrawl-py 4.49.3 |
| PDF | pypdf 6.19.0；独立核验 Poppler 26.07.0 |
| 通用正文提取 | trafilatura 2.3.1 |
| 浏览器扩展 | scrapy-playwright 0.0.48 / Playwright 1.63.0 |
| 浏览器 | 已安装 Google Chrome 154.0.8037.98，`channel=chrome` |
| Firecrawl Key / 自托管地址 | 用户提供 Key 已验证；只临时注入子进程。无自托管地址，没有启动或购买服务 |
| 网络出口 | 继承环境存在代理配置，值不记录，不轮换；本轮验收移除代理并只访问 loopback |
| 依赖隔离 | 复用实验已有 `.venv`，未修改生产依赖；锁定全部已安装版本 |

完整环境见 `results/environment.json`、`requirements.lock.txt`。Scrapy 是现有环境内的稳定、非预发布版本，运行确认与 Python 兼容；没有额外查询 PyPI 来证明它仍为当天最新版本。没有编造升级过程或开发工时。

### 接力时发现的历史问题

1. 早期 Scrapy 将 `SETTINGS` 字典误当模块配置加载，限速没有生效。14 个请求及输出保留在 `results/*run1_impolite*`，**不纳入质量/性能对比**。
2. 后一轮 Scrapy 有 15 个响应，日志确认 `DOWNLOAD_DELAY=3`、每域并发 1。该轮没有完整的发送前预算计量；响应时间戳不能精确证明全部发送间隔。
3. A 曾 HEAD 403 后 GET；B 曾 403 后更换 UA；F/G 412 后仍有历史浏览器尝试。这些不是本任务允许的访问限制处理方式。**本轮未重复这些行为，也不将其当“突破反爬成功”。**
4. D robots 获取出现 SSL EOF，却仍取得了公司页。D 的网络政策资格为 `BLOCKED_POLICY`，只能用已存文件分析解析能力。A/C/H robots 返回 HTML 或跳转页面，不等同于经过完整政策审查。
5. 浏览器遗留统计与日志对应不同尝试：有缺浏览器/启动错误的统计，也有 Chrome 启动后 `Page.content` 因持续导航失败的日志。不能把二者拼成一次正常运行。
6. 历史账本声明 55 个请求，后续 Scrapy 为 15 个，合计 **70 个顶层调用**；浏览器子资源、重定向没有完整计量。历史总网络请求量为 **UNKNOWN**，不能声称已经证明未超过 80。
7. 历史记录声称调用过 2 次 Firecrawl 额度/队列接口，但未留下响应证据。授权前阶段找不到 Key，不能据此认定历史云服务可用或历史计费为零；新授权阶段有独立真实计费证据。

因此授权前阶段保持**新增目标网站请求 0、Firecrawl API 请求 0、credits 0**；新批准阶段另计 **6 次 API / 4 credits / 0 个候选网站请求**。未知历史预算没有被清零。原先通过真实已存响应及新的本地 HTTP 路径接力验收。`raw/all/playwright` 等旧外网入口现已 fail-closed，不允许重新运行绕过预算。控制面文档查询另列在 metrics，不伪装成目标采集流量；这些工具内部网络扇出也未计量。

本报告不颁发“整个历史实验合规通过”的结论。解析正确性证据仍然有效，但来源可采集性、全局预算和公平性能对照必须在新的受控实验中重验。

## 2. 实际执行与验收方法

### 本轮执行命令

工作目录均为 `experiments/scraper-benchmark/`：

```bash
.venv/bin/python acceptance.py --baseline   # 修复前：8 PASS / 7 FAIL
.venv/bin/python acceptance.py              # 最终：46 PASS / 0 FAIL
.venv/bin/python summarize.py               # 重生成 metrics/run_log/哈希清单
uv pip check --python .venv/bin/python     # 依赖检查通过，不代替业务验收
.venv/bin/python -m compileall -q bench *.py
.venv/bin/python firecrawl_test.py fixtures # exit 2，缺 Key，零 API 调用
.venv/bin/python scrapy_test.py raw         # exit 2，历史预算未知，零目标请求
```

`acceptance.py` 真正执行：

```bash
.venv/bin/python scrapy_test.py replay
.venv/bin/python scrapy_test.py pdf
.venv/bin/python fixture_test.py raw
.venv/bin/python fixture_test.py playwright
.venv/bin/python list_replay.py
.venv/bin/python quality_audit.py
.venv/bin/python firecrawl_test.py status
```

此外实际执行了 `pdftotext -layout` 和 `pdftoppm -scale-to 1100 -png`，对 PDF 五页渲染图作人工目视核对。不是仅以两个框架输出互相验证。

### 数据与断言边界

- 真实来源质量：独立的 stdlib HTMLParser + 人工检查的原始正文区域、HTML head 标题、标注日期；不调用 Recipe 生成答案再给自身评分。
- 正文完整性：忽略空白后全文完全匹配。它不证明排版、上下标或段落结构完全保真。
- PDF：pypdf 输出对比独立 Poppler 布局文本，逐行检查三列、顺序、页范围；原图保留。
- 列表预期集合：直接枚举已留档 HTML 的目标 href，独立于 Recipe CSS。范围仅为这两页。
- 原始文件不覆盖。Recipe 修复输出另存 `results/replay/`；旧解析错误保留。
- 本地合成数据与真实来源结果严格分开。46 个验收断言并不等于 46 个网站测试。

## 3. 各网站、各方案结果

状态：`PASS_ARCHIVED_SAMPLE` 仅表示留档样本达到内容合同，不代表该站生产授权、全站准确率或实时可用性。

| 来源 | Scrapy / 成熟解析库 | Firecrawl 云 | Scrapy + Playwright |
|---|---|---|---|
| A 最高法 | 留档 2 列表 + 5 详情；样本字段/正文通过。历史有访问政策瑕疵 | NOT_TESTED：本次新增授权只限合成上传 | NOT_TESTED |
| B 采购网 | PARTIAL：主页 + 1 篇财政部新闻通过；采购公告未测试，历史 UA 重试不合规 | BLOCKED | NOT_TESTED |
| C NPPA PDF | 留档原件 107,131 字节；5 页、111 行表格通过 | BLOCKED | NOT_TESTED；PDF 不需要浏览器 |
| D Companies House | BLOCKED_POLICY：robots 失败。离线 6/9 字段修至 9/9；真实搜索流程未测 | BLOCKED | NOT_TESTED |
| E 统计局 | PARTIAL：HTTP 200 是外壳；正文为空，识别 2 个 iframe，未跟进加载 | BLOCKED | 真实 E NOT_TESTED；合成本地 JS/iframe 通过 |
| F 信用中国 | 预探测 HTTP 412，ACCESS_RESTRICTED；没有独立基础 Scrapy 实测 | BLOCKED，且不得拿它绕过 412 | 历史受限站尝试失败且排除；本轮不重试 |
| G 药监局 | 预探测 HTTP 412，ACCESS_RESTRICTED；没有独立基础 Scrapy 实测 | BLOCKED，且不得拿它绕过 412 | 同 F |
| H 最高检 | 留档列表 + 1 篇“第六十三批指导性案例”批次文档通过；30 链接未独立核定覆盖 | BLOCKED | NOT_TESTED |

表中其余 Firecrawl BLOCKED 指真实来源历史未测；Key 当前有效，但未取得真实网站的新预算/政策条件。本轮 Parse 的 4 个成功不转计为这些网站的成功。

没有确认 URL 迁移，也没有将替换地址冒充原入口。D 的 `/company/00445790` 是预选公司，不是通过主页搜索发现；B `/news/202609/t20260928_27408954.htm` 是新闻，不是采购公告。各 URL 见 `bench/targets.py`。

E 的原始 HTML 已公开给出：

- `https://www.stats.gov.cn/xxgk/zzjgxx/gjtjjjld/index.html`：内容 iframe。
- `https://www.stats.gov.cn/xxgk/bottom_new.html`：页脚 iframe。

因此 E 的失败不能直接归因于“Scrapy 不会执行 JS”；先跟进 iframe URL 可能已足够。未确认真实 JSON 数据接口，不猜测接口，更未破解 F/G 的挑战脚本。

## 4. 六项核心实验

### Test 1：单页质量与来源证据

| 指标 | 当前样本结果 | 计算口径 |
|---|---|---|
| 详情标题正确 | 7/7 | A×5、B 新闻×1、H 批次×1；与 head 标题独立比对，去网站后缀 |
| 发布日期正确 | 7/7 | 固定标注日期且日期可在原文验证 |
| 正文完整且无额外文字 | 7/7 | Recipe 正文与原始标注正文去空白后完全相同 |
| 字段证据可解析 | 30/30 | 7 篇×3 字段 + D×9；XPath 引用存在、quote 可回查、snapshot 前缀对应原始哈希 |
| 附件链接 | 7 篇预期均为 0 | 不能把“0/0 没遗漏”当附件采集能力通过 |
| 原始 HTML | 有 | 保留 Scrapy Response.body；它可能已解 HTTP 压缩，并非 TLS/网络线缆字节 |

正文非空白字符数依次为：A **2,905 / 2,877 / 4,696 / 4,156 / 2,529**；B **692**；H **20,825**。`results/quality.json` 提供具体核验与 oracle hash。

公平补充：**Scrapy + trafilatura** 不需要逐源正文选择器。在相同 7 篇上，严格标题匹配 **5/7**、日期 **7/7**、去空白全文精确匹配 **5/7**；所检查的长度不小于 8 字的正文片段均可在其结果找到，但这不证明所有短标题/短片段都完整。B/H 的严格完整性与标题差异仍需处理。不能把 Scrapy 描述成只能纯手写提取。

Firecrawl 没有这 7 份真实详情的内容结果，不填 0% 成功率；新增的四份合成 Parse 结果另列第 0 节。

### Test 2：发现、分页与来源关系

- A 两页各 20 个目标链接，去重后预期 **40**，发现 **40**，匹配 **40**，页面内覆盖率为 `40 / 40`。
- 第一页 `next_page` 与真实留档第二页 URL 一致，5 个详情 URL 均可在第一页原文找到。
- **全站覆盖率 UNKNOWN**。没有遍历所有页，也未评估最新发布漂移或消失检测。
- 原始网络 Spider 预先种入详情 URL，所以原始记录不证明“发现后自动抓详情”的完整调度路径。
- 本轮补充 `list_replay.py`：只种入第一页，通过真实 localhost HTTP 回放同一组原始 HTML，实际发现、翻页、调度 5 详情，形成 5 条 parent→detail 边；**7 个本地请求**，全部正文与留档一致。
- 这证明工作流接线，不补记成一次真实外网站点 Crawl。

Firecrawl 可用官方 Crawl/Map，也可用逐列表 Scrape 的 `links/rawHtml`。保留的 `list` 模式选择后者，以限定为 2 列表 + 最多 5 详情、显式保留关系，不实现一个自定义 Firecrawl 爬虫。该 API 路径尚未运行，覆盖率 UNKNOWN。

### Test 3：PDF、JSON、TXT

原始 PDF SHA-256：

```text
48aac4d1a8230aec91ec160a3fc4bcfe77fa5c8179c483d4e23a22445f2c683a
```

- 5 页均有嵌入文本，非扫描件；pypdf 本轮解析 **0.038 秒**。
- 表格三列为序号、单位名称、SID 码号段；复原 **111/111** 行，与 Poppler 逐字符去空白核对一致、顺序一致。
- 每页序号范围：`1–22 / 23–49 / 50–76 / 77–103 / 104–111`；未发现此样本的阅读顺序错乱。
- 页级字符定位：原实现第 2–5 页有偏移错误，修复计入连接换行后，**5/5 页文本切片精确一致**。
- `pypdf` 是第三方解析能力；111 行表格的窄格式正则以及 snapshot/page/span 包装是实验增加的代码，不是 Scrapy 自带表格识别。
- 不推论扫描件 OCR、复杂合并单元格、跨栏法律 PDF 都能处理。
- JSON/TXT 只测试标注为 SYNTHETIC 的本地 HTTP Fixtures：3 条 JSON 文档及附件关系、TXT 字符定位通过。没有真实授权 API 成绩。

官方 Firecrawl 当前文档支持原生 PDF 解析、页码、`pages`、`blocks`、bbox / markdownSpan，以及上传本地 HTML/PDF 的 Parse。`parsers: []` 文档还支持返回原 PDF base64。**不得声称 Firecrawl 天生没有页级证据或无法拿 PDF 原件。** PDF/blocks/base64 在本环境仍只有文档依据；本地 HTML 上传 Parse 已在第 0 节实测。

### Test 4：动态页面

本轮没有重访受限 F/G，也没有继续消耗未知历史预算访问 E 的子资源。

额外本地合成页面：空 `<main>`，JS 异步写入 `JS_RECORD_001`，iframe 内有 `IFRAME_RECORD_002`。

| 组 | 结果 |
|---|---|
| Scrapy 基础 | 原始 HTML 的 script 含目标字符串，但可见正文没有目标；正确记录未取得渲染数据 |
| Scrapy + Playwright | 真实浏览器取得主页面和子 frame 目标；2 个本地请求，逐请求计量、串行、间隔不小于 3 秒 |
| Firecrawl | BLOCKED |

浏览器输出是渲染 DOM，不能标成服务器原始 HTML。额外组件为 Playwright、scrapy-playwright 和 Chrome。浏览器初次本地调试的 context-route 拦截优先级不正确，子请求未限速；已保留 `fixtures_playwright_unpaced_local.json`，修为 page-route 并重新验收。仅涉及 loopback，不是新的站点流量。

### Test 5：来源追溯和版本变化

受控 A/B/C/A-repeat 均经过真实本地 HTTP → Scrapy Response → 同一 court Recipe → 结果与哈希。

| 比较 | Raw Hash | Content Hash | 意义 |
|---|---|---|---|
| A → B | 不同 | 相同 | 仅页脚访问量、编辑、更新时间变化；不产生正文修订 |
| B → C | 不同 | 不同 | 赔偿金额二十万元改三十万元；产生正文修订 |
| A → A-repeat | 相同 | 相同 | 固定输入重放一致 |

**4 个 FetchObservation、3 个不同 RawSnapshot、1 个 Document Identity、2 个 Content Revision**。标题变化也进入 content hash；正文里的业务时间戳不会被随意删除。trafilatura 在该合成样本也保留了相同的正文变化。

新增能力明确归属 SEAL 实验层：identity、canonicalization、hash、revision 关联、Result 与 Field Evidence。

- Snapshot：字节哈希与原件。
- Observation：每次取回都记录，包括内容没变的情况；本例只演示结构，不实现生产观察数据库。
- Identity：合成文档用固定逻辑 URL；真实文档合并/重定向/外部 ID 策略未验收。
- Revision：标题、日期、正文规范化后计算，页脚不在 Recipe 正文区域。
- Evidence：XPath + snapshot + 引文；不是所有字段都具备字节级 offset。

Replay 对 **14 份 HTML** 重复解析均一致；与历史业务字段比较，**13 份不变，D 因修复变更**。这应记录为 Recipe/extraction 版本改变，而不是来源正文修改。PDF 定位错误同样属于解析器修复。

风险：NFKC 与空白折叠是演示规则，可能掩盖法律排版、全半角或公式语义差异；没有证明通用“完全不会误判”。未实现真正的定期调度、消失文档、完整性闸门和 Revision Timeline 服务。

Firecrawl A/B/C/A-repeat 已通过官方 `/parse` 实测。固定 A 重复结果一致、页脚被移除；A/B 因上传文件名干扰，严格去噪判定仍待固定 filename 重验，详见第 0 节。清洗算法跨版本漂移仍 UNKNOWN。Parse 不等同于含浏览器渲染的 Scrape。

### Test 6：开发维护与执行资源

当前逐源 Recipe 核心函数的物理行数（AST 起止行，含函数内注释/空行；不含共享框架、测试、报告）：

| 来源 | 行数 | 实际维护点 |
|---|---:|---|
| A | 45 | 列表、日期、分页、详情正文选择器 |
| B | 21 | 当前只覆盖新闻模板，不覆盖所有公告模板 |
| D | 59 | 公司字段、历史名称、申报截止日期、字段证据 |
| E | 11 | iframe 枚举；未实现跟进策略 |
| H | 29 | 列表和批次正文 |

总代码明细见 metrics；不拿含审计/预算/验收脚手架的总行数和 Firecrawl 的“一行 SDK 调用”作不公平比较。

首次成功的真实操作记录不完整，故不报告开发小时数、操作步骤差或“快几倍”。可见的维护事件是 D 缺失 3 字段和 PDF 跨页 span 被验收发现后修复，原始快照无需重抓。

| 测量对象 | 实测值 | 限制 |
|---|---:|---|
| 历史 Scrapy 15 请求 | 20.051 秒 | 混合网站、包含限速，政策审计不完整 |
| 单请求 download latency | 中位 0.467 秒，范围 0.076–3.720 秒 | 一轮样本，不能与不同输入的 Firecrawl Parse 比快慢 |
| Scrapy response_bytes 统计 | 500,569 字节 | 与解压后的留档 body 556,108 字节口径不同 |
| 历史 Scrapy 抽样 RSS 峰值 | 87,949,312 字节 | 不是全机器成本 |
| 本地合成基础组 | 7 请求，18.627 秒，Python RSS 94,175,232 字节 | 合成 HTTP，含 3 秒间隔 |
| 本地浏览器组 | 2 请求，18.190 秒，Python RSS 99,598,336 字节 | 不含浏览器进程树 RSS；不是同等负载性能比较 |
| 本地列表 Replay | 7 请求，18.612 秒 | 原始网页的本地回放 |
| 实验 venv | `du -sk` 为 290,928 KiB | 共用实验环境，不等于 Scrapy 单独安装体积 |
| 新授权云实验开销 | 6 次 API、4 credits、未发起付款 | 余额差与 4 个文档计费一致；历史账单 UNKNOWN |

## 5. 综合对比表

`文档`表示当前官方文档确认但本轮未实测；`SEAL`表示必须由业务层增加。

| 指标 | Scrapy | Firecrawl | Scrapy + Playwright |
|---|---|---|---|
| HTML 采集成功情况 | 历史 15 响应；7 详情内容独立通过，不能记成 15 文档成功 | 真实站点未测；合成上传 4/4 有效内容 | 真实来源未形成有效正文成绩 |
| 正文完整性 | Recipe 7/7 精确；trafilatura 5/7 严格全文匹配 | 合成每份 5/5 长片段；仍含导航，真实站 UNKNOWN | 本地 JS/iframe 通过，真实站 UNKNOWN |
| 列表发现覆盖 | 留档两页 40/40；全站 UNKNOWN | UNKNOWN；内置 Crawl/Map/links | NOT_TESTED |
| PDF 提取质量 | pypdf + 窄表格解析：111/111 行、5/5 页定位 | 原生 PDF/pages/blocks 文档支持；质量 UNKNOWN | PDF 无需此组件 |
| 动态页面支持 | 不执行 JS；可跟进公开 iframe/API | 托管渲染/交互文档支持 | 本地真实 Chrome 通过；不得处理访问限制为“渲染失败” |
| 来源证据保留 | Response bytes；SEAL XPath 引文 30/30 可回查 | 合成 Parse raw_html 4/4 字节一致；可用 SEAL Recipe 重放。真实 Scrape/PDF 待测 | 渲染 DOM 与网络 raw 必须分开保存 |
| 版本变化识别 | SEAL Fixture A/B 稳定、B/C 变化 | A-repeat 稳定、金额变化可见；A/B 受 filename 干扰，不能判去噪失败 | 仍需要 SEAL，不天然解决渲染噪声 |
| Recipe 灵活性 | Python 可控、离线 Replay 实测通过 | Python 可编排 SDK；API 选项有边界，清洗内核云端不受 Recipe 控制 | 在 Python Recipe 基础上增加浏览器动作维护 |
| 开发代码量 | 逐源核心 11–59 行，另有共享层 | 零个实测接入来源，不能量化省多少代码 | 本地样例已写，真实站维护量 UNKNOWN |
| 执行耗时 | 历史 20.051 秒/15 请求 | 合成 Parse 单次 1.015–1.249 秒；真实站 UNKNOWN | 本地 18.190 秒/2 请求；不可横比 |
| 请求与服务成本 | 本轮重放不消耗目标站请求；机器成本自行承担 | 新批准实验 6 次 API / 4 credits | 增加浏览器资源与子请求，必须独立计量 |
| 部署复杂度 | Python 进程 + 所需解析库，无必需外部队列服务 | 云端客户端简单；自托管明显更多服务 | 多 Chrome/Playwright 生命周期、版本与隔离 |

### 云端、自托管和本地成本的区别

- **Firecrawl 云端**：无需自己运营渲染集群，但增加 API 配额、网络依赖、内容上传边界、云端提取版本漂移和供应商治理。单个 API 调用可能产生多个目标请求，客户端 `sleep(3)` 并不能约束云端所有子请求。
- **Firecrawl 自托管**：官方当前自托管示例固定 `v2.11.162`；包含 API/worker、Playwright、Redis、PostgreSQL、RabbitMQ 等组件。未部署，不能给内存、CPU 或月费数字。官方明确默认栈不等于 Cloud 全功能，某些 actions/screenshots 等需要额外 Fire-engine；必须按目标版本重新核对，不能假定部署免费即全功能等价。
- **Scrapy**：核心不要求外部服务；持久化、调度和 SEAL 业务系统是另外的成本。PDF/trafilatura 为本地依赖，浏览器按必要来源增加。
- 当前官方 billing 文档列普通 scrape 基础 1 credit；PDF、结构化 LLM 等按附加项计费；Parse 的 PDF 文档列每页计费。数字可能变化且接口口径不同，必须以前后额度、每请求 metadata 和账单核验。本轮仅 HTML Parse 的 1 credit/份及 4 credits 合计经过计费响应确认；不把其他文档价格当实测费用。

## 6. 对 SEAL V1 的建议与五个问题

### 问题一：Firecrawl 能否替代 Scrapy 作为主要采集框架？

**本次无法证实能够完整替代，也无法判定不能替代。**

其文档能力可以覆盖许多获取/清洗需求。本轮云端 Parse 已证明可以取回合成原件并复用本地 Recipe，但真实网站的发现、获取、PDF 和成本对照仍缺失，不能宣布全面替代或不能替代。

若 V1 必须现在推进，采用 Scrapy + 成熟解析库作为**可撤销的实验基线**，将 Firecrawl 留在候选名单；不要把这写成双方质量/性能的最终胜负。未修改 SEAL 正式架构。

### 问题二：选择 Firecrawl 后，SEAL 仍需自研什么？

1. 来源登记、授权/robots 审核、URL 白名单、真实预算、限速与失败分类；不能默认云 API 符合本任务的全部网络约束。
2. 跨源统一 schema、严格校验、字段证据与 snapshot 关联、原件持久化；对原 PDF base64 也要独立保存和校验。
3. 列表范围/分页完备性、详情身份、附件父子关系、重复采集幂等与可靠的消失判定。
4. RawSnapshot / Observation / Identity / Content Revision / Result 数据模型，提取器与 Recipe 版本、业务语义哈希、Revision Timeline。
5. Replay 数据集、验收闸门、错误隔离、回滚、可观测性及发布审批。
6. Agent 的隔离与权限边界。

不重造其已内置的网页清洗、Markdown、Crawl/Map、PDF/OCR/页级块提取。即使采用其 change tracking，也需验证和映射到 SEAL 合同，不能直接把供应商 diff 当最终业务修订。

### 问题三：选择 Scrapy，额外开发成本在哪里？

主要是逐源发现/分页与正文/结构化字段 Recipe、模板变化诊断、编码与异构附件处理、必要时的浏览器集成，以及原件到字段证据的映射。通用正文用 trafilatura、PDF 用成熟库，减少不必要的自研。

队列/请求调度、去重、中间件和 Response/Selector 不必重造。前一问题中的 SEAL 业务合同仍然存在，不应把双方共同需要的业务代码算成 Scrapy 独有负担。

### 问题四：混合是否有收益？

**当前没有实测边际收益，V1 不应默认双引擎。**

未来仅当同一组允许采集的复杂来源证明 Firecrawl 明显降低维护量、增加有效完整内容，且预算/原件/证据合同可满足时，才值得作为按源指定的获取后端。不要实现“Scrapy 403 → Firecrawl 自动兜底”，这既可能规避限制，也污染失败统计。

代价包括两套调度/额度、不同原件语义、提取漂移、缓存时效和差异归因。应统一下游业务合同，而不是合并两份不一致的正文或双写 Revision。

### 问题五：未来 Agent 自动修复 Recipe，哪个更适合？

**更关键的是纯 Python Recipe 与不可变原件的合同，不是采集品牌。** Scrapy 对应的 D 修复已有真实网页留档证据；新增实验中，Firecrawl 返回 raw_html 也能复用同一 Recipe，4/4 业务哈希与 Scrapy 一致。后者仅为合成样本，不能外推真实网页获取能力。没有运行真实 Agent，也没有验证安全沙箱。

Firecrawl 同样可以由 Python Recipe 编排、调整 include/exclude tags 或解析返回 HTML；其内部云端清洗算法变化不能完全由 Recipe 修复或离线锁定；但本轮已确认对所存 raw_html 自行解析可以离线重放，应区分这两层。自托管是否可固定版本、能否同成本重现，尚需实测。

两者都不会自动提供 Agent 安全边界。建议边界（仅报告建议，未改生产）：Agent 只提修改；受信工作流加载批准的 Recipe 版本；Recipe 接收不可变 snapshot/有限请求描述；独立进程或更强沙箱默认无网络、无 API Key、无生产数据写权限；校验器/预算/调度/发布策略不可由 Recipe 配置覆盖。仅靠约定、Python import 白名单或普通子进程，不足以隔离恶意代码。

### 两个使用场景

- **最少代码最快得到内容**：Firecrawl 值得下一轮重点验证；单 SDK 调用的文档体验不等于已测收益。同时 Scrapy + trafilatura 的样本表现说明，不能将它只与手写提取器比较。
- **长期采集、精确追溯、Recipe 修复**：本次证据支持本地可重放 Recipe 路线。Firecrawl 已补了单个合成固定输入的一致性和原件回放；仍需补证真实原件语义、字段 grounding 和清洗版本变化；采集器选型不能替代 SEAL 自身设计。

## 7. 足以继续决策的最小补充实验

凭据有效及本次免费额度已确认，6 次调用 / 4 credits 的授权已全部用完。继续联网须批准**另一个独立预算**；不能通过改旧计数器把未知历史清零。

1. **最小去干扰复测**：固定同一 filename，仅重测 A/B 两次 Parse，再做前后额度查询，需另批最多 4 次 API / 2 credits。本次 A-repeat 已一致，原 A/B 只差派生链接，但离线替换不能取代这个受控网络结果。
2. **同一批允许 URL**：A 两列表 + 同样 5 详情；B 更换为经官方导航确认的真正公告；D 先解决 robots 和搜索授权；C 同一 PDF；E 只在允许时加载其 iframe。F/G 仍禁止重试限制。
3. A 7 页、B 1、D 1、C 5 页 PDF、E 1，加固定 filename 的 A/B 两个 Fixture，按现有文档可保守预留 **18 credits**；以实际接口计费规则复核，不保证一定按此结算。Scrapy/Firecrawl 采用相同文档集合；浏览器第三组只用于真正需要且未受限的页面。
4. 给真实子请求可见性和限速建立可验证边界。云 API 若无法证明所有目标请求满足并发/间隔/总量，严格实验必须保持 `BLOCKED_POLICY`，选择受控自托管或由用户明确调整约束，不偷偷把“80 次请求”改成“80 次 API 调用”。
5. 用本报告同一 independent oracle 评分并保存原件、原生结果、前后 credits。仅在此后讨论主采集框架替代或混用。

实际采购公告、附件下载、扫描 PDF、公共 JSON API 和定期“消失”判断是后续业务验收缺口；不能只靠以上最小样本宣布整个 SEAL V1 完成。

## 8. 工件索引与证据分级

| 路径 | 内容 |
|---|---|
| `results/raw/` | 历史网页/PDF/robots 原件；本轮本地快照在 `raw/fixtures/` |
| `results/parsed/*.raw.json` | 历史解析输出，含待审慎使用的旧粗略 `_checks` |
| `results/replay/` | 修复后 HTML 业务字段及证据 |
| `results/quality.json` | 独立字段、全文、列表、PDF、证据定位核验 |
| `results/discovery_evidence.json` | 两页完整预期集合、实际集合、父子关系 |
| `results/pdf_quality_evidence.json` | Poppler vs pypdf 的 111 行逐行数据 |
| `results/pdf-page-1.png` … `pdf-page-5.png` | 原 PDF 渲染图，可目视复查 |
| `results/acceptance_baseline.json` | 修复前 8 通过 / 7 失败，缺口未掩盖 |
| `results/acceptance.json` | 最终 46 通过 / 0 失败，含实际子命令日志路径 |
| `results/fixtures_raw.json` / `fixtures_playwright.json` | 合成数据结果、哈希、差异、浏览器子请求 |
| `results/list_replay_e2e.json` | 真实留档数据的本地列表→详情 E2E |
| `results/firecrawl_status.json` / `network_blocked.json` | 历史环境检查/旧外网阻断；不能覆盖新批准实验结果 |
| `results/firecrawl_fixture-20261008-approved1/` | 6 次真实请求、4 个原生响应、费用及含变量干扰说明的验收 |
| `results/metrics.json` / `run_log.json` | 机器可读指标、历史计数、不确定性及执行索引 |
| `results/artifact_manifest.json` | 原件、Fixtures、解析结果 SHA-256 清单 |
| `results/sensitive_scan.json` | 敏感凭据模式扫描，无命中；不声称能检出所有敏感数据 |
| `results/documentation.json` | 官方文档取回存档；与实验事实区分 |
| `logs/` | 原运行日志、失败日志、按时间命名的最终验收日志 |

已得到实测支持：留档正文质量、两页发现、PDF 本样本质量、30 份字段引用、本地固定输入稳定性、D Recipe 修复、合成浏览器渲染路径；新增 Firecrawl HTML Parse、4 credits 费用、单个固定输入一致性、原件回放与 Python Recipe 复用。

尚未得到实测支持：Firecrawl 相对 Scrapy 的真实站点质量/成本优势、固定 filename 的原生 A/B 去噪、自托管运营成本、混合收益、真实站浏览器收益、Agent 自修复安全性、全站覆盖率及整个 SEAL 生产工作流。

### 参考资料（文档能力，不当作实测）

- Scrapy 官方文档：<https://docs.scrapy.org/en/latest/topics/settings.html>；Context7 `/scrapy/scrapy`。
- Firecrawl 文档：<https://docs.firecrawl.dev/features/parse>、<https://docs.firecrawl.dev/billing>、<https://docs.firecrawl.dev/contributing/self-host>；Context7 `/firecrawl/firecrawl-docs`。
- Python SDK 安装类型检查确认 `scrape`、`parse`、`start_crawl`、`map` 和 PDF options；HTML Parse API 成功路径现已真实运行，其余 API 成功路径仍未运行。

原始公开网页可能含法律文书公开姓名或公司信息；不包含运行环境的 Key、Cookie、代理地址或认证头。未修改 SEAL 生产代码、数据库或现有设计文档。
