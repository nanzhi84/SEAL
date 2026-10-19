"""Final figure layout; numerical results and frozen timing sources stay immutable."""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import MaxNLocator, PercentFormatter

from common import OUT, read


def main():
    rows = read(OUT / "summary.json")["configurations"]
    languages = {"chi_sim": "Simplified Chinese", "chi_tra": "Traditional Chinese",
                 "eng": "English", "vie": "Vietnamese"}
    models = {"tessdata_fast": ("#2463a5", "o"), "PP-OCRv6_tiny": ("#bd7325", "^"),
              "PP-OCRv6_small": ("#bd7325", "s"), "PP-OCRv6_medium": ("#bd7325", "D")}
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10,
                         "axes.spines.top": False, "axes.spines.right": False})
    fig, axes = plt.subplots(2, 2, figsize=(15, 11))
    fig.subplots_adjust(top=.89, bottom=.18, left=.07, right=.98, hspace=.36, wspace=.2)
    for ax, (language, name) in zip(axes.flat, languages.items(), strict=True):
        selected = [r for r in rows if r["language"] == language and r["eligible"]]
        for model, (color, marker) in models.items():
            group = [r for r in selected if r["engine"] == model]
            ax.scatter([r["total_seconds"] for r in group], [r["cer"] for r in group],
                       s=44, marker=marker, edgecolors=color, linewidths=1,
                       facecolors=[color if r["stage"] == "replicated" else "none" for r in group],
                       label=model, zorder=3)
        front = sorted([r for r in selected if r["pareto"]], key=lambda r: r["total_seconds"])
        ax.plot([r["total_seconds"] for r in front], [r["cer"] for r in front], color="#69747e", linewidth=1)
        for row in front:
            short = "fast" if row["engine"] == "tessdata_fast" else row["engine"].rsplit("_", 1)[1]
            offset, align = (7, 8), "left"
            if language == "chi_tra" and short == "small" and row["dpi"] == 120:
                offset, align = (-8, 25), "right"
            elif language == "chi_tra" and short == "small" and row["dpi"] == 200:
                offset = (10, 10)
            elif language == "eng" and short == "medium" and row["dpi"] == 96:
                offset, align = (-9, 21), "right"
            elif language == "eng" and short == "medium" and row["dpi"] == 144:
                offset, align = (-10, -11), "right"
            ax.annotate(f"{short} {row['dpi']}", (row["total_seconds"], row["cer"]),
                        xytext=offset, textcoords="offset points", ha=align, fontsize=8,
                        arrowprops={"arrowstyle": "-", "color": "#a0a7ae", "lw": .5})
        ymax = max(r["cer"] for r in selected) * 1.12
        ax.set_ylim(-.025 * ymax, ymax)
        ticks = MaxNLocator(nbins=6).tick_values(0, ymax)
        ax.set_yticks([v for v in ticks if 0 <= v <= ymax])
        ax.yaxis.set_major_formatter(PercentFormatter(1))
        ax.set_xscale("log")
        ax.margins(x=.23)
        ax.set_title(name, loc="left", fontsize=13, pad=12)
        ax.set_xlabel("4-page elapsed time (seconds; log scale)", labelpad=8)
        ax.set_ylabel("Fixed-region character error rate (CER)")
        ax.grid(axis="y", color="#e5e9ed", linewidth=.7)
    handles, labels = axes.flat[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", bbox_to_anchor=(.5, .075), ncol=4, frameon=False)
    fig.suptitle("PP-OCRv6 and tessdata_fast: screened grid and confirmed frontier", x=.07, y=.974, ha="left", fontsize=17)
    fig.text(.07, .936, "Apple M5 Pro CPU, one thread · 4 pages / 12 fixed regions per language · lower and left are better", fontsize=11, color="#59636d")
    fig.text(.5, .045, "Filled markers: 3 repeats/page · Open markers: screening · Gray line: empirical nondominated frontier", ha="center", fontsize=10)
    fig.text(.5, .022, "Per-page median elapsed time includes render + OCR + output writes; Paddle load/warmup excluded; Tesseract CLI startup included.", ha="center", fontsize=9, color="#59636d")
    for extension in ["png", "svg", "pdf"]:
        fig.savefig(OUT / ("pareto." + extension), dpi=180)
    plt.close(fig)


if __name__ == "__main__":
    main()
