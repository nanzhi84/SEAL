-- Legacy identities remain unknown: never rewrite historical evidence.
LOCK TABLE seal.version IN SHARE ROW EXCLUSIVE MODE;
DO $$
BEGIN
 IF EXISTS (SELECT observation_id FROM seal.version GROUP BY observation_id HAVING count(*)>1) THEN
  RAISE EXCEPTION 'version_observation_conflict: one observation created multiple versions';
 END IF;
END $$;
ALTER TABLE seal.version ADD CONSTRAINT version_observation_creator UNIQUE(observation_id);
ALTER TABLE seal.run ADD COLUMN parser_manifest jsonb;
ALTER TABLE seal.observation ADD COLUMN parser_fingerprint text;
ALTER TABLE seal.observation ADD COLUMN change_reason text;
ALTER TABLE seal.version ADD COLUMN parser_fingerprint text;
ALTER TABLE seal.version ADD COLUMN version_fingerprint text;
ALTER TABLE seal.observation ADD CONSTRAINT observation_parser_fingerprint_check
 CHECK(parser_fingerprint IS NULL OR parser_fingerprint ~ '^[0-9a-f]{64}$');
ALTER TABLE seal.version ADD CONSTRAINT version_parser_fingerprint_check
 CHECK(parser_fingerprint IS NULL OR parser_fingerprint ~ '^[0-9a-f]{64}$');
ALTER TABLE seal.version ADD CONSTRAINT version_fingerprint_check
 CHECK(version_fingerprint IS NULL OR version_fingerprint ~ '^[0-9a-f]{64}$');
-- Recreate SELECT * views so additive identity fields are visible to SQL consumers.
CREATE OR REPLACE VIEW seal.current_resource_version AS
 SELECT f.source_id,r.url,v.* FROM seal.resource r
 JOIN seal.frontier f ON f.id=r.frontier_id JOIN seal.version v ON v.id=r.current_version_id;
CREATE OR REPLACE VIEW seal.version_delta AS
 SELECT f.source_id,r.url,v.* FROM seal.version v
 JOIN seal.resource r ON r.id=v.resource_id JOIN seal.frontier f ON f.id=r.frontier_id;
