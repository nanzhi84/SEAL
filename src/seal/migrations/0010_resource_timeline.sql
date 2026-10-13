-- Additive upgrade: historical Document/Recipe/Binding tables remain read-only history.
-- No AST-to-CrawlResult conversion: old evidence is not a native CrawlResult.
ALTER TABLE seal.run ALTER COLUMN binding_id DROP NOT NULL;
ALTER TABLE seal.run ALTER COLUMN namespace SET DEFAULT 'runtime';
ALTER TABLE seal.run ADD COLUMN config jsonb;
ALTER TABLE seal.run ADD COLUMN config_hash text;
UPDATE seal.run SET status='failed',completed_at=now(),
 errors=errors || '["crawl4ai_upgrade_requires_new_run"]'::jsonb
 WHERE status NOT IN ('complete','failed');
CREATE TABLE seal.frontier (
 id text PRIMARY KEY, source_id text NOT NULL UNIQUE REFERENCES seal.source(id),
 run_id text REFERENCES seal.run(id), checkpoint jsonb NOT NULL DEFAULT '{}',
 updated_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO seal.frontier(id,source_id) SELECT gen_random_uuid()::text,id FROM seal.source;
CREATE TABLE seal.resource (
 id text PRIMARY KEY, frontier_id text NOT NULL REFERENCES seal.frontier(id),
 url text NOT NULL, role text NOT NULL DEFAULT 'content' CHECK(role IN ('content','policy')),
 current_version_id text, last_checked_at timestamptz, last_outcome text,
 next_check timestamptz NOT NULL DEFAULT now(),
 UNIQUE(frontier_id,url), UNIQUE(id,frontier_id)
);
CREATE TABLE seal.observation (
 id text PRIMARY KEY, resource_id text NOT NULL REFERENCES seal.resource(id),
 run_id text NOT NULL REFERENCES seal.run(id), attempt_epoch int NOT NULL,
 delivery_key text NOT NULL UNIQUE, url text NOT NULL, actual_url text,
 status int, body_hash text, archive_ref text, input_mode text NOT NULL,
 outcome text NOT NULL, error text, version_id text,
 observed_at timestamptz NOT NULL DEFAULT now(),
 CHECK((body_hash IS NULL) = (archive_ref IS NULL)),
 UNIQUE(id,resource_id), UNIQUE(id,resource_id,body_hash,input_mode)
);
CREATE TABLE seal.version (
 id text PRIMARY KEY, resource_id text NOT NULL REFERENCES seal.resource(id),
 sequence int NOT NULL CHECK(sequence>0), observation_id text NOT NULL,
 body_hash text NOT NULL, input_mode text NOT NULL,
 crawl_result jsonb NOT NULL, framework_version text NOT NULL, parser_config jsonb NOT NULL,
 cursor bigserial UNIQUE, created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(resource_id,sequence), UNIQUE(id,resource_id),
 FOREIGN KEY(observation_id,resource_id,body_hash,input_mode)
  REFERENCES seal.observation(id,resource_id,body_hash,input_mode)
);
ALTER TABLE seal.observation ADD CONSTRAINT observation_version_fk
 FOREIGN KEY(version_id,resource_id) REFERENCES seal.version(id,resource_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE seal.resource ADD CONSTRAINT resource_current_version_fk
 FOREIGN KEY(current_version_id,id) REFERENCES seal.version(id,resource_id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX timeline_observation_run ON seal.observation(run_id,observed_at);
CREATE INDEX resource_due ON seal.resource(frontier_id,next_check);
CREATE TRIGGER version_immutable BEFORE UPDATE OR DELETE ON seal.version
 FOR EACH ROW EXECUTE FUNCTION seal.reject_mutation();
CREATE TRIGGER observation_immutable BEFORE UPDATE OR DELETE ON seal.observation
 FOR EACH ROW EXECUTE FUNCTION seal.reject_mutation();
CREATE VIEW seal.current_resource_version AS
 SELECT f.source_id,r.url,v.* FROM seal.resource r
 JOIN seal.frontier f ON f.id=r.frontier_id JOIN seal.version v ON v.id=r.current_version_id;
CREATE VIEW seal.version_delta AS
 SELECT f.source_id,r.url,v.* FROM seal.version v
 JOIN seal.resource r ON r.id=v.resource_id JOIN seal.frontier f ON f.id=r.frontier_id;
