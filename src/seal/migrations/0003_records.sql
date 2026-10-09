-- V1.2 adds business records without changing the URL/raw-resource contract.
-- Historical migrations, documents, revisions and results remain untouched.
CREATE TABLE seal_record (
 id text PRIMARY KEY, source_id text NOT NULL REFERENCES seal_source,
 namespace text NOT NULL, record_type text NOT NULL, record_key text NOT NULL,
 detail_url text, parent_request jsonb,
 latest_version text, latest_result text,
 next_check timestamptz NOT NULL DEFAULT now(),
 created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(source_id, namespace, record_type, record_key)
);

CREATE TABLE seal_record_result (
 id text PRIMARY KEY, record_id text NOT NULL REFERENCES seal_record,
 binding_id text NOT NULL REFERENCES seal_binding,
 processing_key text NOT NULL, output_hash text NOT NULL,
 content_hash text NOT NULL, inputs jsonb NOT NULL,
 candidate jsonb NOT NULL, checks jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(processing_key, output_hash)
);

CREATE TABLE seal_record_emission (
 id text PRIMARY KEY, run_id text NOT NULL REFERENCES seal_run,
 attempt_epoch int NOT NULL, record_id text NOT NULL REFERENCES seal_record,
 result_id text NOT NULL REFERENCES seal_record_result,
 observation_id text NOT NULL REFERENCES seal_fetch_observation,
 inputs jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE seal_record_version (
 id text PRIMARY KEY, record_id text NOT NULL REFERENCES seal_record,
 binding_id text NOT NULL REFERENCES seal_binding,
 predecessor text REFERENCES seal_record_version,
 run_id text NOT NULL REFERENCES seal_run, attempt_epoch int NOT NULL,
 content_hash text NOT NULL, schema_version text NOT NULL, data jsonb NOT NULL,
 change_reason text NOT NULL CHECK (change_reason IN ('first_seen','source_updated','reprocessed')),
 created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(record_id, binding_id, run_id, attempt_epoch)
);

ALTER TABLE seal_record ADD CONSTRAINT seal_record_latest_version_fk
 FOREIGN KEY(latest_version) REFERENCES seal_record_version;
ALTER TABLE seal_record ADD CONSTRAINT seal_record_latest_result_fk
 FOREIGN KEY(latest_result) REFERENCES seal_record_result;

CREATE INDEX seal_record_emission_run ON seal_record_emission(run_id, attempt_epoch);
CREATE INDEX seal_record_version_binding ON seal_record_version(record_id, binding_id);
CREATE INDEX seal_record_recheck ON seal_record(source_id, namespace) WHERE latest_result IS NOT NULL;

CREATE TRIGGER seal_record_result_immutable BEFORE UPDATE OR DELETE ON seal_record_result
 FOR EACH ROW EXECUTE FUNCTION seal_reject_mutation();
CREATE TRIGGER seal_record_emission_immutable BEFORE UPDATE OR DELETE ON seal_record_emission
 FOR EACH ROW EXECUTE FUNCTION seal_reject_mutation();
CREATE TRIGGER seal_record_version_immutable BEFORE UPDATE OR DELETE ON seal_record_version
 FOR EACH ROW EXECUTE FUNCTION seal_reject_mutation();
