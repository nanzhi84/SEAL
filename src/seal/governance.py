from .config import SourceConfig
from .core import SealError, digest
from .db import connect, decision, j, one


def register_source(data):
    config = SourceConfig.model_validate(data)
    with connect() as c:
        c.execute(
            "INSERT INTO seal_source(id, config) VALUES(%s,%s) ON CONFLICT(id) DO UPDATE SET config=excluded.config",
            (config.id, j(config.model_dump())),
        )
        decision(
            c,
            config.id,
            "control",
            {"action": "source_apply", "config_hash": digest(config.model_dump())},
        )
    return {"source_id": config.id}


def review_export(run_id):
    with connect() as c:
        run = one(c, "SELECT * FROM seal_run WHERE id=%s", (run_id,))
        binding = one(c, "SELECT * FROM seal_binding WHERE id=%s", (run["binding_id"],))
        if run["mode"] != "trial" or run["status"] != "complete":
            raise SealError("complete_online_trial_required")
        return {
            "binding_id": binding["id"],
            "fingerprint": binding["fingerprint"],
            "trial_run_id": run_id,
            "report_digest": digest(run["report"]),
            "approved": False,
            "scope": "",
            "gold": [],
            "replay_run_id": None,
        }


def review_import(data):
    required = {
        "binding_id",
        "fingerprint",
        "trial_run_id",
        "report_digest",
        "approved",
        "scope",
        "gold",
        "replay_run_id",
    }
    if (
        set(data) != required
        or data["approved"] is not True
        or not isinstance(data["scope"], str)
        or len(data["scope"]) < 10
        or not data["gold"]
    ):
        raise SealError("review_scope_and_gold_required")
    with connect() as c:
        binding = one(c, "SELECT * FROM seal_binding WHERE id=%s", (data["binding_id"],))
        source = one(c, "SELECT * FROM seal_source WHERE id=%s FOR UPDATE", (binding["source_id"],))
        trial = one(c, "SELECT * FROM seal_run WHERE id=%s", (data["trial_run_id"],))
        if (
            trial["binding_id"] != binding["id"]
            or trial["mode"] != "trial"
            or trial["status"] != "complete"
            or digest(trial["report"]) != data["report_digest"]
            or binding["fingerprint"] != data["fingerprint"]
        ):
            raise SealError("review_evidence_mismatch")
        failure = c.execute(
            "SELECT completed_at FROM seal_run WHERE source_id=%s AND mode IN ('production','recheck') AND status='partial' ORDER BY completed_at DESC LIMIT 1",
            (source["id"],),
        ).fetchone()
        if source["needs_repair"] and failure and trial["completed_at"] <= failure["completed_at"]:
            raise SealError("fresh_repair_trial_required")
        results = c.execute(
            "SELECT candidate FROM seal_result WHERE id=ANY(%s)", (trial["result_ids"],)
        ).fetchall()
        for fixture in data["gold"]:
            if (
                not isinstance(fixture, dict)
                or set(fixture) - {"url", "title", "body", "date"}
                or not {"title", "body"} <= set(fixture)
            ):
                raise SealError("invalid_gold_fixture")
            # A URL, when supplied, is part of the independent expectation.
            if not any(
                all(r["candidate"].get(k) == v for k, v in fixture.items()) for r in results
            ):
                raise SealError("gold_fixture_mismatch")
        if source["binding_id"] and source["binding_id"] != binding["id"]:
            if not data["replay_run_id"]:
                raise SealError("upgrade_replay_required")
        if data["replay_run_id"]:
            replay = one(c, "SELECT * FROM seal_run WHERE id=%s", (data["replay_run_id"],))
            if (
                replay["mode"] != "replay"
                or replay["status"] != "complete"
                or replay["binding_id"] != binding["id"]
            ):
                raise SealError("upgrade_replay_invalid")
        evidence = dict(
            data, inputs_digest=digest(trial["inputs"]), results_digest=digest(trial["result_ids"])
        )
        identity = decision(c, source["id"], "review", evidence)
    return {"review_id": identity, "binding_id": binding["id"]}


def activate(binding_id, generation, rollback_reason=None):
    from .recipes import load_bundle

    with connect() as c:
        binding = one(c, "SELECT * FROM seal_binding WHERE id=%s", (binding_id,))
        load_bundle(binding["recipe_version"])
        source = one(c, "SELECT * FROM seal_source WHERE id=%s FOR UPDATE", (binding["source_id"],))
        if source["generation"] != generation:
            raise SealError("generation_conflict")
        review = c.execute(
            "SELECT * FROM seal_decision WHERE source_id=%s AND kind='review' AND payload->>'binding_id'=%s ORDER BY created_at DESC LIMIT 1",
            (source["id"], binding_id),
        ).fetchone()
        if not review:
            raise SealError("review_required")
        # A repair requires evidence newer than the failure, even for the same binding.
        last_failure = c.execute(
            "SELECT completed_at FROM seal_run WHERE source_id=%s AND mode IN ('production','recheck') AND status='partial' ORDER BY completed_at DESC LIMIT 1",
            (source["id"],),
        ).fetchone()
        if source["needs_repair"] and (
            last_failure and review["created_at"] <= last_failure["completed_at"]
        ):
            raise SealError("fresh_repair_review_required")
        c.execute(
            "UPDATE seal_source SET binding_id=%s,generation=generation+1,paused=false,needs_repair=false,next_poll=now() WHERE id=%s",
            (binding_id, source["id"]),
        )
        identity = decision(
            c,
            source["id"],
            "rollback" if rollback_reason else "activate",
            {
                "binding_id": binding_id,
                "generation": generation + 1,
                "review_id": review["id"],
                "reason": rollback_reason,
            },
        )
    return {"activation_id": identity, "generation": generation + 1}


def pause(source_id, reason):
    with connect() as c:
        source = one(c, "SELECT * FROM seal_source WHERE id=%s FOR UPDATE", (source_id,))
        c.execute(
            "UPDATE seal_source SET paused=true,generation=generation+1 WHERE id=%s", (source_id,)
        )
        decision(
            c,
            source_id,
            "control",
            {"action": "pause", "reason": reason, "generation": source["generation"] + 1},
        )
    return {"source_id": source_id, "paused": True, "generation": source["generation"] + 1}


def withdraw(result_id, reason):
    with connect() as c:
        info = one(
            c,
            "SELECT d.source_id FROM seal_result r JOIN seal_document d ON d.id=r.document_id WHERE r.id=%s",
            (result_id,),
        )
        one(c, "SELECT id FROM seal_source WHERE id=%s FOR UPDATE", (info["source_id"],))
        result = one(c, "SELECT * FROM seal_result WHERE id=%s FOR UPDATE", (result_id,))
        c.execute("UPDATE seal_result SET withdrawn=true WHERE id=%s", (result_id,))
        c.execute(
            "UPDATE seal_document SET current_result=NULL,publication_id=NULL WHERE id=%s AND current_result=%s",
            (result["document_id"], result_id),
        )
        identity = decision(
            c,
            info["source_id"],
            "withdraw",
            {"result_id": result_id, "reason": reason},
            "withdraw:" + result_id,
        )
    return {"withdrawal_id": identity, "result_id": result_id}
