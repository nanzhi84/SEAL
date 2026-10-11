"""Observable failure and fidelity cases for the document parser."""

from pathlib import Path


def exercise_edges(h, version):
    names = (
        "ragged.csv",
        "many.csv",
        "nul.txt",
        "header.docx",
        "interleaved.docx",
        "formula.xlsx",
        "overflow.json",
        "nul.json",
        "precision.json",
    )
    h.config(
        "ir_edges",
        entries=[h.site.url + "/ir/" + name for name in names],
        output_schema="record.v1",
        document_ir=True,
        seed_role="attachment",
        budget={"requests": 40, "seconds": 25, "response_bytes": 2_000_000},
        delay=0.0,
        concurrency=2,
    )
    binding = h.binding("ir_edges", version, {})
    receipt = h.cli("collect", "ir_edges", "--binding", binding, ok=False)
    exported = h.cli("export", "ir_edges", "--run", receipt["run_id"])
    docs = {Path(row["url"]).name: row for row in exported["content_documents"]}
    events = h.cli("inspect", "run", receipt["run_id"])["discovery"]["events"]

    def rejected(name, code):
        return any(
            event["url"].endswith("/" + name) and event["reason"] == code and event["snapshot_id"]
            for event in events
        )

    h.check(
        "ragged_table_rejected_before_padding", rejected("ragged.csv", "document_table_too_large")
    )
    h.check("many_csv_rows_rejected", rejected("many.csv", "document_table_too_large"))
    h.check("nul_article_rejected_before_db", rejected("nul.txt", "content_storage_unsupported"))
    h.check("unsupported_docx_content_rejected", "docx_unsupported_content" in receipt["errors"])
    h.check(
        "jsonb_unsupported_values_rejected",
        receipt["errors"].count("json_storage_unsupported") >= 1,
    )
    h.check(
        "failed_inputs_have_no_success_documents",
        sorted(docs),
        ["formula.xlsx", "interleaved.docx"],
    )
    h.check("xlsx_formula_preserved", docs["formula.xlsx"]["children"][0]["rows"][0], [1, "=A1+1"])
    h.check(
        "docx_blocks_keep_order",
        [node["type"] for node in docs["interleaved.docx"]["children"]],
        ["article", "table", "article"],
    )
    h.check(
        "docx_text_keeps_position",
        [
            node["text"]
            for node in docs["interleaved.docx"]["children"]
            if node["type"] == "article"
        ],
        ["Introduction", "Conclusion"],
    )
    h.check(
        "precision_rejected_with_snapshot", rejected("precision.json", "json_storage_unsupported")
    )
    h.capture("edges", {"receipt": receipt, "export": exported})
