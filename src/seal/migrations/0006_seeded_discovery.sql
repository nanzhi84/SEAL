-- Add evidence to the existing attempt ledger; Scrapy remains the scheduler.
ALTER TABLE seal_discovery ADD COLUMN discovery_method text;
ALTER TABLE seal_discovery ADD COLUMN depth int CHECK (depth IS NULL OR depth >= 0);
ALTER TABLE seal_discovery ADD COLUMN scope_decision text;
