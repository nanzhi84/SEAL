CREATE TABLE IF NOT EXISTS seal_source (
 id text PRIMARY KEY, config jsonb NOT NULL, binding_id text,
 generation bigint NOT NULL DEFAULT 0, run_seq bigint NOT NULL DEFAULT 0,
 write_seq bigint NOT NULL DEFAULT 0, paused boolean NOT NULL DEFAULT false,
 needs_repair boolean NOT NULL DEFAULT false, cooldown_until timestamptz,
 next_poll timestamptz NOT NULL DEFAULT now(), created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS seal_recipe_version (
 id text PRIMARY KEY, manifest jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS seal_binding (
 id text PRIMARY KEY, source_id text NOT NULL REFERENCES seal_source,
 recipe_version text NOT NULL REFERENCES seal_recipe_version,
 config jsonb NOT NULL, params jsonb NOT NULL, fingerprint text NOT NULL UNIQUE,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS seal_decision (
 id text PRIMARY KEY, source_id text NOT NULL REFERENCES seal_source,
 kind text NOT NULL CHECK (kind IN ('review','activate','rollback','withdraw','control','publish')),
 actor text NOT NULL, payload jsonb NOT NULL, idempotency_key text UNIQUE,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS seal_run (
 id text PRIMARY KEY, source_id text NOT NULL REFERENCES seal_source,
 binding_id text NOT NULL REFERENCES seal_binding,
 mode text NOT NULL CHECK (mode IN ('trial','production','recheck','replay')),
 namespace text NOT NULL, generation bigint NOT NULL, run_seq bigint NOT NULL,
 attempt_epoch int NOT NULL DEFAULT 0, max_attempts int NOT NULL DEFAULT 3,
 deadline timestamptz NOT NULL, job_id bigint, slot text UNIQUE,
 status text NOT NULL DEFAULT 'pending', seeds jsonb NOT NULL,
 replay_inputs jsonb NOT NULL DEFAULT '[]', inputs jsonb NOT NULL DEFAULT '[]',
 result_ids jsonb NOT NULL DEFAULT '[]', refs jsonb NOT NULL DEFAULT '[]',
 errors jsonb NOT NULL DEFAULT '[]', report jsonb NOT NULL DEFAULT '{}',
 created_at timestamptz NOT NULL DEFAULT now(), completed_at timestamptz
);
CREATE TABLE IF NOT EXISTS seal_fetch_observation (
 id text PRIMARY KEY, source_id text NOT NULL REFERENCES seal_source,
 run_id text NOT NULL REFERENCES seal_run, attempt_epoch int NOT NULL,
 request_id text NOT NULL UNIQUE, request_key text NOT NULL, method text NOT NULL,
 url text NOT NULL, status int, snapshot_id text, body_hash text,
 requested_at timestamptz NOT NULL, fetched_at timestamptz,
 archived_at timestamptz NOT NULL DEFAULT now(), origin text NOT NULL DEFAULT 'network',
 error text, parent_id text, chain_id text, final_url text,
 eligible boolean NOT NULL DEFAULT false
);
CREATE TABLE IF NOT EXISTS seal_document (
 id text PRIMARY KEY, source_id text NOT NULL REFERENCES seal_source,
 namespace text NOT NULL, identity text NOT NULL, url text NOT NULL,
 latest_revision text, latest_observation text, current_result text, publication_id text,
 next_check timestamptz NOT NULL DEFAULT now(),
 UNIQUE(source_id,namespace,identity)
);
CREATE TABLE IF NOT EXISTS seal_revision (
 id text PRIMARY KEY, document_id text NOT NULL REFERENCES seal_document,
 predecessor text REFERENCES seal_revision, body_hash text NOT NULL,
 snapshot_id text NOT NULL, observation_id text NOT NULL REFERENCES seal_fetch_observation,
 created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(document_id,observation_id)
);
CREATE TABLE IF NOT EXISTS seal_result (
 id text PRIMARY KEY, document_id text NOT NULL REFERENCES seal_document,
 binding_id text NOT NULL REFERENCES seal_binding,
 processing_key text NOT NULL UNIQUE, inputs jsonb NOT NULL,
 output_hash text NOT NULL, candidate jsonb NOT NULL, checks jsonb NOT NULL,
 withdrawn boolean NOT NULL DEFAULT false, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS seal_observation_run ON seal_fetch_observation(run_id, attempt_epoch);
CREATE INDEX IF NOT EXISTS seal_decision_source ON seal_decision(source_id, kind);
CREATE INDEX IF NOT EXISTS seal_run_source ON seal_run(source_id, created_at);

CREATE OR REPLACE FUNCTION seal_reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'immutable SEAL record'; END $$;
DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='seal_binding_immutable') THEN
  CREATE TRIGGER seal_binding_immutable BEFORE UPDATE OR DELETE ON seal_binding FOR EACH ROW EXECUTE FUNCTION seal_reject_mutation();
  CREATE TRIGGER seal_recipe_immutable BEFORE UPDATE OR DELETE ON seal_recipe_version FOR EACH ROW EXECUTE FUNCTION seal_reject_mutation();
  CREATE TRIGGER seal_decision_immutable BEFORE UPDATE OR DELETE ON seal_decision FOR EACH ROW EXECUTE FUNCTION seal_reject_mutation();
  CREATE TRIGGER seal_revision_immutable BEFORE UPDATE OR DELETE ON seal_revision FOR EACH ROW EXECUTE FUNCTION seal_reject_mutation();
 END IF;
END $$;
CREATE OR REPLACE FUNCTION seal_result_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' OR (to_jsonb(NEW)-'withdrawn') IS DISTINCT FROM (to_jsonb(OLD)-'withdrawn') OR (OLD.withdrawn AND NOT NEW.withdrawn) THEN
  RAISE EXCEPTION 'immutable SEAL result';
 END IF;
 RETURN NEW;
END $$;
DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='seal_result_immutable') THEN
  CREATE TRIGGER seal_result_immutable BEFORE UPDATE OR DELETE ON seal_result FOR EACH ROW EXECUTE FUNCTION seal_result_immutable();
 END IF;
END $$;
