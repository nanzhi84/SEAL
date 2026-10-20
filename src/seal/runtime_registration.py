"""Atomic task expansion, replay candidates and bounded Adapter state."""

from .adapter_context import target_key
from .core import digest, uid
from .db import j
from .transport import request_key

ADAPTER_STATE_SCHEMA = 1


def register(self, result, parent=None, depth=0):
    """Persist follow-up requests/targets and complete the parent in one transaction."""

    def apply(c):
        for request in result.get("requests", []):
            self.insert_request(c, request, request.get("depth", depth))
        for target in result.get("targets", []):
            key = target_key(target)
            count = c.execute(
                """SELECT count(*) AS n FROM seal.task WHERE run_id=%s AND kind='target'
                AND key NOT LIKE 'replay:%%'""",
                (self.run_id,),
            ).fetchone()["n"]
            exists = c.execute(
                """SELECT * FROM seal.task WHERE run_id=%s AND kind='target' AND key=%s
                FOR UPDATE""",
                (self.run_id, key),
            ).fetchone()
            payload = {
                "identity": target.get("identity"),
                "context": target.get("context") or {},
                "inputs": [
                    {
                        **{k: v for k, v in item.items() if k != "request"},
                        "request_key": request_key(item["request"]),
                    }
                    for item in target["inputs"]
                ],
            }
            if exists:
                old = exists["payload"]
                if old == payload:
                    continue
                extends = all(item in payload["inputs"] for item in old["inputs"])
                if exists["state"] == "pending" and extends:
                    amend(self, c, exists, target, depth)
                    continue
                # A later occurrence is a separate candidate, even for the same identity.
                # Delivery validates all three fingerprints against the immutable fact.
                key = "replay:" + digest({"target": key, "payload": payload})
                if c.execute(
                    "SELECT 1 FROM seal.task WHERE run_id=%s AND kind='target' AND key=%s",
                    (self.run_id, key),
                ).fetchone():
                    continue
            state = "pending"
            if not exists and count >= self.config["budget"]["documents"]:
                state = "skipped"
            target_id = uid()
            c.execute(
                """INSERT INTO seal.task(id,run_id,kind,key,payload,state,error)
                VALUES(%s,%s,'target',%s,%s,%s,%s)""",
                (
                    target_id,
                    self.run_id,
                    key,
                    j(payload),
                    state,
                    "document_budget_exceeded" if state == "skipped" else None,
                ),
            )
            if state == "skipped":
                self.truncated = "document_budget_exceeded"
                continue
            for position, item in enumerate(target["inputs"]):
                request_id = self.insert_request(
                    c, item["request"], item["request"].get("depth", depth)
                )
                c.execute(
                    """INSERT INTO seal.task_input(target_id,request_id,position)
                    VALUES(%s,%s,%s) ON CONFLICT DO NOTHING""",
                    (target_id, request_id, position),
                )
        if parent is not None:
            c.execute(
                """UPDATE seal.task SET state='done',attempt_epoch=%s,
                updated_at=clock_timestamp() WHERE id=%s""",
                (self.epoch, parent),
            )
        if "state" in result:
            self.checkpoint["adapter_state"] = {
                "schema": ADAPTER_STATE_SCHEMA,
                **(result["state"] or {}),
            }
            c.execute(
                """UPDATE seal.frontier SET checkpoint=checkpoint || %s,updated_at=now()
                WHERE source_id=%s AND run_id=%s""",
                (
                    j({"adapter_state": self.checkpoint["adapter_state"]}),
                    self.run["source_id"],
                    self.run_id,
                ),
            )

    self.write(apply)


def amend(self, c, row, target, depth):
    """A not-yet-delivered target gains newly discovered required inputs."""
    payload = row["payload"]
    known = {item["request_key"] for item in payload["inputs"]}
    added = payload.get("identity") != target.get("identity") or payload.get("context") != (
        target.get("context") or {}
    )
    payload["identity"] = target.get("identity")
    payload["context"] = target.get("context") or {}
    for item in target["inputs"]:
        key = request_key(item["request"])
        if key in known:
            continue
        request_id = self.insert_request(c, item["request"], item["request"].get("depth", depth))
        c.execute(
            """INSERT INTO seal.task_input(target_id,request_id,position) VALUES(%s,%s,%s)
            ON CONFLICT DO NOTHING""",
            (row["id"], request_id, len(payload["inputs"])),
        )
        payload["inputs"].append(
            {**{k: v for k, v in item.items() if k != "request"}, "request_key": key}
        )
        known.add(key)
        added = True
    if added:
        c.execute(
            "UPDATE seal.task SET payload=%s,updated_at=clock_timestamp() WHERE id=%s",
            (j(payload), row["id"]),
        )
