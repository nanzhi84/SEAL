-- Native Source Adapter contract: Acquisition, persistent tasks, multi-input documents,
-- 16-column delivery projection and snapshot file closure. New environments only:
-- historical content is never rewritten, backfilled or read through a compatibility path.
DO $$
BEGIN
 IF EXISTS (SELECT 1 FROM seal.source) OR EXISTS (SELECT 1 FROM seal.run)
  OR EXISTS (SELECT 1 FROM seal.resource) OR EXISTS (SELECT 1 FROM seal.snapshot) THEN
  RAISE EXCEPTION 'source_adapter_contract_requires_new_database';
 END IF;
END $$;

DROP VIEW IF EXISTS seal.current_resource_version;
DROP VIEW IF EXISTS seal.version_delta;
DROP TABLE seal.snapshot_publication, seal.publication, seal.snapshot_item, seal.snapshot_source,
 seal.snapshot, seal.observation, seal.version, seal.resource, seal.discovery,
 seal.document_emission, seal.document_reference, seal.document_version, seal.revision,
 seal.document, seal.fetch_observation CASCADE;
ALTER TABLE seal.run DROP COLUMN binding_id, DROP COLUMN namespace, DROP COLUMN inputs;
ALTER TABLE seal.source DROP COLUMN binding_id;
DROP TABLE seal.binding, seal.recipe_version CASCADE;
DROP FUNCTION IF EXISTS seal.snapshot_mapping_guard() CASCADE;
DROP FUNCTION IF EXISTS seal.snapshot_require_sealed() CASCADE;
DROP FUNCTION IF EXISTS seal.snapshot_seal() CASCADE;
ALTER TABLE seal.run DROP CONSTRAINT IF EXISTS run_mode_check;
ALTER TABLE seal.run ADD CONSTRAINT run_mode_check CHECK (mode IN ('collect','recheck','reparse'));
ALTER TABLE seal.run ADD COLUMN reparse_plan jsonb
 CHECK (reparse_plan IS NULL OR jsonb_typeof(reparse_plan)='object');

CREATE FUNCTION seal.sha256_text(value text) RETURNS boolean LANGUAGE sql IMMUTABLE AS
 $$ SELECT value ~ '^[0-9a-f]{64}$' $$;

-- One controlled external raw-data acquisition. Terminal facts are never edited in place.
CREATE TABLE seal.acquisition (
 id text PRIMARY KEY,
 source_id text NOT NULL REFERENCES seal.source(id),
 run_id text NOT NULL REFERENCES seal.run(id),
 attempt_epoch int NOT NULL CHECK (attempt_epoch > 0),
 request_key text NOT NULL CHECK (seal.sha256_text(request_key)),
 acquisition_kind text NOT NULL CHECK (acquisition_kind IN ('http','rendered_dom')),
 role text NOT NULL CHECK (role IN ('content','policy')),
 http_method text,
 request_url text NOT NULL,
 actual_url text,
 request_meta jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(request_meta)='object'),
 parent_acquisition_id text REFERENCES seal.acquisition(id),
 parent_relation text CHECK (parent_relation IN ('redirect','retry')),
 mime_type text,
 charset text,
 representation text,
 byte_size bigint CHECK (byte_size IS NULL OR byte_size >= 0),
 body_hash text CHECK (body_hash IS NULL OR seal.sha256_text(body_hash)),
 archive_ref text,
 state text NOT NULL CHECK (state IN ('running','archived','failed','cancelled','abandoned')),
 http_status int,
 outcome text,
 error text,
 started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 acquired_at timestamptz,
 CHECK ((parent_acquisition_id IS NULL) = (parent_relation IS NULL)),
 CHECK (archive_ref IS NULL OR archive_ref = body_hash),
 CHECK ((state = 'archived') = (archive_ref IS NOT NULL)),
 CHECK (state = 'running' OR acquired_at IS NOT NULL)
);
CREATE INDEX acquisition_run ON seal.acquisition(run_id, attempt_epoch, state);
CREATE INDEX acquisition_request ON seal.acquisition(source_id, request_key);
CREATE FUNCTION seal.acquisition_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP <> 'UPDATE' OR OLD.state <> 'running' OR NEW.state = 'running' OR
   (to_jsonb(OLD) - ARRAY['state','http_status','outcome','error','acquired_at','actual_url',
     'mime_type','charset','representation','byte_size','body_hash','archive_ref']) <>
   (to_jsonb(NEW) - ARRAY['state','http_status','outcome','error','acquired_at','actual_url',
     'mime_type','charset','representation','byte_size','body_hash','archive_ref']) THEN
  RAISE EXCEPTION 'immutable_acquisition';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER acquisition_terminal BEFORE UPDATE OR DELETE ON seal.acquisition
 FOR EACH ROW EXECUTE FUNCTION seal.acquisition_guard();

-- Persistent request tasks and document targets. Three completion points stay distinct:
-- archived input (request task archived), registered target (target row exists) and
-- delivered target (observation committed; target done/failed).
CREATE TABLE seal.task (
 id text PRIMARY KEY,
 run_id text NOT NULL REFERENCES seal.run(id),
 seq bigserial NOT NULL,
 kind text NOT NULL CHECK (kind IN ('request','target')),
 key text NOT NULL CHECK (seal.sha256_text(key)),
 payload jsonb NOT NULL CHECK (jsonb_typeof(payload)='object'),
 depth int NOT NULL DEFAULT 0 CHECK (depth >= 0),
 state text NOT NULL DEFAULT 'pending'
  CHECK (state IN ('pending','archived','done','failed','skipped')),
 acquisition_id text REFERENCES seal.acquisition(id),
 observation_id text,
 error text,
 attempt_epoch int,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE (run_id, kind, key)
);
CREATE INDEX task_queue ON seal.task(run_id, kind, state, seq);
CREATE TABLE seal.task_input (
 target_id text NOT NULL REFERENCES seal.task(id),
 request_id text NOT NULL REFERENCES seal.task(id),
 position int NOT NULL CHECK (position >= 0),
 PRIMARY KEY (target_id, request_id)
);
CREATE INDEX task_input_request ON seal.task_input(request_id);

CREATE TABLE seal.resource (
 id text PRIMARY KEY,
 frontier_id text NOT NULL REFERENCES seal.frontier(id),
 identity_namespace text NOT NULL CHECK (identity_namespace <> ''),
 external_key text NOT NULL CHECK (external_key <> ''),
 public_url text,
 recheck_locator jsonb NOT NULL CHECK (jsonb_typeof(recheck_locator)='object'),
 current_version_id text,
 last_checked_at timestamptz,
 last_outcome text,
 next_check timestamptz NOT NULL DEFAULT now(),
 UNIQUE (frontier_id, identity_namespace, external_key),
 UNIQUE (id, frontier_id)
);
CREATE INDEX resource_due ON seal.resource(frontier_id, next_check);

CREATE TABLE seal.observation (
 id text PRIMARY KEY,
 resource_id text NOT NULL REFERENCES seal.resource(id),
 run_id text NOT NULL REFERENCES seal.run(id),
 attempt_epoch int NOT NULL,
 delivery_key text NOT NULL UNIQUE,
 outcome text NOT NULL CHECK (outcome IN ('created','changed','unchanged','failed')),
 change_reason text,
 error text,
 inputs_ref text CHECK (inputs_ref IS NULL OR seal.sha256_text(inputs_ref)),
 result_ref text CHECK (result_ref IS NULL OR seal.sha256_text(result_ref)),
 input_fingerprint text CHECK (input_fingerprint IS NULL OR seal.sha256_text(input_fingerprint)),
 parser_fingerprint text CHECK (parser_fingerprint IS NULL OR seal.sha256_text(parser_fingerprint)),
 version_fingerprint text CHECK (version_fingerprint IS NULL OR seal.sha256_text(version_fingerprint)),
 content_hash text CHECK (content_hash IS NULL OR seal.sha256_text(content_hash)),
 version_id text,
 observed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 CHECK (outcome = 'failed' OR (inputs_ref IS NOT NULL AND result_ref IS NOT NULL
   AND version_id IS NOT NULL AND content_hash IS NOT NULL AND version_fingerprint IS NOT NULL)),
 CHECK (outcome <> 'failed' OR (version_id IS NULL AND error IS NOT NULL))
);
CREATE INDEX observation_run ON seal.observation(run_id);
CREATE TRIGGER observation_immutable BEFORE UPDATE OR DELETE ON seal.observation
 FOR EACH ROW EXECUTE FUNCTION seal.reject_mutation();
-- Later same-semantics acquisitions are appended as evidence; never merged into inputs_ref.
CREATE TABLE seal.observation_evidence (
 observation_id text NOT NULL REFERENCES seal.observation(id),
 acquisition_id text NOT NULL REFERENCES seal.acquisition(id),
 relation text NOT NULL CHECK (relation IN ('input','replay')),
 recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY (observation_id, acquisition_id, relation)
);
CREATE TRIGGER observation_evidence_immutable BEFORE UPDATE OR DELETE ON seal.observation_evidence
 FOR EACH ROW EXECUTE FUNCTION seal.reject_mutation();
CREATE TABLE seal.delivery_conflict (
 id text PRIMARY KEY,
 run_id text NOT NULL REFERENCES seal.run(id),
 resource_id text NOT NULL REFERENCES seal.resource(id),
 attempt_epoch int NOT NULL,
 reason text NOT NULL,
 existing_observation_id text REFERENCES seal.observation(id),
 attempted jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TRIGGER delivery_conflict_immutable BEFORE UPDATE OR DELETE ON seal.delivery_conflict
 FOR EACH ROW EXECUTE FUNCTION seal.reject_mutation();

CREATE SEQUENCE seal.version_cursor;
CREATE TABLE seal.version (
 id text PRIMARY KEY,
 resource_id text NOT NULL REFERENCES seal.resource(id),
 sequence int NOT NULL CHECK (sequence > 0),
 observation_id text NOT NULL UNIQUE REFERENCES seal.observation(id) DEFERRABLE INITIALLY DEFERRED,
 result_ref text NOT NULL CHECK (seal.sha256_text(result_ref)),
 inputs_ref text NOT NULL CHECK (seal.sha256_text(inputs_ref)),
 input_fingerprint text NOT NULL CHECK (seal.sha256_text(input_fingerprint)),
 parser_fingerprint text NOT NULL CHECK (seal.sha256_text(parser_fingerprint)),
 version_fingerprint text NOT NULL CHECK (seal.sha256_text(version_fingerprint)),
 content_hash text NOT NULL CHECK (seal.sha256_text(content_hash)),
 parser_config jsonb NOT NULL,
 change_reason text NOT NULL,
 cursor bigint NOT NULL DEFAULT nextval('seal.version_cursor'),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE (id, resource_id),
 UNIQUE (resource_id, sequence),
 UNIQUE (observation_id, resource_id, result_ref)
);
CREATE TRIGGER version_immutable BEFORE UPDATE OR DELETE ON seal.version
 FOR EACH ROW EXECUTE FUNCTION seal.reject_mutation();
ALTER TABLE seal.observation ADD CONSTRAINT observation_version_fk FOREIGN KEY (version_id, resource_id)
 REFERENCES seal.version(id, resource_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE seal.resource ADD CONSTRAINT resource_current_version_fk
 FOREIGN KEY (current_version_id, id) REFERENCES seal.version(id, resource_id)
 DEFERRABLE INITIALLY DEFERRED;

-- One row per delivered Markdown file and physical file (法规案例元数据上传模板, 16 columns).
CREATE TABLE seal.delivery_row (
 version_id text NOT NULL REFERENCES seal.version(id),
 row_number int NOT NULL CHECK (row_number > 0),
 title text NOT NULL CHECK (title <> ''),
 markdown_file text NOT NULL CHECK (markdown_file ~ '\.md$'),
 document_type text NOT NULL CHECK (document_type IN
  ('法律法规','政策文件','案例','行业标准','通知公告','财务报告','会计准则','税务资料','其他')),
 file_hash text NOT NULL CHECK (seal.sha256_text(file_hash)),
 acquired_at timestamptz NOT NULL,
 original_file text,
 volume_order int CHECK (volume_order IS NULL OR volume_order > 0),
 document_number text,
 issuer text,
 published_at date,
 effective_at date,
 expired_at date,
 status text,
 source_name text,
 detail_url text,
 source_record_hash text NOT NULL CHECK (seal.sha256_text(source_record_hash)),
 markdown_hash text NOT NULL CHECK (seal.sha256_text(markdown_hash)),
 original_ref text CHECK (original_ref IS NULL OR seal.sha256_text(original_ref)),
 original_media_type text,
 role text NOT NULL CHECK (role IN ('primary','volume','independent_attachment')),
 locator jsonb NOT NULL,
 PRIMARY KEY (version_id, row_number),
 CHECK ((original_ref IS NULL) = (original_file IS NULL)),
 CHECK (original_ref IS NULL OR file_hash = original_ref),
 CHECK (original_ref IS NOT NULL OR file_hash = markdown_hash),
 CHECK ((role = 'volume') = (volume_order IS NOT NULL))
);
CREATE INDEX delivery_row_file ON seal.delivery_row(file_hash);
CREATE INDEX delivery_row_markdown ON seal.delivery_row(markdown_hash);
CREATE TRIGGER delivery_row_immutable BEFORE UPDATE OR DELETE ON seal.delivery_row
 FOR EACH ROW EXECUTE FUNCTION seal.reject_mutation();

CREATE TABLE seal.snapshot (
 id text PRIMARY KEY,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 sealed boolean NOT NULL DEFAULT false,
 source_count bigint NOT NULL DEFAULT 0 CHECK (source_count >= 0),
 resource_count bigint NOT NULL DEFAULT 0 CHECK (resource_count >= 0),
 version_count bigint NOT NULL DEFAULT 0 CHECK (version_count >= 0),
 row_count bigint NOT NULL DEFAULT 0 CHECK (row_count >= 0),
 file_count bigint NOT NULL DEFAULT 0 CHECK (file_count >= 0)
);
CREATE TABLE seal.snapshot_source (
 snapshot_id text NOT NULL REFERENCES seal.snapshot(id),
 source_id text NOT NULL REFERENCES seal.source(id),
 frontier_id text NOT NULL REFERENCES seal.frontier(id),
 coverage jsonb NOT NULL CHECK (jsonb_typeof(coverage)='object'),
 PRIMARY KEY (snapshot_id, source_id),
 UNIQUE (snapshot_id, source_id, frontier_id)
);
CREATE TABLE seal.snapshot_item (
 snapshot_id text NOT NULL,
 source_id text NOT NULL,
 frontier_id text NOT NULL,
 resource_id text NOT NULL,
 version_id text NOT NULL,
 PRIMARY KEY (snapshot_id, resource_id),
 FOREIGN KEY (snapshot_id, source_id, frontier_id)
  REFERENCES seal.snapshot_source(snapshot_id, source_id, frontier_id),
 FOREIGN KEY (resource_id, frontier_id) REFERENCES seal.resource(id, frontier_id),
 FOREIGN KEY (version_id, resource_id) REFERENCES seal.version(id, resource_id)
);
CREATE INDEX snapshot_item_source ON seal.snapshot_item(snapshot_id, source_id, resource_id);
-- Closure of downloadable bytes (originals and Markdown) per authorized Source.
CREATE TABLE seal.snapshot_file (
 snapshot_id text NOT NULL,
 source_id text NOT NULL,
 sha256 text NOT NULL CHECK (seal.sha256_text(sha256)),
 kind text NOT NULL CHECK (kind IN ('original','markdown')),
 media_type text NOT NULL,
 file_name text NOT NULL,
 byte_size bigint NOT NULL CHECK (byte_size >= 0),
 PRIMARY KEY (snapshot_id, source_id, sha256, kind),
 FOREIGN KEY (snapshot_id, source_id) REFERENCES seal.snapshot_source(snapshot_id, source_id)
);
CREATE INDEX snapshot_file_hash ON seal.snapshot_file(snapshot_id, sha256);

CREATE FUNCTION seal.snapshot_mapping_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE frozen boolean;
BEGIN
 IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'immutable_snapshot_mapping'; END IF;
 SELECT sealed INTO frozen FROM seal.snapshot WHERE id=NEW.snapshot_id FOR UPDATE;
 IF frozen IS DISTINCT FROM false THEN RAISE EXCEPTION 'immutable_snapshot_mapping'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER snapshot_source_immutable BEFORE INSERT OR UPDATE OR DELETE ON seal.snapshot_source
 FOR EACH ROW EXECUTE FUNCTION seal.snapshot_mapping_guard();
CREATE TRIGGER snapshot_item_immutable BEFORE INSERT OR UPDATE OR DELETE ON seal.snapshot_item
 FOR EACH ROW EXECUTE FUNCTION seal.snapshot_mapping_guard();
CREATE TRIGGER snapshot_file_immutable BEFORE INSERT OR UPDATE OR DELETE ON seal.snapshot_file
 FOR EACH ROW EXECUTE FUNCTION seal.snapshot_mapping_guard();

CREATE FUNCTION seal.snapshot_seal() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE counters text[] := ARRAY['sealed','source_count','resource_count','version_count',
  'row_count','file_count'];
BEGIN
 IF TG_OP = 'INSERT' THEN
  IF NEW.sealed OR NEW.source_count<>0 OR NEW.resource_count<>0 OR NEW.version_count<>0
    OR NEW.row_count<>0 OR NEW.file_count<>0 THEN
   RAISE EXCEPTION 'invalid_snapshot';
  END IF;
  RETURN NEW;
 END IF;
 IF TG_OP <> 'UPDATE' OR OLD.sealed OR NOT NEW.sealed OR
   (to_jsonb(OLD)-counters) <> (to_jsonb(NEW)-counters) THEN
  RAISE EXCEPTION 'immutable_snapshot';
 END IF;
 IF NEW.source_count <> (SELECT count(*) FROM seal.snapshot_source WHERE snapshot_id=OLD.id)
 OR NEW.resource_count <> (SELECT count(*) FROM seal.snapshot_item WHERE snapshot_id=OLD.id)
 OR NEW.version_count <> (SELECT count(DISTINCT version_id) FROM seal.snapshot_item
                          WHERE snapshot_id=OLD.id)
 OR NEW.row_count <> (SELECT count(*) FROM seal.snapshot_item i
                      JOIN seal.delivery_row d ON d.version_id=i.version_id WHERE i.snapshot_id=OLD.id)
 OR NEW.file_count <> (SELECT count(*) FROM seal.snapshot_file WHERE snapshot_id=OLD.id)
 THEN RAISE EXCEPTION 'invalid_snapshot_counts'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER snapshot_immutable BEFORE INSERT OR UPDATE OR DELETE ON seal.snapshot
 FOR EACH ROW EXECUTE FUNCTION seal.snapshot_seal();
CREATE FUNCTION seal.snapshot_require_sealed() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NOT (SELECT sealed FROM seal.snapshot WHERE id=NEW.id) THEN
  RAISE EXCEPTION 'snapshot_not_sealed';
 END IF;
 RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER snapshot_sealed_commit AFTER INSERT ON seal.snapshot
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION seal.snapshot_require_sealed();

CREATE TABLE seal.publication (
 singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
 published_snapshot_id text REFERENCES seal.snapshot(id)
);
INSERT INTO seal.publication(singleton) VALUES (true);
CREATE TABLE seal.snapshot_publication (
 sequence bigserial PRIMARY KEY,
 before_snapshot_id text REFERENCES seal.snapshot(id),
 after_snapshot_id text NOT NULL REFERENCES seal.snapshot(id),
 published_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 CHECK (before_snapshot_id IS DISTINCT FROM after_snapshot_id)
);
CREATE TRIGGER snapshot_publication_immutable BEFORE UPDATE OR DELETE ON seal.snapshot_publication
 FOR EACH ROW EXECUTE FUNCTION seal.reject_mutation();
UPDATE seal.frontier SET checkpoint='{}', run_id=NULL;
