-- Issue #87: shelter on-site start codes. Additive only; existing reservations and walks are unchanged.
-- Only an HMAC of each code is stored. Re-running the file is a no-op.
BEGIN;

CREATE TABLE IF NOT EXISTS shelter_code_issuances (
    id BIGSERIAL NOT NULL,
    walk_request_id INTEGER NOT NULL,
    volunteer_user_id INTEGER NOT NULL,
    issued_by INTEGER NOT NULL,
    code_digest VARCHAR(64) NOT NULL,
    issued_at TIMESTAMP WITH TIME ZONE NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    revoked_at TIMESTAMP WITH TIME ZONE,
    verified_at TIMESTAMP WITH TIME ZONE,
    used_at TIMESTAMP WITH TIME ZONE,
    PRIMARY KEY (id),
    CONSTRAINT ck_shelter_code_expiry CHECK (expires_at > issued_at),
    CONSTRAINT ck_shelter_code_digest CHECK (length(code_digest) = 64),
    CONSTRAINT ck_shelter_code_used_after_verified CHECK (used_at IS NULL OR verified_at IS NOT NULL),
    CONSTRAINT ck_shelter_code_used_not_revoked CHECK (revoked_at IS NULL OR used_at IS NULL),
    CONSTRAINT fk_shelter_code_issuances_request FOREIGN KEY (walk_request_id)
        REFERENCES walk_requests (id) ON DELETE NO ACTION,
    CONSTRAINT fk_shelter_code_issuances_volunteer FOREIGN KEY (volunteer_user_id)
        REFERENCES users (id) ON DELETE NO ACTION,
    CONSTRAINT fk_shelter_code_issuances_issuer FOREIGN KEY (issued_by)
        REFERENCES users (id) ON DELETE NO ACTION
);

-- At most one non-revoked issuance per reservation.
CREATE UNIQUE INDEX IF NOT EXISTS uq_shelter_code_issuances_active
    ON shelter_code_issuances (walk_request_id) WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS shelter_code_attempts (
    walk_request_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    failure_count SMALLINT NOT NULL,
    locked_until TIMESTAMP WITH TIME ZONE,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (walk_request_id, user_id),
    CONSTRAINT ck_shelter_code_failures CHECK (failure_count >= 0 AND failure_count <= 5),
    CONSTRAINT fk_shelter_code_attempts_request FOREIGN KEY (walk_request_id)
        REFERENCES walk_requests (id) ON DELETE NO ACTION,
    CONSTRAINT fk_shelter_code_attempts_user FOREIGN KEY (user_id)
        REFERENCES users (id) ON DELETE NO ACTION
);

COMMIT;
