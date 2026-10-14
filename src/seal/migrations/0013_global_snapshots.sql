-- Global publication snapshots are distinct from historical raw snapshot_id fields.
ALTER TABLE seal.frontier ADD CONSTRAINT frontier_source_identity UNIQUE(id,source_id);
CREATE TABLE seal.snapshot (
 id text PRIMARY KEY,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 sealed boolean NOT NULL DEFAULT false,
 source_count bigint NOT NULL DEFAULT 0 CHECK(source_count>=0),
 resource_count bigint NOT NULL DEFAULT 0 CHECK(resource_count>=0),
 version_count bigint NOT NULL DEFAULT 0 CHECK(version_count>=0)
);
CREATE TABLE seal.snapshot_source (
 snapshot_id text NOT NULL REFERENCES seal.snapshot(id),
 source_id text NOT NULL REFERENCES seal.source(id),
 frontier_id text NOT NULL,
 entry_urls jsonb NOT NULL CHECK(jsonb_typeof(entry_urls)='array'),
 PRIMARY KEY(snapshot_id,source_id), UNIQUE(snapshot_id,source_id,frontier_id),
 FOREIGN KEY(frontier_id,source_id) REFERENCES seal.frontier(id,source_id)
);
CREATE TABLE seal.snapshot_item (
 snapshot_id text NOT NULL,
 source_id text NOT NULL,
 frontier_id text NOT NULL,
 resource_id text NOT NULL,
 version_id text NOT NULL,
 url text NOT NULL,
 PRIMARY KEY(snapshot_id,resource_id),
 FOREIGN KEY(snapshot_id,source_id,frontier_id)
  REFERENCES seal.snapshot_source(snapshot_id,source_id,frontier_id),
 FOREIGN KEY(resource_id,frontier_id) REFERENCES seal.resource(id,frontier_id),
 FOREIGN KEY(version_id,resource_id) REFERENCES seal.version(id,resource_id)
);
CREATE INDEX snapshot_source_page ON seal.snapshot_source(snapshot_id,source_id COLLATE "C");
CREATE INDEX snapshot_result_page ON seal.snapshot_item(snapshot_id,source_id,resource_id COLLATE "C");
CREATE INDEX snapshot_inspect_page ON seal.snapshot_item(snapshot_id,resource_id COLLATE "C");
CREATE INDEX snapshot_id_page ON seal.snapshot(id COLLATE "C");

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

CREATE FUNCTION seal.snapshot_seal() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP = 'INSERT' THEN
  IF NEW.sealed OR NEW.source_count<>0 OR NEW.resource_count<>0 OR NEW.version_count<>0 THEN
   RAISE EXCEPTION 'invalid_snapshot';
  END IF;
  RETURN NEW;
 END IF;
 IF TG_OP <> 'UPDATE' OR OLD.sealed OR NOT NEW.sealed OR
   (to_jsonb(OLD)-ARRAY['sealed','source_count','resource_count','version_count']) <>
   (to_jsonb(NEW)-ARRAY['sealed','source_count','resource_count','version_count']) THEN
  RAISE EXCEPTION 'immutable_snapshot';
 END IF;
 IF NEW.source_count <> (SELECT count(*) FROM seal.snapshot_source WHERE snapshot_id=OLD.id)
 OR NEW.resource_count <> (SELECT count(*) FROM seal.snapshot_item WHERE snapshot_id=OLD.id)
 OR NEW.version_count <> (SELECT count(DISTINCT version_id) FROM seal.snapshot_item WHERE snapshot_id=OLD.id)
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
 singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
 published_snapshot_id text REFERENCES seal.snapshot(id)
);
INSERT INTO seal.publication(singleton) VALUES(true);
CREATE TABLE seal.snapshot_publication (
 sequence bigserial PRIMARY KEY,
 before_snapshot_id text REFERENCES seal.snapshot(id),
 after_snapshot_id text NOT NULL REFERENCES seal.snapshot(id),
 published_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 CHECK(before_snapshot_id IS DISTINCT FROM after_snapshot_id)
);
CREATE TRIGGER snapshot_publication_immutable BEFORE UPDATE OR DELETE ON seal.snapshot_publication
 FOR EACH ROW EXECUTE FUNCTION seal.reject_mutation();
