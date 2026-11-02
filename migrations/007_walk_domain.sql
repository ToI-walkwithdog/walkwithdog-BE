-- Apply with scripts.migrate_backend 007: validated schema and backup required.

-- Additive only. Existing tables and rows remain unchanged.

BEGIN;

CREATE TABLE IF NOT EXISTS dog_trait_summaries (
	dog_id INTEGER NOT NULL,
	activity_level NUMERIC(4, 2),
	walk_need NUMERIC(4, 2),
	exploration_level NUMERIC(4, 2),
	sociability_level NUMERIC(4, 2),
	barking_level NUMERIC(4, 2),
	handling_difficulty NUMERIC(4, 2),
	walk_count INTEGER DEFAULT '0' NOT NULL,
	review_count INTEGER DEFAULT '0' NOT NULL,
	calculation_version VARCHAR(50) NOT NULL,
	evidence JSON,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (dog_id),
	CONSTRAINT ck_activity_level CHECK (activity_level BETWEEN 1 AND 5),
	CONSTRAINT ck_walk_need CHECK (walk_need BETWEEN 1 AND 5),
	CONSTRAINT ck_exploration_level CHECK (exploration_level BETWEEN 1 AND 5),
	CONSTRAINT ck_sociability_level CHECK (sociability_level BETWEEN 1 AND 5),
	CONSTRAINT ck_barking_level CHECK (barking_level BETWEEN 1 AND 5),
	CONSTRAINT ck_handling_difficulty CHECK (handling_difficulty BETWEEN 1 AND 5),
	CONSTRAINT ck_trait_counts CHECK (walk_count >= 0 AND review_count >= 0 AND review_count <= walk_count),
	CONSTRAINT ck_calculation_version CHECK (length(calculation_version) > 0),
	CONSTRAINT ck_trait_observed CHECK (walk_count > 0 OR (activity_level IS NULL AND walk_need IS NULL AND exploration_level IS NULL AND sociability_level IS NULL AND barking_level IS NULL AND handling_difficulty IS NULL)),
	CONSTRAINT ck_trait_evidence CHECK ((activity_level IS NULL AND walk_need IS NULL AND exploration_level IS NULL AND sociability_level IS NULL AND barking_level IS NULL AND handling_difficulty IS NULL) OR evidence IS NOT NULL),
	FOREIGN KEY(dog_id) REFERENCES dogs (id) ON DELETE NO ACTION
);

CREATE TABLE IF NOT EXISTS dog_walk_settings (
	id SERIAL NOT NULL,
	dog_id INTEGER NOT NULL,
	walkable BOOLEAN DEFAULT false NOT NULL,
	difficulty VARCHAR(20) DEFAULT 'BEGINNER' NOT NULL,
	precautions TEXT,
	recommended_duration_minutes INTEGER,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_dog_walk_settings_dog UNIQUE (dog_id),
	CONSTRAINT ck_difficulty CHECK (difficulty IN ('BEGINNER', 'EXPERIENCED')),
	CONSTRAINT ck_recommended_duration CHECK (recommended_duration_minutes > 0),
	FOREIGN KEY(dog_id) REFERENCES dogs (id) ON DELETE NO ACTION
);

CREATE TABLE IF NOT EXISTS volunteer_profiles (
	user_id INTEGER NOT NULL,
	level INTEGER DEFAULT '1' NOT NULL,
	total_walk_count INTEGER DEFAULT '0' NOT NULL,
	total_distance_m NUMERIC(14, 2) DEFAULT '0' NOT NULL,
	total_duration_seconds INTEGER DEFAULT '0' NOT NULL,
	unique_dog_count INTEGER DEFAULT '0' NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (user_id),
	CONSTRAINT ck_volunteer_level CHECK (level >= 1),
	CONSTRAINT ck_volunteer_totals CHECK (total_walk_count >= 0 AND total_distance_m >= 0 AND total_duration_seconds >= 0 AND unique_dog_count >= 0 AND unique_dog_count <= total_walk_count),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE NO ACTION
);

CREATE TABLE IF NOT EXISTS walk_requests (
	id SERIAL NOT NULL,
	user_id INTEGER NOT NULL,
	dog_id INTEGER NOT NULL,
	shelter_id INTEGER NOT NULL,
	requested_start_at TIMESTAMP WITH TIME ZONE NOT NULL,
	requested_end_at TIMESTAMP WITH TIME ZONE NOT NULL,
	status VARCHAR(20) DEFAULT 'PENDING' NOT NULL,
	reviewed_by INTEGER,
	reviewed_at TIMESTAMP WITH TIME ZONE,
	rejection_reason TEXT,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_status CHECK (status IN ('PENDING', 'APPROVED', 'REJECTED', 'CANCELLED')),
	CONSTRAINT ck_request_time_order CHECK (requested_start_at < requested_end_at),
	CONSTRAINT ck_review_pair CHECK ((reviewed_by IS NULL) = (reviewed_at IS NULL)),
	CONSTRAINT ck_review_required CHECK (status NOT IN ('APPROVED', 'REJECTED') OR reviewed_by IS NOT NULL),
	CONSTRAINT ck_pending_unreviewed CHECK (status <> 'PENDING' OR reviewed_by IS NULL),
	CONSTRAINT ck_rejection_reason CHECK (status <> 'REJECTED' OR rejection_reason IS NOT NULL),
	CONSTRAINT ck_rejection_status CHECK (status IN ('REJECTED', 'CANCELLED') OR rejection_reason IS NULL),
	CONSTRAINT ex_walk_requests_dog_booking EXCLUDE USING gist (int8range(dog_id, dog_id, '[]') WITH &&, tstzrange(requested_start_at, requested_end_at, '[)') WITH &&) WHERE (status = 'APPROVED'),
	CONSTRAINT ex_walk_requests_user_booking EXCLUDE USING gist (int8range(user_id, user_id, '[]') WITH &&, tstzrange(requested_start_at, requested_end_at, '[)') WITH &&) WHERE (status = 'APPROVED'),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE NO ACTION,
	FOREIGN KEY(dog_id) REFERENCES dogs (id) ON DELETE NO ACTION,
	FOREIGN KEY(shelter_id) REFERENCES shelters (id) ON DELETE NO ACTION,
	FOREIGN KEY(reviewed_by) REFERENCES users (id) ON DELETE NO ACTION
);

CREATE INDEX IF NOT EXISTS ix_walk_requests_dog_start ON walk_requests (dog_id, requested_start_at);

CREATE INDEX IF NOT EXISTS ix_walk_requests_reviewer ON walk_requests (reviewed_by);

CREATE INDEX IF NOT EXISTS ix_walk_requests_shelter_status ON walk_requests (shelter_id, status, requested_start_at);

CREATE INDEX IF NOT EXISTS ix_walk_requests_user_start ON walk_requests (user_id, requested_start_at);

CREATE UNIQUE INDEX IF NOT EXISTS uq_walk_requests_active ON walk_requests (user_id, dog_id, requested_start_at, requested_end_at) WHERE status IN ('PENDING', 'APPROVED');

CREATE TABLE IF NOT EXISTS dog_walk_slots (
	id SERIAL NOT NULL,
	walk_setting_id INTEGER NOT NULL,
	day_of_week INTEGER NOT NULL,
	start_time TIME WITHOUT TIME ZONE NOT NULL,
	end_time TIME WITHOUT TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_day_of_week CHECK (day_of_week BETWEEN 0 AND 6),
	CONSTRAINT ck_slot_time_order CHECK (start_time < end_time),
	CONSTRAINT uq_walk_slot UNIQUE (walk_setting_id, day_of_week, start_time, end_time),
	FOREIGN KEY(walk_setting_id) REFERENCES dog_walk_settings (id) ON DELETE NO ACTION
);

CREATE TABLE IF NOT EXISTS walks (
	id SERIAL NOT NULL,
	walk_request_id INTEGER NOT NULL,
	status VARCHAR(20) DEFAULT 'IN_PROGRESS' NOT NULL,
	started_at TIMESTAMP WITH TIME ZONE NOT NULL,
	ended_at TIMESTAMP WITH TIME ZONE,
	total_distance_m NUMERIC(12, 2),
	duration_seconds INTEGER,
	average_pace NUMERIC(12, 2),
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_walks_request UNIQUE (walk_request_id),
	CONSTRAINT ck_status CHECK (status IN ('IN_PROGRESS', 'COMPLETED', 'CANCELLED', 'INTERRUPTED')),
	CONSTRAINT ck_walk_time_order CHECK (ended_at IS NULL OR ended_at >= started_at),
	CONSTRAINT ck_walk_end_status CHECK ((status = 'IN_PROGRESS' AND ended_at IS NULL) OR (status <> 'IN_PROGRESS' AND ended_at IS NOT NULL)),
	CONSTRAINT ck_walk_distance CHECK (total_distance_m >= 0),
	CONSTRAINT ck_walk_duration CHECK (duration_seconds >= 0),
	CONSTRAINT ck_walk_pace CHECK (average_pace IS NULL OR (average_pace > 0 AND total_distance_m IS NOT NULL AND total_distance_m > 0 AND duration_seconds IS NOT NULL AND duration_seconds > 0)),
	FOREIGN KEY(walk_request_id) REFERENCES walk_requests (id) ON DELETE NO ACTION
);

CREATE INDEX IF NOT EXISTS ix_walks_status_started ON walks (status, started_at);

CREATE TABLE IF NOT EXISTS walk_incidents (
	id SERIAL NOT NULL,
	walk_id INTEGER NOT NULL,
	reported_by INTEGER NOT NULL,
	incident_type VARCHAR(30) NOT NULL,
	description TEXT NOT NULL,
	latitude NUMERIC(9, 6),
	longitude NUMERIC(9, 6),
	occurred_at TIMESTAMP WITH TIME ZONE NOT NULL,
	status VARCHAR(20) DEFAULT 'OPEN' NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_latitude CHECK (latitude BETWEEN -90 AND 90),
	CONSTRAINT ck_longitude CHECK (longitude BETWEEN -180 AND 180),
	CONSTRAINT ck_incident_coordinate_pair CHECK ((latitude IS NULL) = (longitude IS NULL)),
	CONSTRAINT ck_incident_type CHECK (incident_type IN ('INJURY', 'LOST_DOG', 'SAFETY_THREAT', 'OTHER')),
	CONSTRAINT ck_status CHECK (status IN ('OPEN', 'IN_REVIEW', 'RESOLVED', 'DISMISSED')),
	CONSTRAINT ck_incident_description CHECK (length(description) > 0),
	FOREIGN KEY(walk_id) REFERENCES walks (id) ON DELETE NO ACTION,
	FOREIGN KEY(reported_by) REFERENCES users (id) ON DELETE NO ACTION
);

CREATE INDEX IF NOT EXISTS ix_walk_incidents_reporter ON walk_incidents (reported_by);

CREATE INDEX IF NOT EXISTS ix_walk_incidents_status ON walk_incidents (status, created_at);

CREATE INDEX IF NOT EXISTS ix_walk_incidents_time ON walk_incidents (walk_id, occurred_at);

CREATE TABLE IF NOT EXISTS walk_markers (
	id SERIAL NOT NULL,
	walk_id INTEGER NOT NULL,
	marker_type VARCHAR(30) NOT NULL,
	latitude NUMERIC(9, 6) NOT NULL,
	longitude NUMERIC(9, 6) NOT NULL,
	recorded_at TIMESTAMP WITH TIME ZONE NOT NULL,
	memo TEXT,
	PRIMARY KEY (id),
	CONSTRAINT ck_latitude CHECK (latitude BETWEEN -90 AND 90),
	CONSTRAINT ck_longitude CHECK (longitude BETWEEN -180 AND 180),
	CONSTRAINT ck_marker_type CHECK (marker_type IN ('SNIFF', 'POOP', 'DOG_ENCOUNTER', 'PERSON_ENCOUNTER', 'BARKING')),
	FOREIGN KEY(walk_id) REFERENCES walks (id) ON DELETE NO ACTION
);

CREATE INDEX IF NOT EXISTS ix_walk_markers_time ON walk_markers (walk_id, recorded_at);

CREATE TABLE IF NOT EXISTS walk_points (
	id SERIAL NOT NULL,
	walk_id INTEGER NOT NULL,
	latitude NUMERIC(9, 6) NOT NULL,
	longitude NUMERIC(9, 6) NOT NULL,
	recorded_at TIMESTAMP WITH TIME ZONE NOT NULL,
	accuracy_m NUMERIC(10, 2),
	sequence_no INTEGER NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_latitude CHECK (latitude BETWEEN -90 AND 90),
	CONSTRAINT ck_longitude CHECK (longitude BETWEEN -180 AND 180),
	CONSTRAINT ck_point_accuracy CHECK (accuracy_m >= 0),
	CONSTRAINT ck_point_sequence CHECK (sequence_no >= 0),
	CONSTRAINT uq_walk_points_sequence UNIQUE (walk_id, sequence_no),
	FOREIGN KEY(walk_id) REFERENCES walks (id) ON DELETE NO ACTION
);

CREATE INDEX IF NOT EXISTS ix_walk_points_time ON walk_points (walk_id, recorded_at, sequence_no);

CREATE TABLE IF NOT EXISTS walk_reviews (
	id SERIAL NOT NULL,
	walk_id INTEGER NOT NULL,
	leash_pulling INTEGER,
	handling_difficulty INTEGER,
	sociability_with_people INTEGER,
	sociability_with_dogs INTEGER,
	barking_level INTEGER,
	memo TEXT,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_walk_reviews_walk UNIQUE (walk_id),
	CONSTRAINT ck_leash_pulling CHECK (leash_pulling BETWEEN 1 AND 5),
	CONSTRAINT ck_handling_difficulty CHECK (handling_difficulty BETWEEN 1 AND 5),
	CONSTRAINT ck_sociability_with_people CHECK (sociability_with_people BETWEEN 1 AND 5),
	CONSTRAINT ck_sociability_with_dogs CHECK (sociability_with_dogs BETWEEN 1 AND 5),
	CONSTRAINT ck_barking_level CHECK (barking_level BETWEEN 1 AND 5),
	CONSTRAINT ck_review_observation CHECK (leash_pulling IS NOT NULL OR handling_difficulty IS NOT NULL OR sociability_with_people IS NOT NULL OR sociability_with_dogs IS NOT NULL OR barking_level IS NOT NULL),
	FOREIGN KEY(walk_id) REFERENCES walks (id) ON DELETE NO ACTION
);

COMMIT;
