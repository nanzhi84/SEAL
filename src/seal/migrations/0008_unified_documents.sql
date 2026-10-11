-- Parsed documents extend the existing namespace-isolated URL resource identity.
-- Historical raw revisions, results and migrations retain their existing contracts.
CREATE TABLE seal_document_version (
 id text PRIMARY KEY, document_id text NOT NULL REFERENCES seal_document,
 binding_id text NOT NULL REFERENCES seal_binding,
 recipe_version text NOT NULL REFERENCES seal_recipe_version,
 revision_id text NOT NULL REFERENCES seal_revision,
 snapshot_id text NOT NULL, body_hash text NOT NULL,
 observation_id text NOT NULL REFERENCES seal_fetch_observation,
 parser_version text NOT NULL, parse_options jsonb NOT NULL,
 source_format text NOT NULL, content jsonb NOT NULL,
 content_hash text NOT NULL, processing_key text NOT NULL UNIQUE,
 created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE seal_document ADD COLUMN latest_document_version text
 REFERENCES seal_document_version;

CREATE TABLE seal_document_emission (
 id text PRIMARY KEY, run_id text NOT NULL REFERENCES seal_run,
 attempt_epoch int NOT NULL, document_id text NOT NULL REFERENCES seal_document,
 document_version_id text NOT NULL REFERENCES seal_document_version,
 snapshot_id text NOT NULL, observation_id text NOT NULL REFERENCES seal_fetch_observation,
 created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(run_id,attempt_epoch,document_version_id,observation_id)
);
CREATE TABLE seal_document_reference (
 document_version_id text NOT NULL REFERENCES seal_document_version,
 ordinal int NOT NULL CHECK (ordinal >= 0),
 target_document_id text NOT NULL REFERENCES seal_document,
 url text NOT NULL, name text, relation text NOT NULL
 CHECK (relation IN ('attachment','link')),
 PRIMARY KEY(document_version_id,ordinal)
);
CREATE INDEX seal_document_version_document ON seal_document_version(document_id,created_at);
CREATE INDEX seal_document_emission_run ON seal_document_emission(run_id,attempt_epoch);
CREATE INDEX seal_document_reference_target ON seal_document_reference(target_document_id);
CREATE TRIGGER seal_document_version_immutable BEFORE UPDATE OR DELETE ON seal_document_version
 FOR EACH ROW EXECUTE FUNCTION seal_reject_mutation();
CREATE TRIGGER seal_document_emission_immutable BEFORE UPDATE OR DELETE ON seal_document_emission
 FOR EACH ROW EXECUTE FUNCTION seal_reject_mutation();
CREATE TRIGGER seal_document_reference_immutable BEFORE UPDATE OR DELETE ON seal_document_reference
 FOR EACH ROW EXECUTE FUNCTION seal_reject_mutation();
