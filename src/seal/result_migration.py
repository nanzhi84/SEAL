"""Verify historical result objects before replacing relational JSON with references."""

from .core import SealError
from .objects import Objects


def verify_result(objects, ref):
    envelope = objects.json(ref)
    if not isinstance(envelope, dict) or not isinstance(envelope.get("result"), dict):
        raise SealError("historical_result_invalid")
    for key in envelope.get("artifacts", {}).values():
        objects.get(key)
    for key in envelope["result"].get("downloaded_files") or []:
        objects.get(key)
    return envelope


def prepare_references(c):
    # Keep concurrent writers out until backfill, constraints and ledger commit together.
    c.execute("LOCK TABLE seal.run, seal.observation, seal.version IN ACCESS EXCLUSIVE MODE")
    c.execute(
        "CREATE TEMP TABLE result_refs (observation_id text PRIMARY KEY, ref text NOT NULL) ON COMMIT DROP"
    )
    objects = Objects()
    with c.cursor(name="historical_observations") as rows:
        rows.execute("""SELECT o.id,r.report->'crawl_results'->>o.id AS ref
            FROM seal.observation o JOIN seal.run r ON r.id=o.run_id""")
        for row in rows:
            if row["ref"] is not None:
                verify_result(objects, row["ref"])
                c.execute("INSERT INTO result_refs VALUES (%s,%s)", (row["id"], row["ref"]))
    with c.cursor(name="historical_versions") as rows:
        rows.execute("SELECT observation_id,crawl_result FROM seal.version ORDER BY cursor")
        for row in rows:
            ref = objects.put_json(row["crawl_result"])
            verify_result(objects, ref)
            existing = c.execute(
                "SELECT ref FROM result_refs WHERE observation_id=%s", (row["observation_id"],)
            ).fetchone()
            if existing and existing["ref"] != ref:
                raise SealError("historical_result_reference_mismatch")
            c.execute(
                "INSERT INTO result_refs VALUES (%s,%s) ON CONFLICT DO NOTHING",
                (row["observation_id"], ref),
            )
