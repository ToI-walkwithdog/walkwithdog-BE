-- PostgreSQL: apply to existing databases BEFORE starting the updated app.
-- Existing history stays intact; survey identity cannot safely be backfilled.
BEGIN;
ALTER TABLE matching_results
    ADD COLUMN IF NOT EXISTS survey_id INTEGER REFERENCES surveys(id);
ALTER TABLE matching_results
    ADD COLUMN IF NOT EXISTS analysis_snapshot JSON;
CREATE UNIQUE INDEX IF NOT EXISTS uq_matching_survey_dog
    ON matching_results (survey_id, dog_id);
COMMIT;
