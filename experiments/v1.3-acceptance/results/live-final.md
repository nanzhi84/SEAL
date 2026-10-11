# V1.3 exploratory live Source runs

These are observed execution results, with no expected real-site totals or business quality approval.
The exact seed host is allowed at path `/`; robots and access controls apply. Associated hosts are not implicitly allowed. Request, depth and query limits are execution budgets.
The PostgreSQL service and immutable archive were externally provisioned and were left in place.
The environment proxy and TLS verification were preserved; no direct connection retry was used.
Snapshot counts include archived HTTP error or robots responses. A proxy denial body is transport evidence and does not establish that upstream site content was fetched.

| Source | Run | Status | Snapshots | Records | Termination |
| --- | --- | --- | ---: | ---: | --- |
| dd-017 | cbb8675d-11be-4842-8297-d9cb46d24680 | partial | 0 | 0 | robots_unavailable |
| dd-041 | 858df6bf-f196-4447-8a76-1d82eb2fe11a | partial | 0 | 0 | robots_unavailable |
| dd-250 | 38ee5119-3f49-4db6-b10a-ca8108efda3f | partial | 0 | 0 | robots_unavailable |
| dd-468 | 8f40d455-9c14-497d-bc31-15673300fba9 | partial | 1 | 0 | robots_denied |
| dd-102 | 1034650d-95ef-48cd-bc5f-462fbeba1cd0 | partial | 0 | 0 | robots_unavailable |
| dd-247 | bb35fc74-d440-4d4d-9e86-650bf0a9a009 | partial | 0 | 0 | robots_unavailable |
| dd-357 | 4e649b40-8977-4214-bf95-7677f1964d56 | partial | 1 | 0 | robots_denied |
| dd-009 | aa5b8ef8-d004-4344-a99d-6d71b2ca2729 | partial | 0 | 0 | robots_unavailable |

The local evidence directory contains one directory per Source; each Source directory contains the actual Source and params, Binding, Run inspection, per-run export and a fresh database inspection after Collect. Immutable snapshot/body hashes in Run/export evidence refer to the retained archive recorded in manifest.json.

Source homepage search forms do not supply a finite list of all possible queries. This experiment does not invent TESCO, 易方达 or other search conditions. JSON endpoints and semantic `!` links must be discovered or supplied by reviewed Recipe rules; a proxy denial establishes neither upstream site availability nor template support.

The final runs were executed at 2026-10-10 18:55:53 +0800 to 2026-10-10 18:56:34 +0800, using frozen implementation commit `7906d9b59c21b700f17507578b188449053b59db` and Recipe version `fc33ff6227f2916d50f0fb6259349a5b272c88f1497f240324295cb4909e6fc6`. All eight runs terminated `partial`, with `summary.termination=blocked`. The six HTTPS Source runs failed on their robots-policy request with `TunnelError`, resulting in `robots_unavailable`; the HTTP MOE and permit hosts received proxy `403 Domain forbidden`, resulting in `robots_denied`. These labels describe the Runtime fail-closed decision, not an upstream site's actual robots policy. No DNS-based failure was observed and no upstream site page was fetched.

An independent PostgreSQL read after the driver exited found all 8 final Run rows and 32 final discovery events (24 with parent URLs). The final attempt has 8 network observations, 0 Source Records and 2 archived proxy-denial Snapshots. The cumulative evidence for these 8 live Source IDs contains 16 Runs, 16 Bindings and 64 discovery events across both attempts. Cumulative values are not per-attempt counts. The final Snapshots reused the two immutable Snapshot IDs already archived in the initial attempt; both refer to one 16-byte `Domain forbidden` body whose hash and byte size were independently checked. All original business-map seeds remained intact, including the literal `!` in dd-357.

`retention-verification.json` contains the direct database and archive check. PostgreSQL and archive remain at `/workspace/work/v13-persistent/pg` and `/workspace/work/v13-persistent/archive`. These local services depend on this managed workspace and do not establish a continuously operated production deployment. The public metadata at `experiments/v1.3-acceptance/results/live-final.json` includes exact seeds, Recipe/Binding versions, final Run IDs, counters, terminal causes, discovery parent/Scope events, archive references and hashes of the local evidence.

The executor policy must permit the seven Source hosts before current real pagination, iframe tables, XLS parsing, long legal body extraction, company search/profile discovery or unknown-template parsing on these live websites can be verified. No live template or business-quality approval is inferred from these blocked runs.
