-- Explicit destructive upgrade. Stop writers, inspect catalog, back up before execution.
-- Never CASCADE: unknown external dependencies must abort this transaction.
ALTER TABLE public.seal_record DROP CONSTRAINT seal_record_latest_version_fk;
ALTER TABLE public.seal_record DROP CONSTRAINT seal_record_latest_result_fk;
DROP TABLE public.seal_record_emission;
DROP TABLE public.seal_record_version;
DROP TABLE public.seal_record_result;
DROP TABLE public.seal_record;
DROP TABLE public.seal_result;
DROP FUNCTION public.seal_result_immutable();

ALTER TABLE public.seal_document DROP COLUMN current_result, DROP COLUMN publication_id;
ALTER TABLE public.seal_source DROP COLUMN needs_repair, DROP COLUMN next_poll;
UPDATE public.seal_source SET config=config-'output_schema'-'identity'-'poll_seconds';
ALTER TABLE public.seal_run DROP COLUMN result_ids, DROP COLUMN refs,
 DROP COLUMN replay_inputs, DROP COLUMN slot;
ALTER TABLE public.seal_discovery DROP COLUMN replayed;
ALTER TABLE public.seal_fetch_observation ADD COLUMN processing_stage text NOT NULL DEFAULT 'historical';
ALTER TABLE public.seal_fetch_observation ADD COLUMN processing_error text;
ALTER TABLE public.seal_fetch_observation ALTER COLUMN processing_stage SET DEFAULT 'archived';
ALTER TABLE public.seal_fetch_observation ADD COLUMN is_content boolean NOT NULL DEFAULT false;
ALTER TABLE public.seal_document_emission ADD COLUMN change_kind text NOT NULL DEFAULT 'historical'
 CHECK (change_kind IN ('historical','first_seen','unchanged','updated','reprocessed'));
-- Preserve historical modes as existing data; all new writes are online-only.
ALTER TABLE public.seal_run DROP CONSTRAINT seal_run_mode_check;
UPDATE public.seal_run SET status='failed',completed_at=now(),
 errors=errors || '["document_only_upgrade_requires_new_run"]'::jsonb
 WHERE status IN ('pending','running','finishing','retryable');
ALTER TABLE public.seal_run ADD CONSTRAINT seal_run_mode_check
 CHECK (mode IN ('collect','recheck')) NOT VALID;

CREATE SCHEMA seal;
DO $$
DECLARE item record; table_name text; old_name text;
BEGIN
 FOR item IN SELECT c.oid,c.relname FROM pg_catalog.pg_class c
  JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
  WHERE n.nspname='public' AND c.relkind IN ('r','p') AND c.relname LIKE 'seal\_%' ESCAPE '\'
  ORDER BY c.relname
 LOOP
  table_name := substr(item.relname,6);
  EXECUTE format('ALTER TABLE public.%I SET SCHEMA seal',item.relname);
  EXECUTE format('ALTER TABLE seal.%I RENAME TO %I',item.relname,table_name);
  -- Constraint-owned indexes are renamed together with their constraints.
  FOR old_name IN SELECT conname FROM pg_catalog.pg_constraint
   WHERE conrelid=item.oid AND conname LIKE 'seal\_%' ESCAPE '\'
  LOOP
   EXECUTE format('ALTER TABLE seal.%I RENAME CONSTRAINT %I TO %I',table_name,old_name,substr(old_name,6));
  END LOOP;
  FOR old_name IN SELECT tgname FROM pg_catalog.pg_trigger
   WHERE tgrelid=item.oid AND NOT tgisinternal AND tgname LIKE 'seal\_%' ESCAPE '\'
  LOOP
   EXECUTE format('ALTER TRIGGER %I ON seal.%I RENAME TO %I',old_name,table_name,substr(old_name,6));
  END LOOP;
 END LOOP;
 FOR item IN SELECT c.relname,c.relkind FROM pg_catalog.pg_class c
  JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
  WHERE n.nspname='seal' AND c.relkind IN ('i','S') AND c.relname LIKE 'seal\_%' ESCAPE '\'
 LOOP
  IF item.relkind='i' THEN
   EXECUTE format('ALTER INDEX seal.%I RENAME TO %I',item.relname,substr(item.relname,6));
  ELSE
   EXECUTE format('ALTER SEQUENCE seal.%I RENAME TO %I',item.relname,substr(item.relname,6));
  END IF;
 END LOOP;
END $$;
ALTER FUNCTION public.seal_reject_mutation() SET SCHEMA seal;
ALTER FUNCTION seal.seal_reject_mutation() RENAME TO reject_mutation;
CREATE OR REPLACE FUNCTION seal.reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'immutable SEAL evidence'; END $$;
