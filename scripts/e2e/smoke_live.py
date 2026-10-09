"""Opt-in public sources. Network failure is evidence, never a CI requirement."""

import shutil
import time
from urllib.parse import urljoin, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from parsel import Selector

from .smoke_cases import semantic
from .smoke_support import offline_guard, sha, write

PYTHON_CHAPTERS = (
    "appetite",
    "interpreter",
    "introduction",
    "controlflow",
    "datastructures",
    "modules",
    "inputoutput",
    "errors",
    "classes",
    "stdlib",
)
SOURCES = [
    {
        "id": "live_court",
        "entry": "https://www.court.gov.cn/shenpan/gengduo/77.html",
        "lists": [
            "https://www.court.gov.cn/shenpan/gengduo/77.html",
            "https://www.court.gov.cn/shenpan/gengduo/77_2.html",
        ],
        "scope": ["/shenpan/"],
        "params": {
            "links": "a[href^='/shenpan/xiangqing/']",
            "next_page": "li.next a",
            "title": "div.title",
            "body": ".txt_txt",
            "date": "time.no-date",
            "per_page": 5,
            "pages": 2,
        },
        "oracle_xpath": "//a[starts-with(@href,'/shenpan/xiangqing/')][@title]",
    },
    {
        "id": "live_spp",
        "entry": "https://www.spp.gov.cn/spp/jczdal/index.shtml",
        "lists": ["https://www.spp.gov.cn/spp/jczdal/index.shtml"],
        "scope": ["/spp/jczdal/"],
        "params": {
            "links": "a[href^='/spp/jczdal/20']",
            "next_page": "a.no-page",
            "title": ".detail_tit",
            "body": "#fontzoom",
            "date": "time.no-date",
            "per_page": 10,
            "pages": 1,
        },
        "oracle_xpath": "//a[starts-with(@href,'/spp/jczdal/20')]",
    },
    {
        "id": "live_python",
        "entries": [
            f"https://docs.python.org/3.12/tutorial/{name}.html" for name in PYTHON_CHAPTERS
        ],
        "scope": ["/3.12/tutorial/"],
        "params": {"title": "h1", "body": "div.body", "date": "time.no-date"},
    },
]


def prepare(s, spec):
    """Freeze URLs and list anchor titles before observing any Runtime output."""
    folder = s.h.output / spec["id"]
    folder.mkdir()
    expected = {}
    network = []
    opener = build_opener(ProxyHandler({}))
    for index, url in enumerate(spec.get("lists", [])):
        time.sleep(1)
        response = opener.open(
            Request(url, headers={"User-Agent": "SEAL/0.1 (+public source smoke)"}), timeout=15
        )
        body = response.read()
        (folder / f"discovery-{index}.html").write_bytes(body)
        network.append({"url": url, "status": response.status, "sha256": sha(body)})
        selector = Selector(body.decode("utf-8"))
        links = selector.xpath(spec["oracle_xpath"])[: spec["params"]["per_page"]]
        for link in links:
            expected[urljoin(url, link.attrib["href"])] = (
                link.attrib.get("title") or " ".join(link.xpath(".//text()").getall()).strip()
            )
    if "entries" in spec:
        expected = {url: None for url in spec["entries"]}
    write(folder / "discovery-requests.json", network)
    write(folder / "expected-urls-titles.json", expected)
    return folder, expected


def live(s, source_ids=None):
    h = s.h
    package = h.root / "live-recipe"
    shutil.copytree("scripts/e2e/live_recipe", package)
    shutil.copyfile("recipes/generic/recipe.py", package / "generic.py")
    recipe = h.cli("recipe", "pack", package)["recipe_version"]
    old = package / "generic.py"
    old.write_text(old.read_text().replace('.xpath(".//text()")', '.xpath("descendant::text()")'))
    changed = h.cli("recipe", "pack", package)["recipe_version"]
    for number, spec in enumerate(SOURCES, 1):
        if source_ids and spec["id"] not in source_ids:
            continue
        with s.case(f"L0{number}", spec["id"]) as row:
            row["scope"] = "10 public details, two online runs, one changed-recipe offline replay"
            row["limitations"] = [
                "No content quality or source completeness claim",
                "Publication date not mapped; date is null",
                "Public HTML only; live PDF not exercised",
                "List/detail title comparison removes whitespace only; raw values are retained",
            ]
            folder, expected = prepare(s, spec)
            h.check(spec["id"] + "_bounded_expected_details", len(expected), 10)
            entries = spec.get("entries", [spec.get("entry")])
            host = urlsplit(entries[0]).hostname
            h.config(
                spec["id"],
                entries=entries,
                allowed_hosts=[host],
                allowed_path_prefixes=spec["scope"],
                seed_role="detail" if "entries" in spec else "list",
                robots=True,
                concurrency=1,
                delay=1.0,
                scope="Public informational documents; bounded engineering archive, no login or restricted data",
                budget={"requests": 35, "seconds": 65, "response_bytes": 5 * 1024 * 1024},
            )
            binding = h.binding(spec["id"], recipe=recipe, params=spec["params"])
            results = []
            for repeat in range(2):
                run, data = s.execute(spec["id"], binding)
                write(folder / f"run-{repeat}.json", run)
                write(folder / f"export-{repeat}.json", data)
                state = s.lineage(spec["id"], run, data)
                h.check(
                    spec["id"] + "_all_details_exported",
                    sorted(d["url"] for d in data["documents"]),
                    sorted(expected),
                )
                # Retain the failed assertion and continue checking usable partial
                # inputs, repeated observations and offline replay.
                try:
                    h.check(spec["id"] + "_technical_complete", run["run"]["status"], "complete")
                except AssertionError:
                    pass
                h.check(spec["id"] + "_no_stalled_run", run["run"]["attempt_epoch"], 1)
                h.check(
                    spec["id"] + "_meaningful_bodies",
                    all(len(d["body"]) >= 100 for d in data["documents"]),
                )
                for doc in data["documents"]:
                    if expected[doc["url"]] is not None:
                        try:
                            h.check(
                                spec["id"] + "_title_from_discovery",
                                "".join(doc["title"].split()),
                                "".join(expected[doc["url"]].split()),
                            )
                        except AssertionError:
                            pass
                results.append((run, data, state))
            first, second = results
            before_docs = {d["url"]: d for d in first[1]["documents"]}
            changes = []
            for doc in second[1]["documents"]:
                previous = before_docs[doc["url"]]
                if previous["body_hash"] == doc["body_hash"]:
                    h.check(
                        spec["id"] + "_unchanged_has_same_revision",
                        doc["revision_id"],
                        previous["revision_id"],
                    )
                else:
                    changes.append(
                        {
                            "url": doc["url"],
                            "before": previous["body_hash"],
                            "after": doc["body_hash"],
                        }
                    )
                    h.check(
                        spec["id"] + "_changed_has_new_revision",
                        doc["revision_id"] != previous["revision_id"],
                    )
                h.check(
                    spec["id"] + "_new_observation",
                    doc["observation_id"] != previous["observation_id"],
                )
            row["body_changes_between_fetches"] = changes
            h.check(spec["id"] + "_no_duplicate_documents", len(second[2]["documents"]), 10)
            replacement = h.binding(spec["id"], recipe=changed, params=spec["params"])
            h.check(spec["id"] + "_recipe_changed", recipe != changed)
            archive = h.root / "archive" / "objects"
            original = {
                str(p.relative_to(archive)): sha(p.read_bytes()) for p in archive.glob("*/*")
            }
            with offline_guard(h):
                replay = h.cli("replay", first[0]["run"]["id"], "--binding", replacement)
                replay_run = h.cli("inspect", "run", replay["run_id"])
                replay_data = h.cli("export", spec["id"], "--run", replay["run_id"])
                h.check(spec["id"] + "_replay_no_observations", replay_run["observations"], [])
                h.check(
                    spec["id"] + "_replay_equivalent",
                    semantic(replay_data["documents"]),
                    semantic(first[1]["documents"]),
                )
                h.check(
                    spec["id"] + "_replay_new_version",
                    all(d["recipe_version"] == changed for d in replay_data["documents"]),
                )
                s.lineage(spec["id"], replay_run, replay_data)
            h.check(
                spec["id"] + "_original_objects_unchanged",
                {key: sha((archive / key).read_bytes()) for key in original},
                original,
            )
            write(folder / "replay.json", replay_run)
            write(folder / "replay-export.json", replay_data)
            write(folder / "original-object-hashes.json", original)
            row["detail_count"] = 10
            row["online_runs"] = 2
            row["offline_replays"] = 1
