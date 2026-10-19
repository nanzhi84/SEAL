---
kind: plan
lang: zh
---

# PaddleOCR 与 tessdata_fast 多语言选型

## 目标与边界

在本机 Apple Silicon CPU 比较最新稳定 PaddleOCR 3.7.0 / PP-OCRv6
tiny、small、medium 与 Tesseract 5.5.3 / tessdata_fast，分别计算简中、繁中、
英语、越南语的误差—耗时非支配配置。不修改正式采集器、来源配置或数据库。
[执行图](../generated/paddle-ocr-pareto.html)。

## 数据与模块

独立实验位于 `experiments/paddle-ocr-pareto/`；`config/` 保存可编辑矩阵、固定位置
人工真值与公开输入来源；`work/20261019/` 持久保存 PDF、模型和运行输入，
`artifacts/20261019/` 保存冻结配置、模型与依赖哈希、逐页原始输出、计时、评分与图。
实验环境有独立 pyproject/uv.lock，不改变根依赖。

沿用三语言既有人工转写的12页36个矩形区域；越南语另取同一公开文书的
1、2、13、25页，每页三个完整段落区域，由主 agent 在运行新模型前目视核对。
既有 OCR/VLM 输出只用于定位候选文本，人工回看原图确认后冻结真值。
输入复制到实验，保留公开 URL、原件 SHA-256、页号、矩形与来源记录。

## 比较合同

- 每语言4页12个固定区域，DPI 候选为96、120、144、200、240、300。
  用户选择先粗筛：small、medium先测96/144/300，候选前沿及其相邻DPI再复测。
- 每个页面均整页识别，再按固定区域提取，不能到全文中寻找最像真值的片段。
- 中文 NFC 后去空白，英语与越南语 NFC 后合并空白；保留标点、大小写、声调、繁简。
- CER 为全部区域 Levenshtein 编辑距离之和除以人工真值字符总数，不是全文准确率。
- 单线程 CPU，串行处理；最终前沿每配置每页至少重复3次，粗筛点允许1次。
  速度为每页总时长中位数再按语言求和，
  包括 PDF 重开、渲染、PNG 编码、OCR 和结构输出落盘，排除离线评分、下载。
- Paddle 常驻模型初始化与首次预热单列；Tesseract 保留逐页 CLI 启动开销。
  主前沿反映这两种实际调用形态；不能把热推理指标称为冷启动时延。
- 同语言内同时最小化 CER 和总时长；只有至少一项严格更优才构成支配。
  页或重复失败、输出缺失、哈希漂移、区域不完整的配置不得入前沿。
  联合所有已测完整点计算经验非支配关系；候选取前沿与25%耗时保护带，补测相邻DPI，
  迭代复测直到前沿均达到3次。未测点单列，不当作失败；不保证未测网格最优。
- 保留每次计时原始值，微小计时差异不作为稳定性结论；有限语料不代表语言总体最优。

## 执行与验收

先冻结失败方式与评分合同，再实现；下载并记录官方版本、六份检测/识别模型哈希。
验证真实单页后运行矩阵，再离线评分并生成科学散点图和表格。
完全 fresh 的子 agent 在根 `tests/` 独立核验业务合同、原件与输出哈希、
CER 分母与编辑距离、三次计时中位数、完整性及 Pareto。

运行：`uv sync --project experiments/paddle-ocr-pareto --frozen`，随后
先执行 `uv run --project experiments/paddle-ocr-pareto python experiments/paddle-ocr-pareto/prepare.py`，
再执行 `uv run --project experiments/paddle-ocr-pareto python experiments/paddle-ocr-pareto/run.py`。
入口自动建立新运行manifest、执行粗筛并生成报告；保留回执时可断点复用，
仅重建图表用 `run.py --report-only`。入口源码哈希另存 `entry-receipt.json`。
续跑在执行前核对当前运行时、Tesseract 二进制及全部冻结输入、模型、源码；
缺模型先失败，不在计时中临时下载。CPU 线程固定为 1，Paddle 检测尺寸、模式、
识别阈值和词框开关读取 `config/benchmark.json`。失败、部分完成与未测配置分别标记
`failed`、`incomplete`、`not_evaluated`；离线发布也要求非空前沿全部完成三次复测。
输入准备、完整重跑与离线评分命令随最终实验入口交付。
环境要求 macOS arm64、Python 3.12、Tesseract 与四种 fast 权重；PDF 及模型先下载，
矩阵运行不依赖数据库或凭据。独立验收工件放根 `artifacts/paddle-ocr-independent-20261019/`。

原完整矩阵在Tesseract/tiny已完成、small进行中时，按用户选择切换粗筛；已完成正式数据
保留。`manifest.json` 冻结原计时代码，`screening-manifest.json` 追加新编排与策略哈希。
两次线程校准另存实验work，不进入正式比较。macOS OpenCV/GCD需用
`setNumThreads(0)`关闭并行并确认有效线程1；正数1在该后端仍返回18。

2026-10-19 审查修复后，当前源码与上述历史计时源码不同。旧源码在修复前逐项匹配
原三个 manifest 的哈希，并保存到实验 `work/20261019/frozen-source/`，仅作历史审计；
旧 manifest 与计时不回写。保留该输入时，历史数值/哈希复核命令为：

```sh
uv run --no-sync python tests/paddle_ocr_independent_acceptance.py \
  --experiment experiments/paddle-ocr-pareto \
  --source-snapshot experiments/paddle-ocr-pareto/work/20261019/frozen-source
uv run --no-sync python tests/review_20261019_experiment_acceptance.py
```

第二条在根 `work/review-20261019-experiment/` 隔离复制当前实验，验证四语各一页、
三次真实 Tesseract 运行与恢复/失败边界，工件在同名根 `artifacts/`；不复跑完整 Paddle
矩阵，另有简中 tiny 单页真实 CLI 验证。共 11 项边界通过，历史快照核验 14454 项通过，
报告 `artifacts/review-20261019-experiment/final-review.json`。
不把历史结果作为修复后源码的重新计时。当前源码重计时须在配置更换 `run` 后
重新 `prepare.py`，原件路径仍由 `documents.json` 指定；公开 URL 字节变化会明确失败，
恢复同一次历史输入需要持久原件及其哈希，不能保证仅凭当前远端可恢复。
