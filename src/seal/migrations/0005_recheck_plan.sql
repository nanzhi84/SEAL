-- Existing immutable Record candidates and raw inputs are preserved verbatim.
-- A legacy business-key parent is recovered lazily from accepted primary-input
-- evidence; this migration never guesses a request from a mutable Source entry.
ALTER TABLE seal_run ADD COLUMN recheck_plan jsonb;
ALTER TABLE seal_run ADD CONSTRAINT seal_run_recheck_plan_object
 CHECK (recheck_plan IS NULL OR jsonb_typeof(recheck_plan)='object');
ALTER TABLE seal_record ADD COLUMN recheck_retry_at timestamptz;
ALTER TABLE seal_record ADD COLUMN recheck_failures integer NOT NULL DEFAULT 0
 CHECK (recheck_failures>=0);
ALTER TABLE seal_document ADD COLUMN recheck_retry_at timestamptz;
ALTER TABLE seal_document ADD COLUMN recheck_failures integer NOT NULL DEFAULT 0
 CHECK (recheck_failures>=0);
CREATE INDEX seal_record_due ON seal_record(source_id,next_check,recheck_retry_at)
 WHERE namespace='runtime' AND latest_result IS NOT NULL;
CREATE INDEX seal_document_due ON seal_document(source_id,next_check,recheck_retry_at)
 WHERE namespace='runtime';
