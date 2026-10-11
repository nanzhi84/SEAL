"""Observable failure and fidelity cases for the document parser."""

from pathlib import Path


def exercise_edges(h, version):
    names = ("ragged.csv", "header.docx", "formula.xlsx", "overflow.json", "nul.json")
    h.config(
        "ir_edges",
        entries=[h.site.url + "/ir/" + name for name in names],
        output_schema="record.v1",
        document_ir=True,
        seed_role="attachment",
        delay=0.0,
        concurrency=2,
    )
    binding = h.binding("ir_edges", version, {})
    receipt = h.cli("collect", "ir_edges", "--binding", binding, ok=False)
    exported = h.cli("export", "ir_edges", "--run", receipt["run_id"])
    docs = {Path(row["url"]).name: row for row in exported["content_documents"]}
    h.check("ragged_table_rejected_before_padding", "document_table_too_large" in receipt["errors"])
    h.check("unsupported_docx_content_rejected", "docx_unsupported_content" in receipt["errors"])
    h.check(
        "jsonb_unsupported_values_rejected",
        receipt["errors"].count("json_storage_unsupported") >= 1,
    )
    h.check("failed_inputs_have_no_success_documents", sorted(docs), ["formula.xlsx"])
    h.check("xlsx_formula_preserved", docs["formula.xlsx"]["children"][0]["rows"][0], [1, "=A1+1"])
    h.capture("edges", {"receipt": receipt, "export": exported})
