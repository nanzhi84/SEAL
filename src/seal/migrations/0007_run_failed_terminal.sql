-- Stop Runtime writers before upgrading. Content-addressed archive bytes stay immutable.
-- Existing concrete report errors and terminal reasons become machine-readable errors.
-- NOT VALID still checks updated V1 trial/production rows. Reinstall the same
-- new-write constraint after normalizing those historical rows in this transaction.
ALTER TABLE seal_run DROP CONSTRAINT seal_run_mode_check;
WITH normalized AS (
 SELECT id, COALESCE(jsonb_agg(code ORDER BY code) FILTER (WHERE code IS NOT NULL),
                    '["historical_execution_failed"]'::jsonb) AS errors
 FROM seal_run r LEFT JOIN LATERAL (
   SELECT DISTINCT code FROM (
     SELECT jsonb_array_elements_text(r.errors) AS code
     UNION ALL SELECT jsonb_array_elements_text(COALESCE(r.report->'errors','[]'))
     UNION ALL SELECT r.report->>'terminal_reason'
     UNION ALL SELECT 'superseded' WHERE r.status='superseded'
   ) codes WHERE code IS NOT NULL AND code<>''
 ) codes ON true GROUP BY id
)
UPDATE seal_run r SET
 status=CASE WHEN status IN ('partial','superseded') THEN 'failed' ELSE status END,
 errors=normalized.errors,
 report=(report-'terminal_reason'-'termination') || jsonb_build_object('errors',normalized.errors)
FROM normalized WHERE normalized.id=r.id
 AND (r.status IN ('partial','superseded') OR r.report ? 'terminal_reason');

ALTER TABLE seal_run ADD CONSTRAINT seal_run_status_check
 CHECK (status IN ('pending','running','finishing','retryable','complete','failed'));
ALTER TABLE seal_run ADD CONSTRAINT seal_run_mode_check
 CHECK (mode IN ('collect','recheck','replay')) NOT VALID;
