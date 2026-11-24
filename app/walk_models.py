from sqlalchemy import Numeric
"""Walk persistence only. Authorization, transitions and aggregation belong to later issues."""

from sqlalchemy import (
    Numeric,
    Boolean, CheckConstraint, Column, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer,
    JSON, Numeric, String, Text, Time, UniqueConstraint, Uuid, text,
)
from sqlalchemy.dialects.postgresql import ExcludeConstraint
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base


class Timestamps:
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


def reference(table, column="id"):
    # NO ACTION keeps records and prevents implicit history deletion.
    return ForeignKey(f"{table}.{column}", ondelete="NO ACTION")


def choices(column, values):
    literals = ", ".join(repr(value) for value in values.split())
    return CheckConstraint(f"{column} IN ({literals})", name=f"ck_{column}")


def coordinates():
    return (
        CheckConstraint("latitude BETWEEN -90 AND 90", name="ck_latitude"),
        CheckConstraint("longitude BETWEEN -180 AND 180", name="ck_longitude"),
    )


class DogWalkSetting(Timestamps, Base):
    __tablename__ = "dog_walk_settings"
    __table_args__ = (
        UniqueConstraint("dog_id", name="uq_dog_walk_settings_dog"),
        choices("difficulty", "BEGINNER INTERMEDIATE EXPERIENCED"),
        CheckConstraint("recommended_duration_minutes > 0", name="ck_recommended_duration"),
        choices("size_code", "SMALL MEDIUM LARGE"),
        CheckConstraint("difficulty_score BETWEEN 1 AND 5 AND difficulty_score * 2 = CAST(difficulty_score * 2 AS INTEGER)",
                        name="ck_walk_difficulty_score"),
        CheckConstraint("max_walk_minutes > 0", name="ck_walk_max_minutes"),
    )

    id = Column(Integer, primary_key=True)
    dog_id = Column(Integer, reference("dogs"), nullable=False)
    walkable = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    difficulty = Column(String(20), nullable=False, default="BEGINNER", server_default="BEGINNER")
    precautions = Column(Text, nullable=True)
    recommended_duration_minutes = Column(Integer, nullable=True)
    size_code = Column(String(10), nullable=True)
    difficulty_score = Column(Numeric(2, 1), nullable=True)
    max_walk_minutes = Column(Integer, nullable=True)

    dog = relationship("Dog", back_populates="walk_setting")
    slots = relationship("DogWalkSlot", back_populates="walk_setting", passive_deletes="all")


class DogWalkSlot(Base):
    __tablename__ = "dog_walk_slots"
    __table_args__ = (
        CheckConstraint("day_of_week BETWEEN 0 AND 6", name="ck_day_of_week"),
        CheckConstraint("start_time < end_time", name="ck_slot_time_order"),
        UniqueConstraint("walk_setting_id", "day_of_week", "start_time", "end_time", name="uq_walk_slot"),
    )

    id = Column(Integer, primary_key=True)
    walk_setting_id = Column(Integer, reference("dog_walk_settings"), nullable=False)
    day_of_week = Column(Integer, nullable=False)
    start_time = Column(Time(timezone=False), nullable=False)
    end_time = Column(Time(timezone=False), nullable=False)

    walk_setting = relationship("DogWalkSetting", back_populates="slots")


class WalkRequest(Timestamps, Base):
    __tablename__ = "walk_requests"
    __table_args__ = (
        choices("status", "PENDING APPROVED REJECTED CANCELLED"),
        CheckConstraint("requested_start_at < requested_end_at", name="ck_request_time_order"),
        CheckConstraint("(reviewed_by IS NULL) = (reviewed_at IS NULL)", name="ck_review_pair"),
        CheckConstraint("status NOT IN ('APPROVED', 'REJECTED') OR reviewed_by IS NOT NULL", name="ck_review_required"),
        CheckConstraint("status <> 'PENDING' OR reviewed_by IS NULL", name="ck_pending_unreviewed"),
        CheckConstraint("status <> 'REJECTED' OR rejection_reason IS NOT NULL", name="ck_rejection_reason"),
        CheckConstraint("status IN ('REJECTED', 'CANCELLED') OR rejection_reason IS NULL", name="ck_rejection_status"),
        CheckConstraint("cancel_reason_code IN ('SCHEDULE_CHANGED', 'CANNOT_VISIT')", name="ck_cancel_reason_code"),
        CheckConstraint("cancelled_actor_role IN ('USER', 'SHELTER', 'ADMIN')", name="ck_cancel_actor_role"),
        CheckConstraint(
            "(cancelled_at IS NULL AND cancelled_by IS NULL AND cancelled_actor_role IS NULL AND "
            "cancel_reason_code IS NULL) OR (status = 'CANCELLED' AND cancelled_at IS NOT NULL AND "
            "cancelled_by IS NOT NULL AND cancelled_actor_role IS NOT NULL)",
            name="ck_walk_request_cancellation",
        ),
        ForeignKeyConstraint(["date_slot_id", "dog_id"], ["dog_walk_date_slots.id", "dog_walk_date_slots.dog_id"],
                             name="fk_walk_requests_date_slot", ondelete="NO ACTION"),
        CheckConstraint("(idempotency_key IS NULL AND request_fingerprint IS NULL AND creation_receipt IS NULL) OR "
                        "(idempotency_key IS NOT NULL AND request_fingerprint IS NOT NULL AND "
                        "creation_receipt IS NOT NULL AND date_slot_id IS NOT NULL)", name="ck_walk_request_receipt"),
        Index("ix_walk_requests_date_slot", "date_slot_id"),
        Index("uq_walk_requests_user_idempotency", "user_id", "idempotency_key", unique=True),
        Index("uq_walk_requests_occupied_date_slot", "date_slot_id", unique=True,
              postgresql_where=text("date_slot_id IS NOT NULL AND status IN ('PENDING', 'APPROVED')"),
              sqlite_where=text("date_slot_id IS NOT NULL AND status IN ('PENDING', 'APPROVED')")),
        Index("ix_walk_requests_user_start", "user_id", "requested_start_at"),
        Index("ix_walk_requests_dog_start", "dog_id", "requested_start_at"),
        Index("ix_walk_requests_shelter_status", "shelter_id", "status", "requested_start_at"),
        Index("ix_walk_requests_reviewer", "reviewed_by"),
        Index("uq_walk_requests_active", "user_id", "dog_id", "requested_start_at", "requested_end_at",
              unique=True, postgresql_where=text("status IN ('PENDING', 'APPROVED')"),
              sqlite_where=text("status IN ('PENDING', 'APPROVED')")),
        # Built-in range GiST operators avoid requiring the btree_gist extension.
        ExcludeConstraint(
            (text("int8range(dog_id, dog_id, '[]')"), "&&"),
            (text("tstzrange(requested_start_at, requested_end_at, '[)')"), "&&"),
            where=text("status = 'APPROVED'"), name="ex_walk_requests_dog_booking",
        ).ddl_if(dialect="postgresql"),
        ExcludeConstraint(
            (text("int8range(user_id, user_id, '[]')"), "&&"),
            (text("tstzrange(requested_start_at, requested_end_at, '[)')"), "&&"),
            where=text("status = 'APPROVED'"), name="ex_walk_requests_user_booking",
        ).ddl_if(dialect="postgresql"),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, reference("users"), nullable=False)
    dog_id = Column(Integer, reference("dogs"), nullable=False)
    # Historical ownership snapshot: dogs may move to a different shelter later.
    shelter_id = Column(Integer, reference("shelters"), nullable=False)
    requested_start_at = Column(DateTime(timezone=True), nullable=False)
    requested_end_at = Column(DateTime(timezone=True), nullable=False)
    status = Column(String(20), nullable=False, default="PENDING", server_default="PENDING")
    reviewed_by = Column(Integer, reference("users"), nullable=True)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
    rejection_reason = Column(Text, nullable=True)
    rejection_reason_code = Column(String(30), nullable=True)
    cancel_reason_code = Column(String(20), nullable=True)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)
    cancelled_by = Column(Integer, ForeignKey("users.id", name="fk_walk_requests_cancelled_by", ondelete="NO ACTION"),
                          nullable=True)
    cancelled_actor_role = Column(String(10), nullable=True)
    date_slot_id = Column(Integer, nullable=True)
    idempotency_key = Column(Uuid, nullable=True)
    request_fingerprint = Column(String(64), nullable=True)
    creation_receipt = Column(JSON(none_as_null=True), nullable=True)

    user = relationship("User", foreign_keys=[user_id], back_populates="walk_requests")
    reviewer = relationship("User", foreign_keys=[reviewed_by], back_populates="reviewed_walk_requests")
    dog = relationship("Dog", back_populates="walk_requests")
    shelter = relationship("Shelter", back_populates="walk_requests")
    walk = relationship("Walk", back_populates="walk_request", uselist=False, passive_deletes="all")


class Walk(Timestamps, Base):
    __tablename__ = "walks"
    __table_args__ = (
        UniqueConstraint("walk_request_id", name="uq_walks_request"),
        choices("status", "IN_PROGRESS COMPLETED CANCELLED INTERRUPTED"),
        CheckConstraint("ended_at IS NULL OR ended_at >= started_at", name="ck_walk_time_order"),
        CheckConstraint("(status = 'IN_PROGRESS' AND ended_at IS NULL) OR (status <> 'IN_PROGRESS' AND ended_at IS NOT NULL)", name="ck_walk_end_status"),
        CheckConstraint("total_distance_m >= 0", name="ck_walk_distance"),
        CheckConstraint("duration_seconds >= 0", name="ck_walk_duration"),
        CheckConstraint("average_pace IS NULL OR (average_pace > 0 AND total_distance_m IS NOT NULL AND total_distance_m > 0 AND duration_seconds IS NOT NULL AND duration_seconds > 0)", name="ck_walk_pace"),
        CheckConstraint("(moving_duration_seconds IS NULL AND stopped_duration_seconds IS NULL AND unknown_duration_seconds IS NULL AND moving_distance_m IS NULL) OR (moving_duration_seconds IS NOT NULL AND stopped_duration_seconds IS NOT NULL AND unknown_duration_seconds IS NOT NULL AND moving_distance_m IS NOT NULL AND duration_seconds IS NOT NULL AND total_distance_m IS NOT NULL AND ended_at IS NOT NULL AND status <> 'IN_PROGRESS' AND moving_duration_seconds >= 0 AND stopped_duration_seconds >= 0 AND unknown_duration_seconds >= 0 AND moving_distance_m >= 0 AND moving_distance_m <= total_distance_m AND CAST(moving_duration_seconds AS BIGINT) + CAST(stopped_duration_seconds AS BIGINT) + CAST(unknown_duration_seconds AS BIGINT) = duration_seconds)", name="ck_walk_motion_partition"),
        CheckConstraint("moving_duration_seconds IS NULL OR average_pace IS NULL OR (moving_duration_seconds > 0 AND moving_distance_m > 0)", name="ck_walk_motion_pace"),
        CheckConstraint("(last_latitude IS NULL) = (last_longitude IS NULL)", name="ck_walk_last_coordinate_pair"),
        CheckConstraint("last_latitude IS NULL OR (last_latitude BETWEEN -90 AND 90)", name="ck_walk_last_latitude"),
        CheckConstraint("last_longitude IS NULL OR (last_longitude BETWEEN -180 AND 180)", name="ck_walk_last_longitude"),
        CheckConstraint("last_accuracy_m IS NULL OR last_accuracy_m >= 0", name="ck_walk_last_accuracy"),
        CheckConstraint("end_reason IS NULL OR end_reason IN ('USER', 'STAFF', 'AUTO_TIMEOUT')", name="ck_walk_end_reason"),
        CheckConstraint("planned_duration_minutes IS NULL OR planned_duration_minutes > 0", name="ck_walk_planned_duration"),
        CheckConstraint("expected_end_at IS NULL OR expected_end_at > started_at", name="ck_walk_expected_end_order"),
        Index("ix_walks_status_started", "status", "started_at"),
    )

    id = Column(Integer, primary_key=True)
    walk_request_id = Column(Integer, reference("walk_requests"), nullable=False)
    status = Column(String(20), nullable=False, default="IN_PROGRESS", server_default="IN_PROGRESS")
    started_at = Column(DateTime(timezone=True), nullable=False)
    ended_at = Column(DateTime(timezone=True), nullable=True)
    total_distance_m = Column(Numeric(12, 2), nullable=True)
    duration_seconds = Column(Integer, nullable=True)
    average_pace = Column(Numeric(12, 2), nullable=True)  # seconds / kilometre
    moving_duration_seconds = Column(Integer, nullable=True)
    stopped_duration_seconds = Column(Integer, nullable=True)
    unknown_duration_seconds = Column(Integer, nullable=True)
    moving_distance_m = Column(Numeric(12, 2), nullable=True)
    last_latitude = Column(Numeric(9, 6), nullable=True)
    last_longitude = Column(Numeric(9, 6), nullable=True)
    last_accuracy_m = Column(Numeric(10, 2), nullable=True)
    last_point_recorded_at = Column(DateTime(timezone=True), nullable=True)
    last_point_received_at = Column(DateTime(timezone=True), nullable=True)
    end_reason = Column(String(20), nullable=True)
    planned_duration_minutes = Column(Integer, nullable=True)
    expected_end_at = Column(DateTime(timezone=True), nullable=True)
    handover_required = Column(Boolean, nullable=False, default=False, server_default=text("false"))

    @property
    def handover_status(self) -> str:
        if not self.handover_required:
            return "NOT_REQUIRED"
        if self.handover is None:
            return "NOT_CONFIRMED"
        return self.handover.handover_status

    @property
    def is_overdue(self) -> bool:
        from app.walk_service import compute_walk_overdue
        overdue, _ = compute_walk_overdue(self)
        return overdue

    @property
    def overdue_minutes(self) -> int:
        from app.walk_service import compute_walk_overdue
        _, minutes = compute_walk_overdue(self)
        return minutes

    walk_request = relationship("WalkRequest", back_populates="walk")
    points = relationship("WalkPoint", back_populates="walk", passive_deletes="all", order_by="WalkPoint.sequence_no")
    markers = relationship("WalkMarker", back_populates="walk", passive_deletes="all")
    review = relationship("WalkReview", back_populates="walk", uselist=False, passive_deletes="all")
    volunteer_review = relationship("ShelterVolunteerReview", back_populates="walk", uselist=False, passive_deletes="all")
    incidents = relationship("WalkIncident", back_populates="walk", passive_deletes="all")
    handover = relationship("WalkHandover", back_populates="walk", uselist=False, passive_deletes="all")
    private_memo = relationship("WalkPrivateMemo", back_populates="walk", uselist=False, passive_deletes="all")


class WalkHandover(Timestamps, Base):
    """A volunteer's statement attached to a walk; the 032 schema is separate from the 007 registry."""

    __tablename__ = "walk_handovers"
    __table_args__ = (
        UniqueConstraint("walk_id", name="uq_walk_handovers_walk"),
        CheckConstraint(
            "(handover_result = 'COMPLETE' AND dog_returned AND equipment_returned AND condition_shared "
            "AND issue_description IS NULL AND self_confirmed_at IS NOT NULL AND issue_reported_at IS NULL) OR "
            "(handover_result = 'ISSUE' AND NOT (dog_returned AND equipment_returned AND condition_shared) "
            "AND issue_description IS NOT NULL AND trim(issue_description) <> '' AND length(issue_description) <= 500 "
            "AND self_confirmed_at IS NULL AND issue_reported_at IS NOT NULL)",
            name="ck_walk_handover_result",
        ),
        CheckConstraint("level_evaluation_pending_at IS NULL OR handover_result = 'COMPLETE'",
                        name="ck_walk_handover_pending"),
    )

    id = Column(Integer, primary_key=True)
    walk_id = Column(Integer, reference("walks"), nullable=False)
    volunteer_user_id = Column(Integer, reference("users"), nullable=False)
    handover_result = Column(String(8), nullable=False)
    dog_returned = Column(Boolean, nullable=False)
    equipment_returned = Column(Boolean, nullable=False)
    condition_shared = Column(Boolean, nullable=False)
    acknowledge_auto_timeout = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    issue_description = Column(String(500), nullable=True)
    self_confirmed_at = Column(DateTime(timezone=True), nullable=True)
    issue_reported_at = Column(DateTime(timezone=True), nullable=True)
    level_evaluation_pending_at = Column(DateTime(timezone=True), nullable=True)

    walk = relationship("Walk", back_populates="handover")

    @property
    def handover_status(self) -> str:
        return "SELF_REPORTED" if self.handover_result == "COMPLETE" else "ISSUE_REPORTED"


class WalkPrivateMemo(Timestamps, Base):
    __tablename__ = "walk_private_memos"
    __table_args__ = (
        UniqueConstraint("walk_id", name="uq_walk_private_memos_walk"),
        CheckConstraint("trim(memo) <> '' AND length(memo) <= 1000", name="ck_walk_private_memo_content"),
        CheckConstraint("version >= 1", name="ck_walk_private_memo_version"),
    )

    id = Column(Integer, primary_key=True)
    walk_id = Column(Integer, reference("walks"), nullable=False)
    author_user_id = Column(Integer, reference("users"), nullable=False)
    memo = Column(String(1000), nullable=False)
    version = Column(Integer, nullable=False)

    walk = relationship("Walk", back_populates="private_memo")


class WalkPoint(Base):
    __tablename__ = "walk_points"
    __table_args__ = (
        *coordinates(),
        CheckConstraint("accuracy_m >= 0", name="ck_point_accuracy"),
        CheckConstraint("sequence_no >= 0", name="ck_point_sequence"),
        UniqueConstraint("walk_id", "sequence_no", name="uq_walk_points_sequence"),
        Index("ix_walk_points_time", "walk_id", "recorded_at", "sequence_no"),
    )

    id = Column(Integer, primary_key=True)
    walk_id = Column(Integer, reference("walks"), nullable=False)
    latitude = Column(Numeric(9, 6), nullable=False)
    longitude = Column(Numeric(9, 6), nullable=False)
    recorded_at = Column(DateTime(timezone=True), nullable=False)
    accuracy_m = Column(Numeric(10, 2), nullable=True)
    sequence_no = Column(Integer, nullable=False)

    walk = relationship("Walk", back_populates="points")


class WalkMarker(Base):
    __tablename__ = "walk_markers"
    __table_args__ = (
        *coordinates(),
        choices("marker_type", "SNIFF POOP DOG_ENCOUNTER PERSON_ENCOUNTER BARKING CROSSWALK_WAIT OTHER"),
        choices("location_source", "CURRENT LAST_KNOWN"),
        CheckConstraint("marker_type <> 'OTHER' OR (memo IS NOT NULL AND trim(memo) <> '')", name="ck_marker_other_memo"),
        Index("ix_walk_markers_time", "walk_id", "recorded_at"),
        # Issue #89 (031): optional client retry key; legacy rows keep both NULL.
        CheckConstraint("(client_marker_id IS NULL) = (request_fingerprint IS NULL)", name="ck_marker_client_key"),
        Index("uq_walk_markers_client_marker_id", "walk_id", "client_marker_id", unique=True,
              postgresql_where=text("client_marker_id IS NOT NULL"), sqlite_where=text("client_marker_id IS NOT NULL")),
    )

    id = Column(Integer, primary_key=True)
    walk_id = Column(Integer, reference("walks"), nullable=False)
    marker_type = Column(String(30), nullable=False)
    latitude = Column(Numeric(9, 6), nullable=False)
    longitude = Column(Numeric(9, 6), nullable=False)
    recorded_at = Column(DateTime(timezone=True), nullable=False)
    memo = Column(Text, nullable=True)
    location_source = Column(String(12), nullable=False, default="CURRENT", server_default="CURRENT")
    client_marker_id = Column(Uuid, nullable=True)
    request_fingerprint = Column(String(64), nullable=True)
    position_recorded_at = Column(DateTime(timezone=True), nullable=True)

    walk = relationship("Walk", back_populates="markers")


REVIEW_METRICS = ("leash_pulling", "handling_difficulty", "sociability_with_people", "sociability_with_dogs", "barking_level")
TRAIT_METRICS = ("activity_level", "walk_need", "exploration_level", "sociability_level", "barking_level", "handling_difficulty")


class WalkReview(Timestamps, Base):
    __tablename__ = "walk_reviews"
    __table_args__ = (
        UniqueConstraint("walk_id", name="uq_walk_reviews_walk"),
        *(CheckConstraint(f"{metric} BETWEEN 1 AND 5", name=f"ck_{metric}") for metric in REVIEW_METRICS),
        CheckConstraint("rating IS NULL OR (rating BETWEEN 1 AND 5)", name="ck_walk_review_rating"),
        CheckConstraint("rating IS NOT NULL OR " + " OR ".join(f"{metric} IS NOT NULL" for metric in REVIEW_METRICS), name="ck_review_observation"),
    )

    id = Column(Integer, primary_key=True)
    walk_id = Column(Integer, reference("walks"), nullable=False)
    # Author is derived from walk -> request -> user; cannot disagree with the volunteer.
    leash_pulling = Column(Integer, nullable=True)
    handling_difficulty = Column(Integer, nullable=True)
    sociability_with_people = Column(Integer, nullable=True)
    sociability_with_dogs = Column(Integer, nullable=True)
    barking_level = Column(Integer, nullable=True)
    rating = Column(Integer, nullable=True)
    comment = Column(String(100), nullable=True)
    memo = Column(Text, nullable=True)

    walk = relationship("Walk", back_populates="review")
    tags = relationship("WalkReviewTag", back_populates="review", cascade="all, delete-orphan", passive_deletes=True)


class WalkReviewTag(Base):
    __tablename__ = "walk_review_tags"
    __table_args__ = (
        UniqueConstraint("review_id", "tag_code", name="uq_walk_review_tags_review_code"),
        choices("tag_code", "ENERGETIC CALM EXCITED TENSE TIRED SCARED UNWELL"),
        Index("ix_walk_review_tags_review_id", "review_id"),
    )

    id = Column(Integer, primary_key=True)
    review_id = Column(Integer, ForeignKey("walk_reviews.id", ondelete="CASCADE"), nullable=False)
    tag_code = Column(String(30), nullable=False)

    review = relationship("WalkReview", back_populates="tags")


class DogTraitSummary(Base):
    __tablename__ = "dog_trait_summaries"
    __table_args__ = (
        *(CheckConstraint(f"{metric} BETWEEN 1 AND 5", name=f"ck_{metric}") for metric in TRAIT_METRICS),
        CheckConstraint("walk_count >= 0 AND review_count >= 0 AND review_count <= walk_count", name="ck_trait_counts"),
        CheckConstraint("length(calculation_version) > 0", name="ck_calculation_version"),
        CheckConstraint("walk_count > 0 OR (" + " AND ".join(f"{metric} IS NULL" for metric in TRAIT_METRICS) + ")", name="ck_trait_observed"),
        CheckConstraint("(" + " AND ".join(f"{metric} IS NULL" for metric in TRAIT_METRICS) + ") OR evidence IS NOT NULL", name="ck_trait_evidence"),
    )

    dog_id = Column(Integer, reference("dogs"), primary_key=True, autoincrement=False)
    activity_level = Column(Numeric(4, 2), nullable=True)
    walk_need = Column(Numeric(4, 2), nullable=True)
    exploration_level = Column(Numeric(4, 2), nullable=True)
    sociability_level = Column(Numeric(4, 2), nullable=True)
    barking_level = Column(Numeric(4, 2), nullable=True)
    handling_difficulty = Column(Numeric(4, 2), nullable=True)
    walk_count = Column(Integer, nullable=False, default=0, server_default="0")
    review_count = Column(Integer, nullable=False, default=0, server_default="0")
    calculation_version = Column(String(50), nullable=False)
    evidence = Column(JSON, nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    dog = relationship("Dog", back_populates="trait_summary")


class VolunteerProfile(Base):
    __tablename__ = "volunteer_profiles"
    __table_args__ = (
        CheckConstraint("level >= 1", name="ck_volunteer_level"),
        CheckConstraint("total_walk_count >= 0 AND total_distance_m >= 0 AND total_duration_seconds >= 0 AND unique_dog_count >= 0 AND unique_dog_count <= total_walk_count", name="ck_volunteer_totals"),
    )

    user_id = Column(Integer, reference("users"), primary_key=True, autoincrement=False)
    level = Column(Integer, nullable=False, default=1, server_default="1")
    total_walk_count = Column(Integer, nullable=False, default=0, server_default="0")
    total_distance_m = Column(Numeric(14, 2), nullable=False, default=0, server_default="0")
    total_duration_seconds = Column(Integer, nullable=False, default=0, server_default="0")
    unique_dog_count = Column(Integer, nullable=False, default=0, server_default="0")
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    user = relationship("User", back_populates="volunteer_profile")


class WalkIncident(Timestamps, Base):
    __tablename__ = "walk_incidents"
    __table_args__ = (
        *coordinates(),
        CheckConstraint("(latitude IS NULL) = (longitude IS NULL)", name="ck_incident_coordinate_pair"),
        choices("incident_type", "INJURY LOST_DOG SAFETY_THREAT OTHER POOP_NO_BAG ATE_FOREIGN_OBJECT ABNORMAL_BEHAVIOR"),
        choices("status", "OPEN IN_REVIEW RESOLVED DISMISSED"),
        CheckConstraint("length(description) > 0", name="ck_incident_description"),
        CheckConstraint(
            "(acknowledged_at IS NULL AND acknowledged_by IS NULL) OR (acknowledged_at IS NOT NULL AND acknowledged_by IS NOT NULL)",
            name="ck_incident_acknowledgement",
        ),
        Index("ix_walk_incidents_time", "walk_id", "occurred_at"),
        Index("ix_walk_incidents_reporter", "reported_by"),
        Index("ix_walk_incidents_status", "status", "created_at"),
        Index(
            "uq_walk_incidents_reported_client_report_id",
            "reported_by", "client_report_id",
            unique=True,
            postgresql_where=text("client_report_id IS NOT NULL"),
            sqlite_where=text("client_report_id IS NOT NULL"),
        ),
        Index(
            "ix_walk_incidents_unacknowledged",
            "acknowledged_at",
            postgresql_where=text("acknowledged_at IS NULL"),
            sqlite_where=text("acknowledged_at IS NULL"),
        ),
    )

    id = Column(Integer, primary_key=True)
    walk_id = Column(Integer, reference("walks"), nullable=False)
    reported_by = Column(Integer, reference("users"), nullable=False)
    incident_type = Column(String(30), nullable=False)
    description = Column(Text, nullable=False)
    latitude = Column(Numeric(9, 6), nullable=True)
    longitude = Column(Numeric(9, 6), nullable=True)
    occurred_at = Column(DateTime(timezone=True), nullable=False)
    status = Column(String(20), nullable=False, default="OPEN", server_default="OPEN")
    client_report_id = Column(String(36), nullable=True)
    acknowledged_at = Column(DateTime(timezone=True), nullable=True)
    acknowledged_by = Column(Integer, reference("users"), nullable=True)

    walk = relationship("Walk", back_populates="incidents")
    reporter = relationship("User", foreign_keys=[reported_by], back_populates="walk_incidents")


class ShelterVolunteerReview(Timestamps, Base):
    __tablename__ = "shelter_volunteer_reviews"
    __table_args__ = (
        UniqueConstraint("walk_id", name="uq_shelter_volunteer_reviews_walk"),
        CheckConstraint(
            "rating >= 0.5 AND rating <= 5.0 AND CAST(ROUND(rating * 10) AS INTEGER) % 5 = 0",
            name="ck_shelter_volunteer_review_rating",
        ),
        CheckConstraint(
            "comment IS NULL OR length(comment) <= 500",
            name="ck_shelter_volunteer_review_comment_len",
        ),
        Index("ix_shelter_volunteer_reviews_volunteer", "volunteer_user_id", "created_at"),
        Index("ix_shelter_volunteer_reviews_shelter", "shelter_id", "created_at"),
    )

    id = Column(Integer, primary_key=True)
    walk_id = Column(Integer, reference("walks"), nullable=False)
    shelter_id = Column(Integer, reference("shelters"), nullable=False)
    volunteer_user_id = Column(Integer, reference("users"), nullable=False)
    reviewed_by = Column(Integer, reference("users"), nullable=False)
    rating = Column(Numeric(2, 1), nullable=False)
    comment = Column(String(500), nullable=True)

    walk = relationship("Walk", back_populates="volunteer_review")
    shelter = relationship("Shelter")
    volunteer = relationship("User", foreign_keys=[volunteer_user_id])
    reviewer = relationship("User", foreign_keys=[reviewed_by])
    tags = relationship(
        "ShelterVolunteerReviewTag",
        back_populates="review",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ShelterVolunteerReviewTag.id",
    )


class ShelterVolunteerReviewTag(Base):
    __tablename__ = "shelter_volunteer_review_tags"
    __table_args__ = (
        UniqueConstraint("review_id", "tag_code", name="uq_shelter_volunteer_review_tags_review_code"),
        CheckConstraint("tag_code IN ('PUNCTUALITY', 'SAFETY_CARE', 'CONSIDERATE', 'DISAPPOINTING')", name="ck_shelter_volunteer_review_tag_code"),
        Index("ix_shelter_volunteer_review_tags_review_id", "review_id"),
    )

    id = Column(Integer, primary_key=True)
    review_id = Column(Integer, ForeignKey("shelter_volunteer_reviews.id", ondelete="CASCADE"), nullable=False)
    tag_code = Column(String(30), nullable=False)

    review = relationship("ShelterVolunteerReview", back_populates="tags")


WALK_MODELS = (DogWalkSetting, DogWalkSlot, WalkRequest, Walk, WalkPoint,
               WalkMarker, WalkReview, ShelterVolunteerReview, ShelterVolunteerReviewTag,
               DogTraitSummary, VolunteerProfile, WalkIncident)
