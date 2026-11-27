"""Shelter on-site start codes (Issue #87, migration 030_shelter_code_verification.sql).

Kept out of app.walk_models so the walk model registry (WALK_MODELS) does not change.
app.models imports this module so Base.metadata always knows both tables.

- ShelterCodeIssuance: one row per issued code. Only an HMAC of the code is stored. At
  most one non-revoked issuance per reservation; re-issuing revokes the previous one.
  The verification and its use by POST /walks are columns of the same row.
- ShelterCodeAttempt: consecutive failures per (reservation, volunteer), kept apart from
  the issuance so re-issuing a code never lifts a rate limit.
"""
from sqlalchemy import (BigInteger, CheckConstraint, Column, DateTime, ForeignKey, Index, Integer, SmallInteger,
                        String, text)

from app.database import Base

FAILURE_LIMIT = 5


class ShelterCodeIssuance(Base):
    __tablename__ = "shelter_code_issuances"
    __table_args__ = (
        CheckConstraint("expires_at > issued_at", name="ck_shelter_code_expiry"),
        CheckConstraint("length(code_digest) = 64", name="ck_shelter_code_digest"),
        CheckConstraint("used_at IS NULL OR verified_at IS NOT NULL", name="ck_shelter_code_used_after_verified"),
        CheckConstraint("revoked_at IS NULL OR used_at IS NULL", name="ck_shelter_code_used_not_revoked"),
        Index("uq_shelter_code_issuances_active", "walk_request_id", unique=True,
              postgresql_where=text("revoked_at IS NULL"), sqlite_where=text("revoked_at IS NULL")),
    )

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    walk_request_id = Column(Integer, ForeignKey("walk_requests.id", name="fk_shelter_code_issuances_request",
                                                 ondelete="NO ACTION"), nullable=False)
    volunteer_user_id = Column(Integer, ForeignKey("users.id", name="fk_shelter_code_issuances_volunteer",
                                                   ondelete="NO ACTION"), nullable=False)
    issued_by = Column(Integer, ForeignKey("users.id", name="fk_shelter_code_issuances_issuer",
                                           ondelete="NO ACTION"), nullable=False)
    code_digest = Column(String(64), nullable=False)
    issued_at = Column(DateTime(timezone=True), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    verified_at = Column(DateTime(timezone=True), nullable=True)
    used_at = Column(DateTime(timezone=True), nullable=True)


class ShelterCodeAttempt(Base):
    __tablename__ = "shelter_code_attempts"
    __table_args__ = (
        CheckConstraint(f"failure_count >= 0 AND failure_count <= {FAILURE_LIMIT}", name="ck_shelter_code_failures"),
    )

    walk_request_id = Column(Integer, ForeignKey("walk_requests.id", name="fk_shelter_code_attempts_request",
                                                 ondelete="NO ACTION"), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", name="fk_shelter_code_attempts_user", ondelete="NO ACTION"),
                     primary_key=True)
    failure_count = Column(SmallInteger, nullable=False, default=0)
    locked_until = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False)
