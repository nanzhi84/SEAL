"""Publish the evaluated coarse grid and fully repeated empirical frontier."""
import csv
import io

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import PercentFormatter

from common import OUT, read, save


def main():
    summary = read(OUT / "summary.json")
    rows = summary["configurations"]
    if not any(r["pareto"] for r in rows) or any(
        r["pareto"] and r["stage"] != "replicated" for r in rows
    ):
        raise RuntimeError("A report requires a nonempty, fully confirmed frontier")
    names = {"chi_sim": "Simplified Chinese", "chi_tra": "Traditional Chinese",
             "eng": "English", "vie": "Vietnamese"}
    models = {"tessdata_fast": ("#2463a5", "o"), "PP-OCRv6_tiny": ("#bd7325", "^"),
              "PP-OCRv6_small": ("#bd7325", "s"), "PP-OCRv6_medium": ("#bd7325", "D")}
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10,
                         "axes.spines.top": False, "axes.spines.right": False})
    fig, axes = plt.subplots(2, 2, figsize=(15, 10), layout="constrained")
    for ax, (language, name) in zip(axes.flat, names.items(), strict=True):
        selected = [r for r in rows if r["language"] == language and r["eligible"]]
        for model, (color, marker) in models.items():
            group = [r for r in selected if r["engine"] == model]
            ax.scatter([r["total_seconds"] for r in group], [r["cer"] for r in group],
                       s=45, marker=marker, edgecolors=color,
                       facecolors=[color if r["stage"] == "replicated" else "none" for r in group],
                       linewidths=1, label=model, zorder=3)
        front = sorted([r for r in selected if r["pareto"]], key=lambda r: r["total_seconds"])
        ax.plot([r["total_seconds"] for r in front], [r["cer"] for r in front], color="#67717b", linewidth=1)
        for row in front:
            short = "fast" if row["engine"] == "tessdata_fast" else row["engine"].rsplit("_", 1)[1]
            ax.annotate(f"{short} {row['dpi']}", (row["total_seconds"], row["cer"]),
                        xytext=(5, 5), textcoords="offset points", fontsize=8)
        ax.set_title(name + " · 4 pages / 12 fixed regions", loc="left")
        ax.set_xscale("log")
        ax.set_xlabel("4-page elapsed time (seconds, log scale; median per page)")
        ax.set_ylabel("Character-weighted fixed-region CER")
        ax.yaxis.set_major_formatter(PercentFormatter(1))
        ax.set_ylim(bottom=0)
        ax.margins(x=0.25, y=0.1)
        ax.grid(axis="y", color="#e5e9ed", linewidth=0.7)
    handles, labels = axes.flat[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="outside lower center", ncol=4, frameon=False)
    fig.suptitle("PP-OCRv6 and tessdata_fast: screened grid and confirmed frontier", fontsize=16)
    fig.supxlabel("CPU, one thread · filled: 3 repeats/page; open: screening · gray line: empirical frontier · model load/warmup excluded", fontsize=10)
    for extension in ["png", "svg", "pdf"]:
        fig.savefig(OUT / ("pareto." + extension), dpi=180)
    plt.close(fig)
    front = [r for r in rows if r["pareto"]]
    save(OUT / "pareto.json", front)
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    (OUT / "summary.csv").write_text(stream.getvalue())
    lines = ["# 多语言 OCR 粗筛与确认前沿", "",
             "PaddleOCR 3.7.0 / PP-OCRv6 tiny、small、medium 与 Tesseract 5.5.3 / tessdata_fast，"
             "Apple M5 Pro 单线程 CPU。每语言4页、12个人工固定区域；CER为总编辑距离/总真值字符数。", "",
             "按用户选择先粗筛：small、medium 的96/144/300 DPI先跑一次，候选及其相邻DPI再测。"
             "候选由联合前沿与25%计时保护带确定。最终前沿每页至少3次，时间为逐页中位数求和；"
             "被明显支配的粗筛点可能只有1次。未测试配置明确标记not_evaluated。", "",
             "这是已评估网格的经验前沿，不保证未测试DPI的最优，也没有统计置信区间。"
             "Paddle常驻模型加载和预热单列；Tesseract每页CLI启动计入。下载和离线评分排除。", "",
             "|语言|确认非支配模型|DPI|编辑距离/字符数|CER|4页秒|每页最少重复|",
             "|---|---|---:|---:|---:|---:|---:|"]
    for language in names:
        for row in sorted([r for r in front if r["language"] == language], key=lambda r: r["total_seconds"]):
            lines.append(f"|{language}|{row['model']}|{row['dpi']}|{row['errors']}/{row['reference_characters']}|{row['cer']:.3%}|{row['total_seconds']:.3f}|{row['minimum_repeats']}|")
    lines += ["", "## 复现", "", "```sh",
              "uv sync --project experiments/paddle-ocr-pareto --frozen",
              "uv run --project experiments/paddle-ocr-pareto python experiments/paddle-ocr-pareto/prepare.py",
              "uv run --project experiments/paddle-ocr-pareto python experiments/paddle-ocr-pareto/screen.py",
              "uv run --project experiments/paddle-ocr-pareto python experiments/paddle-ocr-pareto/screen_report.py", "```", "",
              "复跑验证冻结文件并复用已有回执；新计时须配置独立run。work/中的原件与模型是持久复现输入。"
              "原完整矩阵改为用户选择的粗筛，已完成Tesseract/tiny三次数据保留，未将校准记录混入正式结果。", "",
              "## 证据与限制", "",
              "manifest.json冻结原件、模型和原计时源码；screening-manifest.json追加粗筛源码/策略哈希；"
              "records.json保留每次回执、几何取字、真值、误差、耗时和输出哈希。原文和TSV/JSON均可回看。", ""]
    lines += ["- " + item for item in read(OUT / "manifest.json")["limitations"]]
    (OUT / "report.md").write_text("\n".join(lines) + "\n")
    print(OUT / "report.md")


if __name__ == "__main__":
    main()
