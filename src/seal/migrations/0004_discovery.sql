-- Native Scrapy discovery evidence, scoped to an execution attempt. This is not a frontier.
CREATE TABLE seal_discovery (
 id text PRIMARY KEY,
 run_id text NOT NULL REFERENCES seal_run,
 attempt_epoch int NOT NULL,
 source_id text NOT NULL REFERENCES seal_source,
 url text NOT NULL,
 method text NOT NULL,
 role text NOT NULL,
 fingerprint text NOT NULL,
 parent_url text,
 parent_snapshot_id text,
 parent_observation_id text,
 parent_discovery_id text,
 transport text NOT NULL DEFAULT 'discovery' CHECK (transport IN ('discovery','retry','redirect')),
 dont_filter boolean NOT NULL DEFAULT false,
 state text NOT NULL DEFAULT 'discovered' CHECK (state IN ('discovered','requested','archived','parsed','failed','skipped')),
 reason text,
 duplicate_of text,
 continued_by text,
 request_id text,
 chain_id text,
 snapshot_id text,
 observation_id text,
 response_status int,
 replayed boolean NOT NULL DEFAULT false,
 created_at timestamptz NOT NULL DEFAULT now(),
 requested_at timestamptz,
 archived_at timestamptz,
 parsed_at timestamptz
);
CREATE INDEX seal_discovery_run ON seal_discovery(run_id, attempt_epoch, created_at);
CREATE INDEX seal_discovery_fingerprint ON seal_discovery(run_id, attempt_epoch, fingerprint);
