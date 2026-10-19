"""Regenerate the research figure and exact Pareto table from frozen scored data."""
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
    colors = {"tessdata_fast": "#2463a5", "PP-OCRv6_tiny": "#bd7325",
              "PP-OCRv6_small": "#bd7325", "PP-OCRv6_medium": "#bd7325"}
    markers = {"tessdata_fast": "o", "PP-OCRv6_tiny": "^",
               "PP-OCRv6_small": "s", "PP-OCRv6_medium": "D"}
    labels = {"chi_sim": "Simplified Chinese", "chi_tra": "Traditional Chinese",
              "eng": "English", "vie": "Vietnamese"}
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10,
                         "axes.spines.top": False, "axes.spines.right": False,
                         "axes.edgecolor": "#7b838b", "text.color": "#293440"})
    fig, axes = plt.subplots(2, 2, figsize=(14, 10), layout="constrained")
    for ax, (language, label) in zip(axes.flat, labels.items(), strict=True):
        selected = [r for r in rows if r["language"] == language and r["eligible"]]
        for engine in colors:
            group = [r for r in selected if r["engine"] == engine]
            ax.scatter([r["total_seconds"] for r in group], [r["cer"] for r in group],
                       label=engine, marker=markers[engine], s=44, linewidths=1.0,
                       facecolors="none", edgecolors=colors[engine], zorder=3)
        pareto = sorted([r for r in selected if r["pareto"]], key=lambda r: r["total_seconds"])
        ax.plot([r["total_seconds"] for r in pareto], [r["cer"] for r in pareto],
                color="#66717c", linewidth=1, zorder=1)
        for r in pareto:
            ax.scatter(r["total_seconds"], r["cer"], marker=markers[r["engine"]],
                       color=colors[r["engine"]], s=48, zorder=4)
            name = "fast" if r["engine"] == "tessdata_fast" else r["engine"].rsplit("_", 1)[1]
            ax.annotate(f"{name} {r['dpi']}", (r["total_seconds"], r["cer"]),
                        xytext=(5, 6), textcoords="offset points", fontsize=8)
        ax.set_title(label + " · 4 pages / 12 fixed regions", loc="left", fontsize=12)
        ax.set_xlabel("Sum of per-page median elapsed time (seconds; log scale)")
        ax.set_ylabel("Character-weighted fixed-region CER")
        ax.set_xscale("log")
        ax.yaxis.set_major_formatter(PercentFormatter(1))
        ax.set_ylim(bottom=0)
        ax.grid(axis="y", color="#e5e9ed", linewidth=0.7)
        ax.margins(x=0.2, y=0.15)
    handles, names = axes.flat[0].get_legend_handles_labels()
    fig.legend(handles, names, loc="outside lower center", ncol=4, frameon=False)
    fig.suptitle("PaddleOCR PP-OCRv6 vs Tesseract tessdata_fast", fontsize=17, x=0.01, ha="left")
    fig.supxlabel("CPU, one thread · 3 repeats/page · filled markers: nondominated · Paddle load/warmup excluded; Tesseract per-page CLI included", fontsize=10)
    for extension in ["png", "svg", "pdf"]:
        fig.savefig(OUT / ("pareto." + extension), dpi=180)
    plt.close(fig)
    frontier_rows = [r for r in rows if r["pareto"]]
    save(OUT / "pareto.json", frontier_rows)
    text = io.StringIO()
    writer = csv.DictWriter(text, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    (OUT / "summary.csv").write_text(text.getvalue())
    lines = ["# PaddleOCR 与 tessdata_fast 多语言实测", "",
             "主指标：同机单线程CPU，每页3次完整处理耗时的中位数，按语言4页求和；"
             "CER为12个固定几何区域的总编辑距离/总真值字符数。", "",
             "Paddle常驻模型，加载和预热单列；Tesseract为逐页CLI。下载与离线评分不计时。"
             "填充点为本矩阵非支配配置，结果不代表全文或语言总体准确率。", "",
             "|语言|非支配模型|DPI|编辑距离/字符分母|CER|4页秒|",
             "|---|---|---:|---:|---:|---:|"]
    for language in labels:
        for r in sorted([r for r in frontier_rows if r["language"] == language], key=lambda r: r["total_seconds"]):
            lines.append(f"|{language}|{r['model']}|{r['dpi']}|{r['errors']}/{r['reference_characters']}|{r['cer']:.3%}|{r['total_seconds']:.3f}|")
    lines += ["", "## 复现", "", "```sh",
              "uv sync --project experiments/paddle-ocr-pareto --frozen",
              "uv run --project experiments/paddle-ocr-pareto python experiments/paddle-ocr-pareto/prepare.py",
              "uv run --project experiments/paddle-ocr-pareto python experiments/paddle-ocr-pareto/benchmark.py",
              "uv run --project experiments/paddle-ocr-pareto python experiments/paddle-ocr-pareto/benchmark.py --score-only",
              "uv run --project experiments/paddle-ocr-pareto python experiments/paddle-ocr-pareto/report.py", "```", "",
              "保留输入和模型位于实验 work/，不可作为缓存清理。重跑自动复用已完成的同一冻结记录；"
              "重新计时须在 config/benchmark.json 更换 run 并准备输入，不能混入旧计时。", "",
              "## 范围", ""]
    lines += ["- " + limitation for limitation in read(OUT / "manifest.json")["limitations"]]
    (OUT / "report.md").write_text("\n".join(lines) + "\n")
    print(OUT / "report.md")


if __name__ == "__main__":
    main()
