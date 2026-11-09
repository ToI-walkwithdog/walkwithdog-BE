"""Walk persistence only. Authorization, transitions and aggregation belong to later issues."""

from sqlalchemy import (
    Boolean, CheckConstraint, Column, DateTime, ForeignKey, Index, Integer,
    JSON, Numeric, String, Text, Time, UniqueConstraint, text,
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
        choices("difficulty", "BEGINNER EXPERIENCED"),
        CheckConstraint("recommended_duration_minutes > 0", name="ck_recommended_duration"),
    )

    id = Column(Integer, primary_key=True)
    dog_id = Column(Integer, reference("dogs"), nullable=False)
    walkable = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    difficulty = Column(String(20), nullable=False, default="BEGINNER", server_default="BEGINNER")
    precautions = Column(Text, nullable=True)
    recommended_duration_minutes = Column(Integer, nullable=True)

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

    walk_request = relationship("WalkRequest", back_populates="walk")
    points = relationship("WalkPoint", back_populates="walk", passive_deletes="all", order_by="WalkPoint.sequence_no")
    markers = relationship("WalkMarker", back_populates="walk", passive_deletes="all")
    review = relationship("WalkReview", back_populates="walk", uselist=False, passive_deletes="all")
    incidents = relationship("WalkIncident", back_populates="walk", passive_deletes="all")


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
        choices("marker_type", "SNIFF POOP DOG_ENCOUNTER PERSON_ENCOUNTER BARKING"),
        Index("ix_walk_markers_time", "walk_id", "recorded_at"),
    )

    id = Column(Integer, primary_key=True)
    walk_id = Column(Integer, reference("walks"), nullable=False)
    marker_type = Column(String(30), nullable=False)
    latitude = Column(Numeric(9, 6), nullable=False)
    longitude = Column(Numeric(9, 6), nullable=False)
    recorded_at = Column(DateTime(timezone=True), nullable=False)
    memo = Column(Text, nullable=True)

    walk = relationship("Walk", back_populates="markers")


REVIEW_METRICS = ("leash_pulling", "handling_difficulty", "sociability_with_people", "sociability_with_dogs", "barking_level")
TRAIT_METRICS = ("activity_level", "walk_need", "exploration_level", "sociability_level", "barking_level", "handling_difficulty")


class WalkReview(Timestamps, Base):
    __tablename__ = "walk_reviews"
    __table_args__ = (
        UniqueConstraint("walk_id", name="uq_walk_reviews_walk"),
        *(CheckConstraint(f"{metric} BETWEEN 1 AND 5", name=f"ck_{metric}") for metric in REVIEW_METRICS),
        CheckConstraint(" OR ".join(f"{metric} IS NOT NULL" for metric in REVIEW_METRICS), name="ck_review_observation"),
    )

    id = Column(Integer, primary_key=True)
    walk_id = Column(Integer, reference("walks"), nullable=False)
    # Author is derived from walk -> request -> user; cannot disagree with the volunteer.
    leash_pulling = Column(Integer, nullable=True)
    handling_difficulty = Column(Integer, nullable=True)
    sociability_with_people = Column(Integer, nullable=True)
    sociability_with_dogs = Column(Integer, nullable=True)
    barking_level = Column(Integer, nullable=True)
    memo = Column(Text, nullable=True)

    walk = relationship("Walk", back_populates="review")


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
        choices("incident_type", "INJURY LOST_DOG SAFETY_THREAT OTHER"),
        choices("status", "OPEN IN_REVIEW RESOLVED DISMISSED"),
        CheckConstraint("length(description) > 0", name="ck_incident_description"),
        Index("ix_walk_incidents_time", "walk_id", "occurred_at"),
        Index("ix_walk_incidents_reporter", "reported_by"),
        Index("ix_walk_incidents_status", "status", "created_at"),
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

    walk = relationship("Walk", back_populates="incidents")
    reporter = relationship("User", back_populates="walk_incidents")


WALK_MODELS = (DogWalkSetting, DogWalkSlot, WalkRequest, Walk, WalkPoint,
               WalkMarker, WalkReview, DogTraitSummary, VolunteerProfile, WalkIncident)
