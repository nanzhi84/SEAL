-- Textual body_hash is an input fingerprint, not an archived object key.
ALTER TABLE seal.observation DROP CONSTRAINT observation_check;
ALTER TABLE seal.observation ADD CONSTRAINT observation_attachment_ref_check
 CHECK(archive_ref IS NULL OR
       (body_hash IS NOT NULL AND archive_ref=body_hash AND archive_ref ~ '^[0-9a-f]{64}$'));
ALTER TABLE seal.observation ADD CONSTRAINT observation_input_fingerprint_check
 CHECK(body_hash IS NULL OR body_hash ~ '^[0-9a-f]{64}$');
-- A no-result delivery must retain files acquired by earlier attempts too.
ALTER TABLE seal.observation ADD COLUMN attachment_refs text[] NOT NULL DEFAULT '{}';
ALTER TABLE seal.observation ADD CONSTRAINT observation_attachment_list_check
 CHECK(array_position(attachment_refs,NULL) IS NULL AND
       array_to_string(attachment_refs,',') ~ '^([0-9a-f]{64}(,[0-9a-f]{64})*)?$');
ALTER TABLE seal.observation ADD CONSTRAINT observation_primary_attachment_check
 CHECK(archive_ref IS NULL OR archive_ref=ANY(attachment_refs));
UPDATE seal.run SET report=report-'manifest_id';
