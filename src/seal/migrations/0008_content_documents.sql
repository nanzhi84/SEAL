-- Additive representation layer; seal_document remains the historical result identity.
CREATE TABLE seal_content_document (
 id text PRIMARY KEY,
 source_id text NOT NULL REFERENCES seal_source,
 namespace text NOT NULL,
 url text NOT NULL,
 method text NOT NULL,
 request_body_sha256 text NOT NULL,
 latest_version text,
 created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(source_id, namespace, url, method, request_body_sha256)
);
CREATE TABLE seal_content_version (
 id text PRIMARY KEY,
 document_id text NOT NULL REFERENCES seal_content_document,
 binding_id text NOT NULL REFERENCES seal_binding,
 run_id text NOT NULL REFERENCES seal_run,
 attempt_epoch int NOT NULL,
 snapshot_id text NOT NULL,
 observation_id text NOT NULL REFERENCES seal_fetch_observation,
 body_hash text NOT NULL,
 parser_version text NOT NULL,
 processing_key text NOT NULL UNIQUE,
 content_hash text NOT NULL,
 tree jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE seal_content_document ADD CONSTRAINT seal_content_latest_version_fk
 FOREIGN KEY(latest_version) REFERENCES seal_content_version;
CREATE TABLE seal_content_ref (
 version_id text NOT NULL REFERENCES seal_content_version,
 ordinal int NOT NULL,
 target_document_id text NOT NULL REFERENCES seal_content_document,
 url text NOT NULL,
 PRIMARY KEY(version_id, ordinal)
);
CREATE TABLE seal_content_emission (
 id text PRIMARY KEY,
 run_id text NOT NULL REFERENCES seal_run,
 attempt_epoch int NOT NULL,
 version_id text NOT NULL REFERENCES seal_content_version,
 observation_id text NOT NULL REFERENCES seal_fetch_observation,
 snapshot_id text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(run_id, attempt_epoch, version_id, observation_id)
);
ALTER TABLE seal_record_result ADD COLUMN document_version_ids jsonb NOT NULL DEFAULT '[]';
CREATE INDEX seal_content_emission_run ON seal_content_emission(run_id, attempt_epoch);
CREATE INDEX seal_content_version_document ON seal_content_version(document_id, created_at);
CREATE TRIGGER seal_content_version_immutable BEFORE UPDATE OR DELETE ON seal_content_version
 FOR EACH ROW EXECUTE FUNCTION seal_reject_mutation();
CREATE TRIGGER seal_content_ref_immutable BEFORE UPDATE OR DELETE ON seal_content_ref
 FOR EACH ROW EXECUTE FUNCTION seal_reject_mutation();
CREATE TRIGGER seal_content_emission_immutable BEFORE UPDATE OR DELETE ON seal_content_emission
 FOR EACH ROW EXECUTE FUNCTION seal_reject_mutation();
