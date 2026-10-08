# SEAL 采集框架实验

所有内容只属于 `experiments/scraper-benchmark/`，不自动发布到 SEAL 生产环境。

## 已确认的选型

**用户根据报告决定继续使用 Scrapy，后续以 Scrapy 为主采集框架。** 决定记录在 `selection_decision.json`，并已同步报告和可视化。实验数据及局限不变；本次未修改生产代码、依赖或既有设计文档。

## 当前实验结果

本轮已按用户确认的新口径完成真实配对：**最多 80 次客户端 HTTP/API 请求、30 credits、仅免费额度；云端内部源站子请求记 UNKNOWN**。

- 冻结 16 个共同 URL，保留全部双边状态；D/E 得到 **4 个同 URL 内容对照**。
- 实际 **18 次客户端请求、9 credits**，包含一次政策预检中断，没有重置预算。
- 公司字段：Scrapy 的逐源 Recipe 与 Firecrawl 原生 JSON 都是 **9/9**。
- 同一 iframe 子页：当前 trafilatura 配置 **0/8** 条内容；Firecrawl 原生清洗 **8/8**；相同 Python Recipe 两方均 **8/8**。
- Firecrawl 父页返回内容已有子页内容，但原 iframe 标签丢失；依赖源 DOM 结构的 Recipe 不可无条件移植。
- 云端缓存年龄未提供，不能把返回内容评分当作严格新鲜度/速度排名。法律正文和 PDF 本轮未形成配对，**没有总体赢家**。

报告：[`BENCHMARK_REPORT.md`](BENCHMARK_REPORT.md)；可视化：[`results/benchmark-overview.html`](results/benchmark-overview.html)。

本轮目录：`results/paired-live-20261008T095127Z/`。`results/current_paired_campaign.json` 指向当前工件，不含密钥。

## 设计与边界

数据流：同一冻结 URL/字段合同 → 双方真实获取 → 独立留档 → 原生/成熟库提取及相同 Python Recipe → 人工标注 oracle → 离线评分。

职责分离：

- `paired_live.py`：共同队列、共享政策预检、串行调度；只允许白名单来源及官方 SDK API。
- `paired_fetch.py`：每次仅一个真实 Scrapy Request/Response；禁重试、重定向、缓存，启用 TLS 校验。
- `bench/paired_meter.py`：SDK 真实 HTTP 发送层计量、发送前预算预留、禁止隐式重定向；不宣称掌握云端内部请求。
- `bench/recipes.py`：只解析输入、URL 和 snapshot ID；不决定网络授权、预算或发布。
- `paired_quality.py`：独立内容评分及同 Recipe 回放，禁止 socket 连接。
- `paired_acceptance.py`：先于采集实现编写的外部工件行为验收，不模拟服务。
- `paired_audit.py`、`summarize.py`：当前状态、历史隔离、指标和哈希清单。

身份、正文 hash、Revision、原件保存和字段证据属于 SEAL 实验业务层，不归功于任一采集框架。

### 编码前确定的失败条件

1. 不同 URL、功能合同、轮次或 oracle，不形成同功能比较。
2. 200 错误页、空正文、字段错位、导航噪声不能仅凭 HTTP 成功过关。
3. 不用历史数据、合成上传或本地浏览器结果补齐真实网站另一组。
4. 保留单边失败和 UNKNOWN，不能删掉失败来源提高成功率。
5. 访问受限就停止该来源双方；不换引擎、代理、UA、验证码方案绕过。
6. 原件字段变化和 DOM 表示变化要区分；同正文 hash 不是所有字段完整的证明。
7. 离线评分不联网；凭据不进入日志、文件、命令参数或工件。
8. 预算在发送前预留，中断不重置；重放不重新抓取。

## 离线复现当前验收

前提：已有隔离 `.venv`，保留整个本轮目录、`oracle.json` 以及对应 Recipe 版本。本轮环境为 Python 3.14.6 / macOS arm64；版本记录及锁文件已有，不需重新安装依赖。

```bash
cd experiments/scraper-benchmark
.venv/bin/python paired_acceptance.py results/paired-live-20261008T095127Z
.venv/bin/python paired_quality.py results/paired-live-20261008T095127Z
.venv/bin/python paired_audit.py
.venv/bin/python summarize.py
node ~/.pi/agent/skills/answer-me-with-html/scripts/am.mjs render \
  results/overview.md -o results/benchmark-overview.html --no-open
```

这些命令**无需 Key、不访问源站、不消耗 credits**。质量脚本 exit 0 仅表示评分完成；必须查看字段和内容评分，不能声称全部业务合同通过。

- 执行合同：`execution_acceptance.json`，本次 **41 PASS / 0 FAIL**。
- 内容对照：`quality.json`，明确保留 iframe 标签丢失、空正文等失败。
- 原始响应、实际 SDK HTTP、请求账本、选项、代码、oracle 都在本轮目录内。
- 预检前后代码及中断账本保留在 `executed_source*`、`interrupted_*`。
- 403 页面回显的供应商出口 IP 已脱敏；`redactions.json` 记录变换和哈希。

验证工件完整性：

```bash
.venv/bin/python - <<'PY'
import hashlib, json
from pathlib import Path
manifest = json.loads(Path('results/artifact_manifest.json').read_text())
for name, expected in manifest.items():
    assert hashlib.sha256(Path(name).read_bytes()).hexdigest() == expected['sha256'], name
print(f'{len(manifest)} artifacts verified')
PY
```

哈希由 `summarize.py` 重新生成。哈希本身不证明网络请求发生；应结合发送账本、SDK 响应及执行日志审阅。

## 联网门禁

本轮现场命令为：

```text
.venv/bin/python paired_live.py --campaign paired-live-20261008T095127Z
# 仅一次、仅在所有内容请求尚未开始的特定政策校验中断后：
.venv/bin/python paired_live.py --campaign paired-live-20261008T095127Z --resume-policy-interruption
```

当前在线门禁已关闭。以上是执行记录，不是让读者现在重跑的指令。原目录不能重用，不能换个 campaign ID 重置本次批准预算；新增联网需要用户重新明确范围与预算，Key 仅通过安全环境传入。

- B 本轮 Firecrawl 返回源站 403，因此没有改用 Scrapy 重试，也未抓取公告。
- A/C/H 的 robots 资格没有解决；不把 UNKNOWN 自动当允许。
- D 的旧 robots 阻断已由本轮 404 结果解除；旧状态仍留在历史数据中。
- F/G 的历史 412 限制不变，本轮未请求。
- 历史总请求量仍 UNKNOWN，旧 `scrapy_test.py raw/all/playwright`、`probe.py`、`recon.py` 继续锁定。
- `firecrawl_test.py` 的旧真实网站分支仍要求严格源站网关；本轮在**新批准的客户端预算口径**下使用独立 `paired_live.py`，没有悄悄放宽旧入口。
- 余额 API 只证明余额。免费且不自动付款以操作者确认为前提；脚本没有购买、充值或订阅操作。

## 历史能力实验：只做背景，不混分

旧原件在 `results/raw/`、`results/parsed/`；修复输出在 `results/replay/`。不可覆盖。

- 本地历史 E2E：修复前 8 PASS / 7 FAIL，修复后 **46 PASS / 0 FAIL**；本轮未重跑。
- 旧 Firecrawl 合成云上传：**6 API / 4 credits / 0 目标网站请求**，见 `results/firecrawl_fixture-20261008-approved1/`。
- 旧 A/B 上传文件名不同，影响相对链接和 hash。已修正未来入口的 filename，但严格同身份云端去噪尚未重测。
- 旧 PDF 的五页、111 行成绩与本地 JS/browser 成绩不能当本轮配对成绩。
- 纠正前报告及配对前状态在 `results/history/`。Scrapy 优先的比较性建议已撤回，没有恢复。

可用的历史离线/loopback命令：

```bash
.venv/bin/python scrapy_test.py replay
.venv/bin/python scrapy_test.py pdf
.venv/bin/python quality_audit.py
.venv/bin/python cloud_acceptance.py results/firecrawl_fixture-20261008-approved1
.venv/bin/python acceptance.py
```

完整旧本地验收还需要 Chrome，以及 Poppler 的 `pdftotext`/`pdftoppm`；只绑定 loopback 并拦截浏览器外网。`acceptance.py --baseline` 应保持历史 7 项失败，不为变绿改原件。

新环境按 `requirements.lock.txt` 在实验 `.venv` 中安装依赖；安装/文档查询是环境或研究流量，独立于上述来源/SDK 请求账本。不要更改 SEAL 生产依赖。
