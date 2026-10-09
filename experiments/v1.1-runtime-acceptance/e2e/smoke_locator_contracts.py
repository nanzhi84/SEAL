"""Behavior contracts established before the Runtime locator repair."""

from .smoke_cases import RECIPE, semantic
from .smoke_support import offline_guard, sha, write


def negative_and_status(s, recipe):
    h = s.h
    faults = {
        "unknown_transform": "unsupported_locator_transform",
        "wrong_locator": "invalid_locator_date",
        "wrong_output": "field_locator_mismatch",
        "invalid_date": "invalid_locator_date",
        "empty_segments": "empty_field_segments",
        "fabricated_separator": "invalid_segment_separator",
        "wrong_segment": "field_locator_mismatch",
        "nested_input": "nested_snapshot_not_supported",
    }
    for number, (fault, error) in enumerate(faults.items(), 1):
        with s.case(f"N{number:02d}", fault):
            source = "reject_" + fault
            binding = s.bind(
                source, ["/notice/simple", "/notice/alternate"], recipe, params={"fault": fault}
            )
            first = None
            for queue in (False, True):
                run, data = s.execute(source, binding, queue=queue)
                h.check(fault + "_partial", run["run"]["status"], "partial")
                h.check(fault + "_diagnostic", error in run["run"]["errors"])
                h.check(
                    fault + "_valid_result_only",
                    [d["url"] for d in data["documents"]],
                    [s.url + "/notice/simple"],
                )
                h.check(fault + "_raw_inputs_retained", len(run["observations"]), 2)
                s.lineage(source, run, data)
                h.check(fault + "_not_in_success_view", h.export(source)["documents"], [])
                if first is None:
                    first = (run, data)
                else:
                    h.check(
                        fault + "_worker_semantics",
                        semantic(data["documents"]),
                        semantic(first[1]["documents"]),
                    )
            with offline_guard(h):
                replay = h.cli("replay", first[0]["run"]["id"], ok=False)
                replay_run = h.cli("inspect", "run", replay["run_id"])
                replay_export = h.cli("export", source, "--run", replay["run_id"])
                h.check(fault + "_replay_error", error in replay["errors"])
                h.check(fault + "_replay_partial", replay["status"], "partial")
                h.check(fault + "_replay_no_observations", replay_run["observations"], [])
                h.check(
                    fault + "_replay_keeps_valid_result",
                    semantic(replay_export["documents"]),
                    semantic(first[1]["documents"]),
                )
                s.lineage(source, replay_run, replay_export)


def network_boundaries(s, recipe):
    h = s.h
    with s.case("N09", "Private IPs rejected before HTTP; loopback is test-only"):
        binding = s.bind("loopback_guard", ["/notice/simple"], recipe)
        before = s.control("stats")["requests"].get("/notice/simple", 0)
        allowed = h.env.pop("SEAL_ALLOW_LOOPBACK", None)
        try:
            run, data = s.execute("loopback_guard", binding)
            h.check(
                "loopback_requires_explicit_test_opt_in",
                "non_public_address" in run["run"]["errors"],
            )
            h.check("blocked_loopback_no_observations", run["observations"], [])
            h.check("blocked_loopback_no_result", data["documents"], [])
        finally:
            if allowed is not None:
                h.env["SEAL_ALLOW_LOOPBACK"] = allowed
        h.check(
            "blocked_loopback_no_http",
            s.control("stats")["requests"].get("/notice/simple", 0),
            before,
        )
        for number, host in enumerate(("10.0.0.1", "169.254.169.254", "[::1]")):
            source = "private_" + str(number)
            h.config(
                source,
                entries=[f"http://{host}/notice"],
                allowed_hosts=[host.strip("[]")],
                seed_role="detail",
            )
            private = h.binding(source, recipe=recipe)
            allowed = h.env.pop("SEAL_ALLOW_LOOPBACK", None)
            try:
                with offline_guard(h):
                    run, data = s.execute(source, private)
                    h.check(
                        "nonpublic_address_rejected:" + host,
                        "non_public_address" in run["run"]["errors"],
                    )
                    h.check("nonpublic_no_network_observation:" + host, run["observations"], [])
            finally:
                if allowed is not None:
                    h.env["SEAL_ALLOW_LOOPBACK"] = allowed
    with s.case("N10", "Domain/path guards also apply to explicit robots URL"):
        h.config(
            "path_guard", entries=[s.url + "/notice/simple"], allowed_path_prefixes=["/notice/"]
        )
        # Recipe follows a real list response; any future accidental robots exception is rejected.
        package = h.root / "scope-recipe"
        import shutil

        shutil.copytree(RECIPE, package)
        file = package / "recipe.py"
        file.write_text(
            file.read_text().replace(
                'seed["url"],', 'seed["url"].replace("/notice/simple", "/robots.txt"),'
            )
        )
        version = h.cli("recipe", "pack", package)["recipe_version"]
        binding = h.binding("path_guard", recipe=version)
        run, data = s.execute("path_guard", binding)
        h.check("robots_cannot_bypass_path_scope", "request_out_of_scope" in run["run"]["errors"])
        h.check("robots_out_of_scope_no_observation", run["observations"], [])
        file.write_text(
            file.read_text()
            .replace('"/robots.txt"', '"/notice/simple"')
            .replace(
                'seed["url"].replace("/notice/simple", "/notice/simple")',
                'seed["url"].replace("127.0.0.1", "localhost")',
            )
        )
        version = h.cli("recipe", "pack", package)["recipe_version"]
        binding = h.binding("path_guard", recipe=version)
        run, data = s.execute("path_guard", binding)
        h.check("unlisted_host_rejected", "request_out_of_scope" in run["run"]["errors"])
        h.check("unlisted_host_no_observation", run["observations"], [])


def replay_all(s, recipe, golden_runs):
    h = s.h
    changed = s.changed_recipe(RECIPE)
    h.check("replay_recipe_really_changed", changed != recipe)
    targets = []
    for _case, source, binding, original, data in golden_runs.values():
        changed_binding = h.binding(source, recipe=changed)
        documents = h.cli("inspect", "source", source)["documents"]
        targets.append((source, binding, changed_binding, original, data, documents))
    archive = h.root / "archive" / "objects"
    before = {str(p.relative_to(archive)): sha(p.read_bytes()) for p in archive.glob("*/*")}
    s.stop_server()
    with offline_guard(h):
        for source, binding, changed_binding, original, expected, online in targets:
            for target in (binding, changed_binding):
                replay = h.cli("replay", original["run"]["id"], "--binding", target)
                run = h.cli("inspect", "run", replay["run_id"])
                data = h.cli("export", source, "--run", replay["run_id"])
                h.check(source + "_offline_complete", replay["status"], "complete")
                h.check(
                    source + "_offline_semantics",
                    semantic(data["documents"]),
                    semantic(expected["documents"]),
                )
                h.check(source + "_offline_no_observations", run["observations"], [])
                s.lineage(source, run, data)
            current = [
                d
                for d in h.cli("inspect", "source", source)["documents"]
                if d["namespace"] == "runtime"
            ]
            h.check(source + "_online_unchanged", current, online)
    h.check(
        "replay_preserves_original_objects",
        {key: sha((archive / key).read_bytes()) for key in before},
        before,
    )
    write(h.output / "original-object-hashes.json", before)


def calendar_rejection(s, recipe):
    h = s.h
    with s.case("N11", "Impossible calendar date cannot normalize to a valid date"):
        h.config(
            "calendar_invalid", entries=[h.site.url + "/probe/invalid-date"], seed_role="detail"
        )
        binding = h.binding("calendar_invalid", recipe=recipe, params={"fault": "calendar_date"})
        run, data = s.execute("calendar_invalid", binding)
        h.check("impossible_date_rejected", "invalid_locator_date" in run["run"]["errors"])
        h.check("impossible_date_no_result", data["documents"], [])
        h.check("impossible_date_raw_archived", len(run["observations"]), 1)
