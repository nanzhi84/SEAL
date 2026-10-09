"""Freeze a reviewed experiment inventory and parsing baselines, without network."""

import argparse
import csv
import hashlib
import json
from collections import Counter
from pathlib import Path

from classify import extract
from content import parse_content, response_for
from inventory import ROOT, inventory, write_json

HERE = Path(__file__).resolve().parent
DOCS = HERE / "reference-snapshot"

LABELS = {
    "ACCESSIBLE_HTML": "公开HTML可提取",
    "ACCESSIBLE_PDF": "文本PDF可提取",
    "ACCESSIBLE_JSON": "JSON可提取",
    "ROBOTS_UNKNOWN": "robots无法确认，停止采集",
    "ROBOTS_DENIED": "robots明确禁止",
    "NETWORK_ERROR": "入口网络失败",
    "HTTP_ERROR": "入口HTTP错误",
    "SKIP_INPUT": "需要登录或输入，跳过",
    "JS_SHELL": "只有JS或iframe框架",
    "EMPTY": "未取得足够正文",
    "ACCESS_RESTRICTED": "访问验证或WAF限制",
}


def failure_detail(row):
    if row["outcome"] == "ROBOTS_UNKNOWN":
        policy = row.get("robots", {})
        observation = policy.get("robots_observation", {})
        error = policy.get("error", {})
        if error:
            causes = " → ".join(c["type"] for c in error.get("causes", []))
            return "robots请求失败：" + (causes or error.get("error", "未知"))
        return "robots最终HTTP " + str(observation.get("status", "未知")) + "，未获得可用规则"
    if row["outcome"] == "SKIP_INPUT":
        return (
            "登录需要身份信息"
            if row["reason"] == "LOGIN_REQUIRED"
            else "需提供查询条件，本轮未输入"
        )
    return {
        "PUBLIC_ENTRY_TEXT_EXTRACTED": "入口文本和链接已提取",
        "PDF_TEXT_LAYER_EXTRACTED": "全部页面文本层可提取",
        "JS_OR_IFRAME_REQUIRED": "静态响应没有足够正文，需要JS渲染或公开数据接口",
        "INSUFFICIENT_VISIBLE_CONTENT": "响应正文不足40字符，未确认为有效内容",
        "WAF_SCRIPT_CHALLENGE": "HTTP 200返回WAF脚本挑战，停止采集",
    }.get(row["reason"], row["reason"])


def merge(campaigns, reviewed):
    merged = {}
    rejected_robots = {
        r["sha256"] for r in json.loads((HERE / "review/robots-rejections.json").read_text())
    }
    for campaign in campaigns:
        for row in json.loads((campaign / "results.json").read_text()):
            old = merged.get(row["id"])
            if (
                old
                and old["observed_outcome"] != "INTERRUPTED"
                and old.get("observed_reason") != "OSError"
            ):
                raise ValueError("Only interrupted rows may be replaced: " + row["id"])
            row["campaign"] = str(campaign.relative_to(ROOT))
            row["observed_outcome"] = row["outcome"]
            row["observed_reason"] = row["reason"]
            if row.get("raw_path"):
                row["raw_file"] = str((campaign / row["raw_path"]).relative_to(ROOT))
                row.update(extract(response_for(row)))
            robot_hash = row.get("robots", {}).get("robots_observation", {}).get("body_sha256")
            if robot_hash in rejected_robots:
                row.update(
                    outcome="ROBOTS_UNKNOWN",
                    reason="ROBOTS_HTML_MISCLASSIFIED_DURING_LIVE_RUN",
                    protocol_deviation=True,
                )
            if row["id"] in reviewed.get("outcome_overrides", {}):
                row.update(reviewed["outcome_overrides"][row["id"]])
            row["note"] = reviewed["notes"].get(row["id"], "")
            row["detail"] = failure_detail(row)
            if row.get("protocol_deviation"):
                row["detail"] = (
                    "robots返回HTML，被首版误认规则后曾请求内容；离线纠正为规则未知，排除golden"
                )
            row["query_submitted"] = False
            row["verification_scope"] = "未取得核验业务数据"
            if row["outcome"].startswith("ACCESSIBLE"):
                row["verification_scope"] = "仅验证入口解析；业务正文、完整性和时效性未验收"
                if row.get("inputs"):
                    row["verification_scope"] += "；有输入框，主体查询跳过"
            if row["id"] in reviewed["content"]:
                row["verification_scope"] = reviewed["content"][row["id"]]["scope"]
            merged[row["id"]] = row
    rows = sorted(merged.values(), key=lambda r: r["id"])
    if [r["id"] for r in rows] != [r["id"] for r in inventory()]:
        raise ValueError("map_coverage_mismatch")
    if any(r["outcome"] == "INTERRUPTED" for r in rows):
        raise ValueError("interrupted_rows_remaining")
    return rows


def freeze(root, rows, reviewed):
    root.mkdir(parents=True, exist_ok=False)
    fixtures = []
    for row in rows:
        if not row.get("raw_file") or row["map_noise"]:
            continue
        if not row["outcome"].startswith("ACCESSIBLE"):
            continue
        fixture = {
            k: row[k]
            for k in (
                "id",
                "name",
                "url",
                "final_url",
                "fetched_at",
                "mime",
                "encoding",
                "raw_file",
                "body_sha256",
                "verification_scope",
            )
        }
        fixture["tier"] = "content" if row["id"] in reviewed["content"] else "entry"
        fixture["expected_entry"] = {
            k: row[k] for k in ("outcome", "title", "text_chars", "link_count", "pages") if k in row
        }
        fixture["expected_links"] = row.get("links", [])
        if fixture["tier"] == "content":
            fixture["spec"] = reviewed["content"][row["id"]]
            output = parse_content(response_for(row), fixture["spec"])
            write_json(root / "extracted" / (row["id"] + ".json"), output)
        fixtures.append(fixture)
    write_json(root / "fixtures.json", fixtures)
    write_json(
        root / "negative-cases.json",
        [
            {**next(r for r in rows if r["id"] == key), "expected_outcome": expected}
            for key, expected in reviewed["negative_cases"].items()
        ],
    )
    write_json(root / "entries.json", rows)
    return fixtures


def write_report(rows, fixtures, campaigns):
    counts = Counter(r["outcome"] for r in rows)
    category = Counter(r["category"] for r in rows)
    by_id = {f["id"]: f for f in fixtures}
    unique_raw = len({f["body_sha256"] for f in fixtures})
    request_count = sum(len((p / "requests.jsonl").read_text().splitlines()) for p in campaigns)
    safe_count = sum(
        r.get("archive_status") == "SENSITIVE_PATTERN_NOT_ARCHIVED"
        and r["outcome"].startswith("ACCESSIBLE")
        for r in rows
    )
    data_dir = DOCS / "due-diligence"
    data_dir.mkdir(parents=True, exist_ok=True)
    csv_fields = [
        "id",
        "line",
        "category",
        "section",
        "name",
        "url",
        "final_url",
        "outcome",
        "detail",
        "verification_scope",
        "note",
        "golden_tier",
        "raw_file",
        "body_sha256",
        "campaign",
    ]
    with (data_dir / "entries.csv").open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=csv_fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows({**r, "golden_tier": by_id.get(r["id"], {}).get("tier", "")} for r in rows)
    summary = {
        "map_rows": len(rows),
        "outcomes": counts,
        "golden_rows": len(fixtures),
        "golden_raw_bodies": unique_raw,
        "golden_content": sum(f["tier"] == "content" for f in fixtures),
        "requests": request_count,
        "categories": category,
    }
    write_json(data_dir / "summary.json", summary)
    lines = [
        "---",
        "kind: reference",
        "lang: zh",
        "---",
        "",
        "# 尽职调查入口地图逐项抓取实验（2026-10-09）",
        "",
        f"本轮覆盖地图 **491 条记录、481 个不同原始 URL、380 个主机名**。经离线纠正 JS 提示页误判，**{counts['ACCESSIBLE_HTML']} 条 HTML 与 {counts['ACCESSIBLE_PDF']} 条文本 PDF 可提取**；其中 **7 条有独立核对的内容字段**，其余只确认入口解析。未指定企业或个人信息，因此没有提交主体查询。",
        "",
        f"已保留 **{len(fixtures)} 条 golden 记录，引用 {unique_raw} 份不同原文**：7 条内容样本，其余为入口解析基线。另有 {safe_count} 条可解析 HTML 命中敏感字段模式，保守拒绝归档，不进入 golden。这不表示那些页面确实泄露了凭据。",
        "",
        "## 工件与复验",
        "",
        "- [491 条 CSV 明细](due-diligence/entries.csv)：Excel 可打开，含地图行号、URL、结果、原因、范围与原文路径。",
        "- [机器可读汇总](due-diligence/summary.json)、[完整结果](../golden/entries.json)、[golden 清单](../golden/fixtures.json)。",
        "- [独立核对合同](../reviewed.json)、[离线验收结果](../golden/verification.json)。",
        "- [首次实验](../results/map-20261009/manifest.json)、[中断补测](../results/map-20261009-resume/manifest.json)和[本地异常单项补测](../results/map-20261009-local-recovery/manifest.json)分别保留，不用补测掩盖首次失败。",
        "",
        "```bash",
        "uv sync --frozen",
        "# 零网络复验：不需要数据库、账号或主体参数。不要重新生成预期来通过检查。",
        "uv run --frozen python experiments/due-diligence/verify.py --output artifacts/due-diligence-verification.json",
        "# 校验补测请求账本和原文完整性",
        "uv run --frozen python experiments/due-diligence/validate.py experiments/due-diligence/results/map-20261009-resume",
        "# 新一轮在线实验，输出目录必须不存在；实时网站结果可能变化",
        "uv run --frozen python experiments/due-diligence/run.py --output artifacts/due-diligence-new",
        "seiso check",
        "```",
        "",
        "离线复验使用固定原文、已冻结的入口预期，以及核对后固定的内容字段断言。失败非零退出；同时保留 expected/actual。验收过程禁用 Python socket 连接，不下载缺失原文。详细环境为 macOS 27.0.1 arm64、Python 3.12.13、Scrapy 2.19.0、pypdf 6.19.0，完整依赖见 uv.lock 和各轮 manifest。",
        "",
        "## 结果口径",
        "",
        "| 结果 | 地图记录数 |",
        "| --- | ---: |",
    ]
    lines += [f"| {LABELS.get(k, k)} | {v} |" for k, v in counts.most_common()]
    lines += [
        "",
        "每条记录独立保留结论，相同 URL 共用请求。最后 3 条是原材料页脚备案链接，已标为地图噪声，不进入 golden。可解析数包含公开首页、导航或查询说明页，不能读作已获得对应企业核验结果。JS 框架、登录页、错误页不算成功。",
        "",
        "robots 无法确认包括 HTTP 错误、返回 HTML 页面、域名解析/连接失败等，通常停止内容请求。工件复查发现4条记录的robots HTML被初版误判，实际曾继续请求，现纠正为规则未知并全部排除golden。robots禁止表示规则拒绝。本轮未更换身份、代理或处理验证码来绕过限制。网络错误只描述这台机器本轮观察，不证明网站永久不可用；初轮部分错误只有异常类，底层TLS/连接原因未确定。",
        "",
        "## 已核对的内容样本",
        "",
        "| ID | 来源 | 实际验证范围 |",
        "| --- | --- | --- |",
    ]
    lines += [
        f"| {f['id']} | {f['name']} | {f['verification_scope']} |"
        for f in fixtures
        if f["tier"] == "content"
    ]
    lines += [
        "",
        "PDF 为历史《环境保护综合名录（2017年版）》，并非现行版本确认；66 个物理页对应印刷页码 3—68，首尾页经过渲染检查，全部页验证非空文本。未把线性文本当成已还原的表格。珍稀动物样本来自食品伙伴网汇编，属于二级来源，不能单独作为法律效力依据。统计局两个样本只核对通知正文和附件链接，DOC/DOCX 附件未解析。",
        "",
        "核对由本轮代理读取原文并固定预期完成，**不是用户/生产审核人的批准**。入口层预期是自动冻结的回归基线，不是独立业务真值。7 个内容样本仍未验证全站、全部历史分页、未来可达性或主体匹配准确率。",
        "",
        "## 实验约束与诊断",
        "",
        f"三次执行共发出 **{request_count} 个 GET 请求**，包含 robots 和跳转，低于本轮 2400 请求总预算；全局并发 8、每域并发 1、间隔 1.5 秒、超时 20 秒、响应上限 5 MiB、最多 4 次跳转，无自动重试。TLS 验证开启，Cookie、缓存和环境代理关闭。原文保留 Scrapy 解压后的应用层字节及 SHA-256，不保存响应敏感头；清理地图中的旧会话标识和追踪参数，原始地图文件不改。",
        "",
        "首次运行有334次OSError，402条被标为中断。现场文件描述符软上限为256，跨大量域名复用连接造成耗尽是诊断推断；首轮未记录errno，不能声称已确证。补测显式关闭连接复用，并增加异常因果类记录，402条均完成且没有Spider异常。首轮校验保留2个FAIL（中断和异常），402条补测的工件校验为449/449 PASS。另外dd-069首轮报本地OSError，单独补测一次后确认WAF脚本挑战，停止；首轮其余88条终态未重跑。",
        "",
        "离线复查另纠正7个noscript提示页，保留修复前失败断言，并用相同原文复验。WAF误判也保留修复前后断言。纠正解析没有重复请求源站。1份携带会话标识的robots脚本和1份WAF挑战原文已移除，仅保留摘要与[移除记录](../review/redactions.json)，未进入golden；不将脱敏文本冒称原文。基金业协会三个名录页确认了HTML及其GET数据接口引用，但API未验证，不计作名单抓取成功。",
        "",
        "实现由地图注册表、受控采集、分类、内容提取、冻结工件、离线验收六部分组成。491 行使用共享的 public-entry-probe.v1 入口 Recipe 与逐行 URL 配置；7 行另有可执行内容提取参数。**这不是 491 个已完成生产接入的业务 Recipe**，没有写入 SEAL 生产数据库、审核或启用 Source。",
        "",
        "## 逐项结论",
        "",
        "| ID / 地图行 | 分类 / 名称 | 采集结果 | 原因与范围 | Golden |",
        "| --- | --- | --- | --- | --- |",
    ]

    def cell(value):
        return str(value).replace("|", "／").replace("\n", " ")

    for row in rows:
        detail = "；".join(filter(None, [row["detail"], row["verification_scope"], row["note"]]))
        if row["map_noise"]:
            detail = "页脚备案噪声；" + detail
        lines.append(
            f"| {row['id']} / {row['line']} | {cell(row['category'])} / {cell(row['name'])} | {LABELS.get(row['outcome'], row['outcome'])} | {cell(detail)} | {by_id.get(row['id'], {}).get('tier', '—')} |"
        )
    (DOCS / "due-diligence-source-experiment.md").write_text("\n".join(lines) + "\n")
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--campaigns", nargs="+", type=Path, required=True)
    args = parser.parse_args()
    campaigns = [p.resolve() for p in args.campaigns]
    reviewed = json.loads((HERE / "reviewed.json").read_text())
    rows = merge(campaigns, reviewed)
    fixtures = freeze(HERE / "golden", rows, reviewed)
    summary = write_report(rows, fixtures, campaigns)
    write_json(
        HERE / "golden/build.json",
        {
            "summary": summary,
            "code_sha256": {
                p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in HERE.glob("*.py")
            },
            "reviewed_sha256": hashlib.sha256((HERE / "reviewed.json").read_bytes()).hexdigest(),
        },
    )
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
