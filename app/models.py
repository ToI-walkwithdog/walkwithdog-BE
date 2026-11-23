import enum

from sqlalchemy import (BigInteger, CheckConstraint, Column, Integer, String, Text, DateTime, ForeignKey, JSON,
                        UniqueConstraint, Boolean, Enum, Date, Time, Index)
from sqlalchemy.sql import func
from app.database import Base
from sqlalchemy.orm import relationship


class UserRole(str, enum.Enum):
    USER = "user"
    SHELTER = "shelter"
    ADMIN = "admin"

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    # Sign-up stores login_id lowercased and nickname NFC-trimmed. Email is not collected.
    # Case-insensitive uniqueness comes from the lower() indexes below the class.
    login_id = Column(String(20), nullable=False)
    nickname = Column(String(20), nullable=False)
    password = Column(String(255), nullable=False)
    role = Column(Enum(UserRole), default=UserRole.USER, server_default="USER", nullable=False)
    # Existing accounts remain unassigned until an administrator chooses a shelter.
    shelter_id = Column(Integer, ForeignKey("shelters.id", name="fk_users_shelter_id"), nullable=True, index=True)
    phone = Column(String(50), nullable=True)
    # Sign-up self-declaration "만 14세 이상" (Issue #31), server time. Not an age check:
    # the birth date is validated later in onboarding Step 1. NULL for accounts made
    # before #31; never back-filled.
    age_14_plus_confirmed_at = Column(DateTime(timezone=True), nullable=True)
    # F-14: Timestamp when the user tapped 'confirmed' on the last slide of the first-walk guide.
    first_walk_guide_completed_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    walk_requests = relationship("WalkRequest", foreign_keys="WalkRequest.user_id", back_populates="user", passive_deletes="all")
    reviewed_walk_requests = relationship("WalkRequest", foreign_keys="WalkRequest.reviewed_by", back_populates="reviewer", passive_deletes="all")
    volunteer_profile = relationship("VolunteerProfile", back_populates="user", uselist=False, passive_deletes="all")
    walk_incidents = relationship("WalkIncident", foreign_keys="WalkIncident.reported_by", back_populates="reporter", passive_deletes="all")


# app.signup_service maps these names to the conflicting field (migrations 016, 017).
Index("uq_users_login_id_lower", func.lower(User.login_id), unique=True)
Index("uq_users_nickname_lower", func.lower(User.nickname), unique=True)


class UserConsent(Base):
    """Consent history (Issue #31, migration 018). Sign-up writes AGREE rows only for
    active items the user agreed to, in the same transaction as the account."""
    __tablename__ = "user_consents"
    __table_args__ = (
        CheckConstraint("consent_type IN ('PRIVACY_COLLECTION_USE', 'AI_OVERSEAS_TRANSFER')",
                        name="ck_user_consents_consent_type"),
        CheckConstraint("action IN ('AGREE', 'WITHDRAW')", name="ck_user_consents_action"),
        Index("ix_user_consents_user_type_recorded", "user_id", "consent_type", "recorded_at"),
    )

    # BIGINT on PostgreSQL; SQLite only autoincrements INTEGER primary keys.
    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", name="fk_user_consents_user_id"), nullable=False)
    consent_type = Column(String(40), nullable=False)
    document_version = Column(String(32), nullable=False)
    action = Column(String(16), nullable=False)
    recorded_at = Column(DateTime(timezone=True), nullable=False)


class UserPledgeAcknowledgement(Base):
    """First walk pledge confirmations (Issue #33, migration 019). One row per user and
    pledge version keeps the first acknowledged_at; rows for older versions are kept."""
    __tablename__ = "user_pledge_acknowledgements"
    __table_args__ = (
        UniqueConstraint("user_id", "pledge_version", name="uq_user_pledge_ack_user_version"),
    )

    # BIGINT on PostgreSQL; SQLite only autoincrements INTEGER primary keys.
    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", name="fk_user_pledge_ack_user_id"), nullable=False)
    pledge_version = Column(String(32), nullable=False)
    acknowledged_at = Column(DateTime(timezone=True), nullable=False)


class SessionToken(Base):
    # Legacy schema retained for compatibility; new login tokens use AuthSession.
    __tablename__ = "session_tokens"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    token = Column(String(255), unique=True, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
class AuthSession(Base):
    __tablename__ = "auth_sessions"
    token_hash = Column(String(64), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)


class Shelter(Base):
    __tablename__ = "shelters"

    id = Column(Integer, primary_key=True, index=True)
    desertion_no = Column(String(50), unique=True, nullable=True)
    name = Column(String(100), nullable=False)
    phone = Column(String(50), nullable=True)
    address = Column(String(255), nullable=True)
    website_url = Column(String(255), nullable=True)
    external_code = Column(String(50), nullable=True, unique=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    dogs = relationship("Dog", back_populates="shelter")
    walk_requests = relationship("WalkRequest", back_populates="shelter", passive_deletes="all")

class Dog(Base):
    __tablename__ = "dogs"

    id = Column(Integer, primary_key=True, index=True)
    shelter_id = Column(Integer, ForeignKey("shelters.id"), nullable=True)
    desertion_no = Column(String(20), unique=True, nullable=True)  # ← 추가

    name = Column(String(50), nullable=False)
    breed = Column(String(50), nullable=True)
    age = Column(Integer, nullable=True)
    gender = Column(String(20), nullable=True)
    image_url = Column(String(255), nullable=True)

    description = Column(Text, nullable=True)
    personality_keywords = Column(Text, nullable=True)

    energy_level = Column(Integer, nullable=True)
    barking_level = Column(Integer, nullable=True)
    independence_level = Column(Integer, nullable=True)
    sociability_level = Column(Integer, nullable=True)
    walk_need = Column(Integer, nullable=True)
    weight = Column(String(50), nullable=True)
    special_note = Column(Text, nullable=True)
    separation_anxiety_level = Column(Integer, nullable=True)
    training_level = Column(Integer, nullable=True)

    trait_source = Column(String(30), nullable=True, default="breed_default")
    external_data = Column(JSON, nullable=True)
    synced_at = Column(DateTime(timezone=True), nullable=True)
    process_state = Column(String(50), nullable=True)
    neuter_status = Column(String(10), nullable=True)

    good_with_children = Column(String(20), nullable=True)
    good_with_other_dogs = Column(String(20), nullable=True)
    separation_anxiety = Column(String(20), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    matching_results = relationship("MatchingResult", back_populates="dog")
    shelter = relationship("Shelter", back_populates="dogs")
    walk_setting = relationship("DogWalkSetting", back_populates="dog", uselist=False, passive_deletes="all")
    walk_requests = relationship("WalkRequest", back_populates="dog", passive_deletes="all")
    trait_summary = relationship("DogTraitSummary", back_populates="dog", uselist=False, passive_deletes="all")

class DogWalkDateSlot(Base):
    __tablename__ = "dog_walk_date_slots"
    
    id = Column(Integer, primary_key=True, index=True)
    dog_id = Column(Integer, ForeignKey("dogs.id", ondelete="CASCADE"), nullable=False)
    slot_date = Column(Date, nullable=False)
    start_time = Column(Time, nullable=False)
    end_time = Column(Time, nullable=False)
    duration_minutes = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
    dog = relationship("Dog", backref="date_slots")

class Survey(Base):
    __tablename__ = "surveys"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    housing_type = Column(String(50), nullable=True)
    family_type = Column(String(50), nullable=True)
    outing_frequency = Column(String(50), nullable=True)
    daily_walk_time = Column(String(50), nullable=True)

    life_pattern = Column(String(50), nullable=True)
    walk_frequency = Column(String(50), nullable=True)
    walk_duration = Column(String(50), nullable=True)

    dog_experience = Column(String(50), nullable=True)
    mbti = Column(String(10), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())


class MatchingResult(Base):
    __tablename__ = "matching_results"
    __table_args__ = (UniqueConstraint("survey_id", "dog_id", name="uq_matching_survey_dog"),)

    id = Column(Integer, primary_key=True, index=True)

    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    dog_id = Column(Integer, ForeignKey("dogs.id"), nullable=False)
    survey_id = Column(Integer, ForeignKey("surveys.id"), nullable=True)
    # Preserve explanations even after the user submits another survey.
    analysis_snapshot = Column(JSON, nullable=True)

    score = Column(Integer, nullable=False)
    reason = Column(Text, nullable=True)
    detail_analysis = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    dog = relationship("Dog", back_populates="matching_results")

class ChatSession(Base):
    __tablename__ = "chat_sessions"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    dog_id = Column(Integer, ForeignKey("dogs.id"), nullable=False)
    survey_id = Column(Integer, ForeignKey("surveys.id"), nullable=False)
    context_snapshot = Column(JSON, nullable=False)
    consent_to_external_ai = Column(Boolean, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    id = Column(Integer, primary_key=True)
    session_id = Column(Integer, ForeignKey("chat_sessions.id"), nullable=False, index=True)
    role = Column(String(10), nullable=False)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class BreedTrait(Base):
    __tablename__ = "breed_traits"

    id = Column(Integer, primary_key=True, index=True)

    breed_name = Column(String(100), unique=True, nullable=False)

    energy_level = Column(Integer, nullable=True)
    barking_level = Column(Integer, nullable=True)
    independence_level = Column(Integer, nullable=True)
    sociability_level = Column(Integer, nullable=True)
    walk_need = Column(Integer, nullable=True)

    good_with_children = Column(String(20), nullable=True)
    good_with_other_dogs = Column(String(20), nullable=True)
    separation_anxiety = Column(String(20), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())


# Register walk tables whenever the existing models entrypoint is imported.
from app.walk_models import (  # noqa: E402,F401
    DogWalkSetting, DogWalkSlot, WalkRequest, Walk, WalkPoint, WalkMarker,
    WalkReview, WalkReviewTag, ShelterVolunteerReview, ShelterVolunteerReviewTag,
    DogTraitSummary, VolunteerProfile, WalkIncident,
)
