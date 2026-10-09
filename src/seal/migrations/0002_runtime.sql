-- V1 schema.sql is an applied migration: never rewrite it.
-- Stop all writers before upgrading. Keep immutable V1 evidence verbatim.
INSERT INTO seal_decision(id,source_id,kind,actor,payload)
SELECT 'v1.1-migration:' || id, id, 'control', 'schema-migration',
 jsonb_build_object('action','v1.1_upgrade','previous_config',config,
                    'previous_paused',paused,'previous_generation',generation,
                    'previous_binding_id',binding_id)
FROM seal_source;

UPDATE seal_source SET config=config-'expected_urls', paused=true, generation=generation+1;
UPDATE seal_document SET namespace='runtime' WHERE namespace='production';

-- End unfinished V1 work before installing the new-write constraint.
-- Queue acknowledgements may still drain, but no old crawl may restart.
UPDATE seal_run SET status='failed', completed_at=now(),
 errors=errors || '["runtime_upgrade_requires_new_run"]'::jsonb,
 report=report || jsonb_build_object('migration','v1.1','previous_status',status)
WHERE status IN ('pending','running','finishing','retryable');

ALTER TABLE seal_run DROP CONSTRAINT seal_run_mode_check;
ALTER TABLE seal_run ADD CONSTRAINT seal_run_mode_check
 CHECK (mode IN ('collect','recheck','replay')) NOT VALID;

-- Legacy decisions remain readable, but new code may only append operator control.
ALTER TABLE seal_decision DROP CONSTRAINT seal_decision_kind_check;
ALTER TABLE seal_decision ADD CONSTRAINT seal_decision_kind_check
 CHECK (kind='control') NOT VALID;

-- Historical publication and repair columns remain as data, not Runtime contracts.
COMMENT ON COLUMN seal_source.needs_repair IS 'V1 legacy only; ignored by V1.1 Runtime';
COMMENT ON COLUMN seal_document.current_result IS 'V1 publication pointer; historical only';
COMMENT ON COLUMN seal_document.publication_id IS 'V1 publication decision; historical only';
COMMENT ON COLUMN seal_result.withdrawn IS 'V1 withdrawal state; historical only';
