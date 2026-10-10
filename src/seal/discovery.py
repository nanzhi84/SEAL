"""Bounded discovery evidence around Scrapy's scheduler; never schedules requests."""

from datetime import date, datetime
from fnmatch import fnmatchcase
from urllib.parse import urlsplit

from scrapy import Request, signals
from scrapy.exceptions import IgnoreRequest

from .core import SealError, digest, safe_url, uid
from .db import connect, locked_run, record_error
from .scope import paths_for, robots_policy_request


class ResourceFingerprinter:
    """Deduplicate identical wire resources without decoding or sorting queries."""

    def fingerprint(self, request):
        return bytes.fromhex(
            digest(
                {
                    "method": request.method,
                    "url": request.url.split("#", 1)[0],
                    "body_sha256": digest(request.body),
                    "headers": {
                        key: request.headers.get(key, b"").decode("latin1")
                        for key in ("Accept", "Accept-Language")
                    },
                }
            )
        )


# Bounds metadata even when a recipe emits an unbounded stream of duplicate URLs.
EVENT_LIMIT = 100_000
DOWNLOAD_FAILURES = {
    "download_failed",
    "TimeoutError",
    "DNSLookupError",
    "TCPTimedOutError",
    "ConnectionRefusedError",
    "ConnectionDone",
    "ConnectionLost",
    "ResponseNeverReceived",
    "TunnelError",
}
SKIP_REASONS = {
    "request_out_of_scope",
    "request_budget_exceeded",
    "deadline_exceeded",
    "request_method_rejected",
    "request_sensitive_or_conditional_header",
    "sensitive_url_rejected",
    "non_public_address",
    "iframe_out_of_scope",
    "attachment_host_path_required",
    "discovery_budget_exceeded",
    "discovery_depth_exceeded",
    "query_variant_budget_exceeded",
    "request_excluded",
    "robots_denied",
    "robots_unavailable",
    "robots_policy_redirect_rejected",
    "optional_entry_missing",
}


def _available(c):
    # Pre-upgrade V1/V1.1 databases remain usable by old compatibility harnesses.
    return c.execute("SELECT to_regclass('seal_discovery') AS name").fetchone()["name"] is not None


def _update(context, request, assignments, params=()):
    identity = request.meta.get("seal_discovery_id")
    if not identity:
        return
    with connect() as c:
        if not _available(c):
            return
        c.execute(
            f"""UPDATE seal_discovery SET {assignments}
                WHERE id=%s AND run_id=%s AND attempt_epoch=%s
                  AND EXISTS (SELECT 1 FROM seal_run WHERE id=%s AND attempt_epoch=%s
                              AND status IN ('running','finishing'))""",
            (
                *params,
                identity,
                context["id"],
                context["attempt_epoch"],
                context["id"],
                context["attempt_epoch"],
            ),
        )


def mark_requested(context, request, replayed=False):
    _update(
        context,
        request,
        "state='requested',requested_at=now(),request_id=%s,chain_id=%s,replayed=%s",
        (request.meta.get("seal_request_id"), request.meta.get("seal_chain_id"), replayed),
    )


def mark_archived(context, request, snapshot_id, observation_id, status=None):
    _update(
        context,
        request,
        "state='archived',archived_at=now(),snapshot_id=%s,observation_id=%s,response_status=%s",
        (snapshot_id, observation_id, status),
    )


def mark_parsed(context, request):
    # Completing a callback successfully is meaningful even when it yields zero Records.
    identity = request.meta.get("seal_discovery_id")
    if not identity:
        return
    with connect() as c:
        if not _available(c):
            return
        c.execute(
            """UPDATE seal_discovery SET state='parsed',parsed_at=now(),reason=NULL
               WHERE id=%s AND run_id=%s AND attempt_epoch=%s
                 AND state NOT IN ('failed','skipped')
                 AND (response_status IS NULL OR response_status BETWEEN 200 AND 299
                      OR (role='robots' AND response_status IN (404,410)))
                 AND EXISTS (SELECT 1 FROM seal_run WHERE id=%s AND attempt_epoch=%s
                             AND status IN ('running','finishing'))""",
            (
                identity,
                context["id"],
                context["attempt_epoch"],
                context["id"],
                context["attempt_epoch"],
            ),
        )


def mark_failed(context, request, code):
    state = "skipped" if code in SKIP_REASONS else "failed"
    _update(context, request, "state=%s,reason=%s", (state, code))


def mark_inputs_failed(context, snapshot_ids, code):
    """Associate item validation failures with the actual callback inputs."""
    if not snapshot_ids:
        return
    with connect() as c:
        if not _available(c):
            return
        c.execute(
            """UPDATE seal_discovery SET state='failed',reason=%s
               WHERE run_id=%s AND attempt_epoch=%s AND snapshot_id=ANY(%s)
                 AND EXISTS (SELECT 1 FROM seal_run WHERE id=%s AND attempt_epoch=%s
                             AND status IN ('running','finishing'))""",
            (
                code,
                context["id"],
                context["attempt_epoch"],
                list(snapshot_ids),
                context["id"],
                context["attempt_epoch"],
            ),
        )


def finish_discovery(c, run, reason):
    """Give every captured event a terminal explanation after a crawl closes."""
    if not _available(c):
        return
    c.execute(
        """UPDATE seal_discovery SET state=CASE
               WHEN continued_by IS NOT NULL THEN 'skipped'
               WHEN response_status >= 400 THEN 'failed'
               ELSE 'skipped' END,reason=CASE
               WHEN continued_by IS NOT NULL THEN 'transport_continued'
               WHEN response_status >= 400 THEN 'http_error'
               WHEN response_status >= 300 THEN 'redirect_incomplete'
               ELSE %s END
           WHERE run_id=%s AND attempt_epoch=%s
             AND state IN ('discovered','requested','archived')""",
        ("crawl_closed_" + (reason or "unknown"), run["id"], run["attempt_epoch"]),
    )


def network_counts(c, run):
    """Keep network attempts distinct from responses admitted to the archive."""
    counts = c.execute(
        """SELECT count(*) AS http_attempts,
                  count(snapshot_id) AS archived_observations
           FROM seal_fetch_observation WHERE run_id=%s AND attempt_epoch=%s
             AND origin='network'""",
        (run["id"], run["attempt_epoch"]),
    ).fetchone()
    # Sensitive bodies are rejected before an Observation is persisted. Native
    # downloader stats still count that exchange, without retaining its bytes.
    stats = run.get("report", {}).get("stats")
    counts["http_attempts_basis"] = "observations_lower_bound"
    if run.get("mode") == "replay":
        counts.update(
            http_attempts=0, archived_observations=0, http_attempts_basis="offline_replay"
        )
    elif stats is not None:
        counts.update(
            http_attempts=stats.get("downloader/request_count", 0),
            http_attempts_basis="scrapy_downloader",
        )
    return counts


def summarize_discovery(c, run, include_events=False):
    """Count discovery lifecycle separately from persisted HTTP observations.

    ``requested`` means a request passed Guard (or was restored by Replay), while
    ``http_attempts`` uses native downloader stats, including refused bodies;
    without completed stats, persisted observations are only a lower bound.
    ``parsed`` requires the final successful state, including valid zero-Record
    callbacks. ``callback_completed`` also counts callbacks whose later item
    validation failed, so a callback return cannot imply a valid structured result.
    """
    available = _available(c)
    rows = (
        c.execute(
            "SELECT * FROM seal_discovery WHERE run_id=%s AND attempt_epoch=%s ORDER BY created_at,id",
            (run["id"], run["attempt_epoch"]),
        ).fetchall()
        if available
        else []
    )
    outcomes = {}
    unknown = set()
    instrumented = available
    if (
        available
        and not rows
        and run.get("status") in ("complete", "partial", "failed", "superseded")
        and not run.get("report", {}).get("discovery", {}).get("instrumented")
    ):
        # Installing a table cannot retrospectively make historical zero evidence
        # a measured zero. Completion writes instrumented=True for new attempts.
        instrumented = False
        unknown.add("discovery_not_recorded")
    # A failed transport attempt remains visible, but a successful final callback resolves
    # its coverage uncertainty. Lineage is used only for evidence, never for deduplication.
    by_id = {row["id"]: row for row in rows}

    def recovered(row):
        seen = set()
        while row.get("continued_by") and row["id"] not in seen:
            seen.add(row["id"])
            row = by_id.get(row["continued_by"])
            if row is None:
                return False
        return row["state"] == "parsed"

    for row in rows:
        reason = row["reason"]
        if reason:
            outcomes[reason] = outcomes.get(reason, 0) + 1
        expected_scope = row.get("scope_decision") == "rejected" and reason in {
            "request_out_of_scope",
            "request_excluded",
            "iframe_out_of_scope",
            "attachment_host_path_required",
        }
        if (
            row["state"] in ("failed", "skipped")
            and reason not in {"duplicate_request", "optional_entry_missing"}
            and not expected_scope
        ):
            if not recovered(row):
                status = row["response_status"]
                if status == 429:
                    unknown.add("source_rate_limited")
                elif status is not None and status >= 500:
                    unknown.add("http_5xx")
                elif status is not None and status >= 400:
                    unknown.add("http_error")
                elif reason in DOWNLOAD_FAILURES and row["archived_at"] is None:
                    unknown.add("download_failed")
                else:
                    unknown.add(reason or "unresolved_discovery")
        elif row["state"] in ("discovered", "requested", "archived"):
            unknown.add("unresolved_discovery")
    if "discovery_budget_exceeded" in run.get("errors", []):
        unknown.add("discovery_budget_exceeded")
    observations = network_counts(c, run)
    summary = {
        "contract": 1,
        "instrumented": instrumented,
        "discovered": len(rows),
        "requested": sum(row["requested_at"] is not None for row in rows),
        "archived": sum(row["archived_at"] is not None for row in rows),
        "parsed": sum(row["state"] == "parsed" for row in rows),
        "callback_completed": sum(row["parsed_at"] is not None for row in rows),
        "failed": sum(row["state"] == "failed" for row in rows),
        "skipped": sum(row["state"] == "skipped" for row in rows),
        "deduplicated": sum(row["reason"] == "duplicate_request" for row in rows),
        "unique_fingerprints": len({row["fingerprint"] for row in rows}),
        **observations,
        "replayed": sum(row["replayed"] for row in rows),
        "pending": sum(row["state"] in ("discovered", "requested", "archived") for row in rows),
        "unknown_coverage": bool(unknown),
        "unknown_coverage_reasons": sorted(unknown),
        "outcomes": outcomes,
        "event_limit": EVENT_LIMIT,
    }
    if include_events:
        summary["events"] = [
            {
                key: value.isoformat() if isinstance(value, (date, datetime)) else value
                for key, value in row.items()
            }
            for row in rows
        ]
    return summary


class Discovery:
    @classmethod
    def from_crawler(cls, crawler):
        value = cls()
        value.crawler = crawler
        value.context = crawler.settings["SEAL_CONTEXT"]
        with connect() as c:
            value.enabled = _available(c)
            value.metadata_enabled = (
                value.enabled
                and c.execute(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                    "WHERE table_name='seal_discovery' AND column_name='discovery_method') AS present"
                ).fetchone()["present"]
            )
            value.count = (
                c.execute(
                    "SELECT count(*) AS n FROM seal_discovery WHERE run_id=%s AND attempt_epoch=%s",
                    (value.context["id"], value.context["attempt_epoch"]),
                ).fetchone()["n"]
                if value.enabled
                else 0
            )
        value.limited = False
        value.query_variants = {}
        value.known_fingerprints = set()
        crawler._seal_discovery = value
        crawler.signals.connect(value.scheduled, signal=signals.request_scheduled)
        crawler.signals.connect(value.dropped, signal=signals.request_dropped)
        return value

    def decision(self, request, already_seen=False):
        """Bound strategies supplied by the frozen recipe, without a URL frontier."""
        config = self.context.get("config", {})
        params = self.context.get("seeded_policy", self.context.get("params", {}))
        parsed = urlsplit(request.url)
        if config and not robots_policy_request(config, request):
            if not paths_for(config, parsed.hostname, parsed.path):
                return "request_out_of_scope", "rejected"
        if request.meta.get("_seal_robots_policy"):
            return None, "policy"
        if any(fnmatchcase(request.url, pattern) for pattern in params.get("exclude_patterns", [])):
            return "request_excluded", "rejected"
        if (
            not already_seen
            and "max_depth" in params
            and request.meta.get("seal_depth", 0) > params["max_depth"]
        ):
            return "discovery_depth_exceeded", "limited"
        maximum = params.get("max_query_variants")
        if maximum is not None and parsed.query:
            key = request.method, parsed.scheme, parsed.netloc, parsed.path
            variants = self.query_variants.setdefault(key, set())
            identity = digest(parsed.query)
            if identity not in variants and len(variants) >= maximum:
                return "query_variant_budget_exceeded", "limited"
            variants.add(identity)
        return None, "allowed"

    def scheduled(self, request, spider=None):
        # This signal runs before Scheduler.enqueue_request / native Dupefilter.
        # Copies used by retry/redirect must get a fresh event rather than overwriting
        # the discovery that led to their original exchange.
        if not self.enabled:
            return
        previous = request.meta.pop("seal_discovery_id", None)
        callback_output = request.meta.pop("_seal_discovery_from_callback", False)
        transport = "discovery"
        if previous and not callback_output:
            if request.meta.get("retry_times"):
                transport = "retry"
            elif request.meta.get("redirect_times"):
                transport = "redirect"
        if self.count >= EVENT_LIMIT:
            if not self.limited:
                record_error(
                    self.context["id"], self.context["attempt_epoch"], "discovery_budget_exceeded"
                )
                self.limited = True
            self.crawler.stats.inc_value("seal/discovery_budget_exceeded")
            # The sole pre-scheduler rejection is the evidence memory bound, not
            # an alternative frontier. Request scope/budget still belongs to Guard.
            raise IgnoreRequest("discovery_budget_exceeded")
        identity = uid()
        fingerprint = self.crawler.request_fingerprinter.fingerprint(request).hex()
        rejection, scope_decision = self.decision(
            request, fingerprint in getattr(self, "known_fingerprints", set())
        )
        # Historical recipes leave scope and budget decisions to RequestGuard.
        strategy = "seeded_policy" in self.context
        if not strategy and not request.meta.get("_seal_robots_policy"):
            rejection = None
        request.meta["seal_scope_decision"] = scope_decision
        parent = request.meta.get("seal_parent_url")
        if not parent and request.headers.get("Referer"):
            parent = request.headers["Referer"].decode("latin1")
        try:
            url = safe_url(request.url)
            parent = safe_url(parent) if parent else None
        except SealError:
            # Requests are HTTP(S), but untrusted Referer headers may not be.
            url, parent = safe_url(request.url), None
        with connect() as c:
            # Item writers lock Source -> Run. The INSERT's Run and Source foreign
            # keys must not acquire KEY SHARE locks in the inverse order while a
            # concurrent item is waiting for Run. Fence this attempt under the
            # same explicit lock order before recording any scheduler evidence.
            source, run = locked_run(c, self.context["id"])
            if (
                source["id"] != self.context["source_id"]
                or run["attempt_epoch"] != self.context["attempt_epoch"]
                or run["status"] != "running"
            ):
                raise IgnoreRequest("stale_attempt")
            c.execute(
                """INSERT INTO seal_discovery
                   (id,run_id,attempt_epoch,source_id,url,method,role,fingerprint,
                    parent_url,parent_snapshot_id,parent_observation_id,
                    parent_discovery_id,transport,dont_filter)
                   SELECT %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
                   FROM seal_run WHERE id=%s AND attempt_epoch=%s AND status='running'""",
                (
                    identity,
                    self.context["id"],
                    self.context["attempt_epoch"],
                    self.context["source_id"],
                    url,
                    request.method,
                    request.meta.get("seal_role", "aux"),
                    fingerprint,
                    parent,
                    request.meta.get("seal_parent_snapshot_id"),
                    request.meta.get("seal_parent_observation_id"),
                    previous if transport != "discovery" else None,
                    transport,
                    request.dont_filter,
                    self.context["id"],
                    self.context["attempt_epoch"],
                ),
            )
            if getattr(self, "metadata_enabled", False):
                c.execute(
                    "UPDATE seal_discovery SET discovery_method=%s,depth=%s,scope_decision=%s "
                    "WHERE id=%s",
                    (
                        transport
                        if transport != "discovery"
                        else request.meta.get(
                            "seal_discovery_method", "seed" if not parent else "html"
                        ),
                        request.meta.get("seal_depth", 0),
                        scope_decision,
                        identity,
                    ),
                )
            if transport != "discovery":
                c.execute(
                    """UPDATE seal_discovery SET continued_by=%s
                       WHERE id=%s AND run_id=%s AND attempt_epoch=%s
                         AND EXISTS (SELECT 1 FROM seal_run WHERE id=%s AND attempt_epoch=%s
                                     AND status IN ('running','finishing'))""",
                    (
                        identity,
                        previous,
                        self.context["id"],
                        self.context["attempt_epoch"],
                        self.context["id"],
                        self.context["attempt_epoch"],
                    ),
                )
        request.meta["seal_discovery_id"] = identity
        self.count += 1
        rejection = request.meta.get("seal_helper_rejection") or rejection
        if rejection:
            _update(self.context, request, "state='skipped',reason=%s", (rejection,))
            if scope_decision != "rejected" or not strategy:
                record_error(self.context["id"], self.context["attempt_epoch"], rejection)
            raise IgnoreRequest(rejection)
        if hasattr(self, "known_fingerprints"):
            self.known_fingerprints.add(fingerprint)

    def dropped(self, request, spider=None):
        if not self.enabled:
            return
        identity = request.meta.get("seal_discovery_id")
        if not identity:
            return
        with connect() as c:
            c.execute(
                """UPDATE seal_discovery SET state='skipped',reason='duplicate_request',
                   duplicate_of=(SELECT other.id FROM seal_discovery other
                     WHERE other.run_id=seal_discovery.run_id
                       AND other.attempt_epoch=seal_discovery.attempt_epoch
                       AND other.fingerprint=seal_discovery.fingerprint
                       AND other.id<>seal_discovery.id
                       AND other.reason IS DISTINCT FROM 'duplicate_request'
                     ORDER BY other.created_at,other.id LIMIT 1)
                   WHERE id=%s AND run_id=%s AND attempt_epoch=%s
                     AND EXISTS (SELECT 1 FROM seal_run WHERE id=%s AND attempt_epoch=%s
                                 AND status IN ('running','finishing'))""",
                (
                    identity,
                    self.context["id"],
                    self.context["attempt_epoch"],
                    self.context["id"],
                    self.context["attempt_epoch"],
                ),
            )


class DiscoverySpiderMiddleware:
    @classmethod
    def from_crawler(cls, crawler):
        value = cls()
        value.context = crawler.settings["SEAL_CONTEXT"]
        return value

    @staticmethod
    def lineage(response, output):
        if isinstance(output, Request):
            if not output.meta.pop("_seal_explicit_policy_parent", False):
                output.meta["seal_parent_url"] = response.url
                output.meta["seal_parent_snapshot_id"] = response.meta.get("seal_snapshot_id")
                output.meta["seal_parent_observation_id"] = response.meta.get("seal_observation_id")
            output.meta["_seal_discovery_from_callback"] = True
            output.meta.setdefault("seal_depth", response.meta.get("seal_depth", 0) + 1)
        elif isinstance(output, dict) and output.get("type") == "diagnostic":
            # Historical Recipes emitted only type/code. Bind their diagnostics
            # to the callback input before Items records a parsing failure.
            output = dict(output)
            if response.meta.get("seal_snapshot_id"):
                output.setdefault("snapshot_id", response.meta["seal_snapshot_id"])
            if response.meta.get("seal_observation_id"):
                output.setdefault("observation_id", response.meta["seal_observation_id"])
        return output

    def process_spider_output(self, response, result, spider=None):
        for output in result:
            yield self.lineage(response, output)
        mark_parsed(self.context, response.request)

    async def process_spider_output_async(self, response, result, spider=None):
        async for output in result:
            yield self.lineage(response, output)
        mark_parsed(self.context, response.request)
