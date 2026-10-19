---
kind: plan
lang: zh
---

# 整份无文字 PDF OCR

[Issue #25](https://github.com/nanzhi84/SEAL/issues/25) · [可视化](../generated/pdf-ocr.html)

## 目标与边界

PDF 单次获取 → 原件 CAS → 异步 PDF 子进程（预检 → native 或 OCR）→ SDK
PDFPage/PDFProcessResult → 既有 scraper → CrawlResult/Markdown → 原有归档、Version、
Snapshot 与 Results API。不增加业务表、队列或独立解析平台。

保留 `any(page.extract_text().strip() for page in reader.pages)` 整份判断。混合 PDF、
仅页码有文字的扫描件走 native，接受正文可能缺失；不做逐页分流或质量判断。
每页处理完成才成功，允许空文字页；整份无文字、损坏、无法解密、零页明确失败。

## 职责与合同

`pdf_worker` 只读下载的本地原件和冻结配置，无下载、数据库、S3 职责或凭据。
预检和 native 也在子进程，避免同步 scraper 或事件循环长时间提取文字。
native 复用 SDK processor 的逐页提取；OCR 由 PDFium 顺序渲染、Tesseract 顺序识别，
两者共用 SDK 的文字转 HTML，再适配 SDK 页面结果。父进程校验格式、页码、页数、
大小、非空整份结果，交给既有 SDK scraper 包装。worker 在处理前发送有界预检帧，
父进程以其实际页数和获取方法核对最终结果，防止 processor 漏页；该边界信任部署的
worker 和本地引擎，不承诺恶意替换 worker 后仍能验证其自报预检值。

`metadata.pdf` 包含 `method: native|ocr`、`pages: [{page_number, text}]`，页码一基，
空页保留。OCR 另记录引擎版本、语言和实际资源配置。
`original_attachment_index` 指向同一结果 attachments 中的原始 PDF，即使重定向链
保留多个附件也能回查；不公开 CAS 键。原始 `body_hash` 不变。不承诺识别准确性、
表格或正文完整性。

Source 新增 `ocr`，随 Run config 冻结：languages=`eng+chi_sim+vie`、timeout_seconds=180、
max_pages=200（OCR 分支）、dpi=144、max_page_pixels=20000000、max_result_bytes=16777216
（两种分支）。同主机、同 SEAL_WORK_DIR 的采集进程以四个 flock 文件共享名额，
跨 Source/Run/Worker 最多四个 PDF 子进程，OCR 因而不会超过四个；不承诺跨主机
分布式全局上限。每个 Run 管理自身在途任务，子进程不继承槽位句柄。
native 保留原有页数能力，不适用 OCR 页数上限。父进程有界读取 stdout，stderr 丢弃；
worker 的 tesseract stdout 同样有界；每次只保留一页图像并立即释放。

运行环境安装 `tesseract` 和需要的 traineddata，Python 依赖由 `uv sync` 安装 PDFium。
macOS 可用 `brew install tesseract`，Linux 可用发行版的 `tesseract-ocr` 包；按
[官方说明](https://tesseract-ocr.github.io/tessdoc/Installation.html) 安装语言数据。
`SEAL_TESSERACT` 可指定受信任的本地可执行文件，`TESSDATA_PREFIX` 指定语言目录；
这些属于部署配置，不接受来源输入。缺少任意配置语言明确失败，不静默降级。

### 按来源选择 OCR（2026-10-19，速度优先）

当前来源与新来源模板显式配置 `ocr.languages` / `ocr.dpi`，不依靠上述历史多语言默认值：

| 来源正文语言 | languages | dpi | 部署权重 |
| --- | --- | --- | --- |
| 简中 | chi_sim | 96 | tessdata_fast |
| 繁中 | chi_tra | 96 | tessdata_fast |
| 英语 | eng | 96 | tessdata_fast |
| 越南语 | vie | 96 | tessdata_fast |

现有 19 个大陆入口配置简中，越南入口 [dd-492](../reference/尽职调查核验入口地图.md#dd-492)
配置越南语；尚无繁中、英语 Source，不新增占位来源。混排来源的语言组合需另行校准。
来源配置 → Run 冻结配置 → PDF worker 的语言及 DPI → CrawlResult OCR 元数据，沿用现有
链路；`fast` 是运行环境加载的权重系列，不是新的来源字段。模型内容哈希参与解析指纹。
更改配置仅作用于新 Run；已有 Run 不读取后来修改的 YAML，历史 Run 不被重写。

本机已在 Tesseract 默认目录和既有 `work/ocr-tessdata` 安装四种 fast 权重。新环境安装：

```sh
mkdir -p work/ocr-tessdata
for language in chi_sim chi_tra eng vie; do
  curl --fail --location --retry 2 \
    "https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/main/$language.traineddata" \
    --output "work/ocr-tessdata/$language.traineddata"
done
export TESSDATA_PREFIX="$PWD/work/ocr-tessdata"
tesseract --list-langs
```

显式设置 `TESSDATA_PREFIX` 时，启动采集器与队列 worker 的环境必须一致。为复现本次选择，
下载后逐项核对 SHA-256（上游更新时不可静默视作同一模型）：

| 模型 | SHA-256 |
| --- | --- |
| chi_sim | a5fcb6f0db1e1d6d8522f39db4e848f05984669172e584e8d76b6b3141e1f730 |
| chi_tra | 529c5b5797d64b126065cd55f2bb4c7fd7b15790798091b1ff259941a829330b |
| eng | 7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2 |
| vie | 79df64caf7bcfb2a27df5042ecb6121e196eada34da774956995747636d5bfa1 |

本次按速度优先选择 [Paddle 与 Tesseract 对比实验](paddle-ocr-pareto.md) 中四语各自最快的
已复测前沿点，均为 `tessdata_fast` / 96 DPI。四页逐页三次计时中位数之和分别为简中
3.02 秒、繁中 4.06 秒、英语 2.10 秒、越南语 2.76 秒；固定区域 CER 分别为
2.451%、8.734%、0.536%、0.626%。简中 tiny / 120 DPI 为 4.61 秒、0% CER，
准确率更高但更慢，因此本次不选择。结果仅适用于本机有限扫描样本，不代表全文或语言总体；
混排另测。旧的 2026-10-16 验收记录如下，不能作为本次 96 DPI 的验收证明。

2026-10-19 速度配置独立验收：`uv run --no-sync python tests/speed_ocr_acceptance.py`，
76/76 PASS。20 个来源及模板核对、四语真实扫描页经 Source → 隔离 worker → InputScraper →
最终 CrawlResult，核验 96 DPI、fast 模型 SHA、正文锚点、页码与 HTML/Markdown。
前提：项目 uv 依赖、Tesseract 与四语固定 fast 权重、实验保留且 SHA 匹配的公开原件；
原件清单及下载 URL 位于 `experiments/paddle-ocr-pareto/config/documents.json`。
工件为 `artifacts/speed-ocr-20261019/report.json`、`inputs.json`、四语 `*-crawlresult.json`。
本次越南原件远端重下载 SHA 与固定输入不符，使用实验持久原件通过验收；
`recovery-limitations.json` 保留该恢复限制，不能声称当前 URL 可重新下载相同字节。
未验证 DB/S3、Results API、全站采集或全文准确率；旧 Run 未访问数据库验收。

2026-10-19 工作区审查另行完成 105/105 断言（上述四语路径 76 项，加隔离存储与
README 合同 29 项）。复跑入口 `uv run --no-sync python tests/review_ocr_readme_acceptance.py`，
需 Docker、本机 fast 权重和上述持久原件；脚本启动并清理独立 PostgreSQL/MinIO，
使用本地公开 fixture 与扫描切页，覆盖 collect → DB/S3 → Snapshot → 鉴权分页 API →
原件哈希，并验证精确回执 Run、失败本轮拒绝旧成功及 unchanged 合法复用、历史冻结
配置不变。报告 `artifacts/review-20261019-config/review-summary.json` 与
`readme-contract/report.json`；不证明当前官方 URL 可下载、全量采集或生产可用。

历史 2026-10-16 配置验收（固定旧 DPI，不作为当前入口）：
`uv run --no-sync python tests/source_ocr_config_acceptance.py`，
61 项断言通过；依赖本机四个 fast 模型和保留于 `artifacts/` 的真实扫描 PDF 输入。
报告 `artifacts/source-ocr-config-20261016/report.json`、同目录 Source 快照与各语言最终
CrawlResult 支持复核输入哈希、正文、页码、HTML、Markdown 及 OCR 配置。入口为 Source
加载 → 隔离 PDF worker → InputScraper → CrawlResult，未经过 DB、S3、网络采集或全站采集。

升级前缺少 ocr 的待执行/重试 Run 使用历史默认值（`eng+chi_sim+vie`、144 DPI），不改写原始 config_hash，
实际执行配置仍记录于结果元数据；不读取后来变更的 Source 配置。

## 生命周期与失败

执行期限为子进程启动前的 monotonic + OCR timeout 与采集剩余预算较早值，包含
启动、预检、模型、渲染、识别、校验；等待名额消耗采集预算。page_timeout 只限制
网络获取，SDK 0.9.5 在 adapter 返回后调用同步 scrap，没有包围解析的 page_timeout。
父进程在 adapter 的异步阶段准备结果，scrap 只包装已验证页面。
本地解释器用 Popen 启动，在首次挂起前获得 PID 所有权，避免已启动但 spawn future
未返回的取消竞态；冻结配置以只读 stdin 文件传入，stdout 通过异步管道读取，进程
以异步 poll 等待。同步部分只有本地解释器启动，不等待模型加载或 PDF/OCR 处理。

超时、取消、预算到期、退出共用清理：停止接收 → TERM 进程组 → 有界等待 → KILL
进程组 → 回收直接子进程并确认组内无运行进程 → 删除目录 → 释放名额。清理不受
二次取消中断；清理失败保留目录和名额、封闭管理器并报告稳定错误，并写入共享
`pdf-processes/blocked` 标志，所有 Run 停止新任务。恢复前必须停止所有采集进程、
确认残留进程已终止并检查故障目录，再移除 blocked 标志；运行中不得删除槽位文件。
采集信号同时取消在途 PDF；提交前再检查取消和预算，拒绝迟到结果。外层采集进程
停止宽限须覆盖 PDF 清理期限。父进程 SIGKILL、断电不承诺即时清理；部署应以容器/
服务 cgroup 整体终止，不能只杀采集 PID。

稳定原因：`pdf_invalid_or_encrypted`、`pdf_zero_pages`、`pdf_ocr_page_limit_exceeded`、
`pdf_ocr_pixel_limit_exceeded`、`pdf_ocr_timeout`、`crawl_time_budget_exceeded`、
`pdf_ocr_failed`、`pdf_output_invalid`、`pdf_output_too_large`、`pdf_no_text`、
`pdf_cleanup_failed`。取消沿用 `interrupted`，默认不重试 OCR。

## 验收与交付

先枚举失败边界，由 fresh 子代理在 tests/ 编写隔离业务验收。真实 collect → SQL/S3 →
Snapshot → HTTP Results/attachments 验证 native、扫描、多语言、空页、原件哈希、
单次下载与已知混合边界。受控引擎覆盖并发、响应性、取消/竞态/超时/预算、进程组、
坏输出和清理失败。运行数据 work/，可重复命令和脱敏证据 artifacts/，均不入库。
静态检查使用项目 Ruff 与 seiso；不以它们替代业务验收。通过独立审查后合并 PR。

无数据库迁移。native 合同更新为 `crawl4ai_0_9_5_native_pages_v1`，避免重查时继续
复用不含页面来源元数据的旧 Version；OCR 为 `crawl4ai_0_9_5_ocr_v1`。显式重采集才
追加不可变 Version，不自动重解析；历史 Snapshot 不变。回滚代码与依赖锁即可停止未来 OCR。
生产部署、负载、真实来源全量 OCR 及不可捕获退出由最终验收单独列出边界。

## 历史 OCR 功能独立验收（2026-10-15）

2026-10-15，macOS / Python 3.12.13 / Crawl4AI 0.9.5 / pypdfium2 5.14.0 /
Tesseract 5.5.3，真实 eng、chi_sim、vie 模型。四份最终报告的实现与依赖摘要一致，
共 **365 PASS / 0 FAIL**；历史失败或源码漂移轮不计入本次结论。

| 路径 | 断言 | 脱敏报告 |
| --- | ---: | --- |
| 真实多语言采集、页面来源、重定向多附件、Snapshot/API/原件回查 | 181 PASS | `artifacts/issue25-real-20261015-g/report.json` |
| 真实旧代码采集升级、旧待执行 Run、旧 Snapshot 不变 | 17 PASS | `artifacts/issue25-upgrade-20261015-e/report.json` |
| 坏输出、超时/预算、取消竞态、事件循环、强退派生进程、清理故障 | 150 PASS | `artifacts/issue25-faults-20261015-e/report.json` |
| 两个 Source 独立 collect 共享上限、跨 Run 清理失败封闭、原生坏页 | 17 PASS | `artifacts/issue25-global-20261015-c/report.json` |

在根目录重跑下列命令，每次用新的运行名。前提：`uv sync`、实际 Tesseract 和
`work/ocr-tessdata/{eng,chi_sim,vie}.traineddata`，本机 PostgreSQL/MinIO 正常且私有
`.env.local` 提供创建隔离数据库/桶的权限。使用合成公开 fixture，允许本地回环；
报告记录引擎/模型/源码哈希、命令与环境前提，不保存凭据。故障引擎只证明边界，
不替代第一项实际 OCR。测试脚本位于 `tests/`，运行数据与生成工件不入库。

```bash
uv run --no-sync python tests/issue25_e2e.py --name issue25-real-rerun
uv run --no-sync python tests/issue25_upgrade.py --name issue25-upgrade-rerun
uv run --no-sync python tests/issue25_faults.py --name issue25-faults-rerun
uv run --no-sync python tests/issue25_global.py --name issue25-global-rerun
```

独立子代理审查最终实现无剩余必修项；Ruff、格式、seiso、diff 与 GitHub 静态 CI 通过。
生产、公网来源全量采集、负载、跨主机分布式上限、SIGKILL/断电即时回收未验证。
