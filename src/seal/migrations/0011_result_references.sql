-- result_refs is prepared by result_migration after verified S3 writes/reads.
ALTER TABLE seal.observation ADD COLUMN result_ref text
 CHECK(result_ref IS NULL OR result_ref ~ '^[0-9a-f]{64}$');
ALTER TABLE seal.version ADD COLUMN result_ref text;
ALTER TABLE seal.observation DISABLE TRIGGER observation_immutable;
ALTER TABLE seal.version DISABLE TRIGGER version_immutable;
UPDATE seal.observation o SET result_ref=r.ref FROM result_refs r WHERE o.id=r.observation_id;
UPDATE seal.version v SET result_ref=o.result_ref FROM seal.observation o WHERE o.id=v.observation_id;
ALTER TABLE seal.observation ENABLE TRIGGER observation_immutable;
ALTER TABLE seal.version ENABLE TRIGGER version_immutable;
ALTER TABLE seal.version ALTER COLUMN result_ref SET NOT NULL;
ALTER TABLE seal.version ADD CHECK(result_ref ~ '^[0-9a-f]{64}$');
ALTER TABLE seal.observation ADD UNIQUE(id,resource_id,result_ref);
ALTER TABLE seal.version ADD FOREIGN KEY(observation_id,resource_id,result_ref)
 REFERENCES seal.observation(id,resource_id,result_ref);
DROP VIEW seal.current_resource_version;
DROP VIEW seal.version_delta;
ALTER TABLE seal.version DROP COLUMN crawl_result;
CREATE VIEW seal.current_resource_version AS
 SELECT f.source_id,r.url,v.* FROM seal.resource r
 JOIN seal.frontier f ON f.id=r.frontier_id JOIN seal.version v ON v.id=r.current_version_id;
CREATE VIEW seal.version_delta AS
 SELECT f.source_id,r.url,v.* FROM seal.version v
 JOIN seal.resource r ON r.id=v.resource_id JOIN seal.frontier f ON f.id=r.frontier_id;
UPDATE seal.run SET report=report-'crawl_results' WHERE report ? 'crawl_results';
