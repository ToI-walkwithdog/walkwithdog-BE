"""Accounts in AGE_REVIEW_REQUIRED (Issue #41): birth date correction and sign-up exit.

Both run in one transaction that first locks the users row FOR UPDATE and re-checks
the status, the same lock as the Step 1 status change and conflicting with the
pledge's and Step 1 save's FOR SHARE (app.account_status). So a correction and an
exit for the same account never both succeed: the second sees the new state.

Sign-up exit deletes explicitly in foreign key order (every FK to users is NO
ACTION; no CASCADE is added):
  auth_sessions, session_tokens, phone_verifications, the account's USER daily
  counter row, volunteer_onboarding_basics, user_pledge_acknowledgements,
  user_consents, then users.
It refuses with 409 ACCOUNT_HAS_RETAINED_ACTIVITY and deletes nothing when
  - activity records exist (walk requests and reviews of them, incidents, chat,
    surveys, matching results, volunteer profile), or
  - consent or pledge records exist and the record policy is not "delete".

Consent and pledge records: whether the law requires keeping them, and for how long,
is not decided (Issues #31, #41). Their deletion only runs when
ACCOUNT_EXIT_RECORD_POLICY=delete AND APP_ENV is local, development or test. Any
other setting, including production, keeps them and answers 409. Enabling it in
production needs the policy decision and a code change here.

Number counters (PHONE scope) are kept: they hold only an HMAC of a number, limit
abuse per number, and are removed by the daily purge after two days.
"""
from datetime import date, datetime
import logging
import os

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.account_status import ACCOUNT_ACTIVE, ACCOUNT_AGE_REVIEW_REQUIRED, lock_user
from app.api_contract import ContractError
from app.auth import contract_unauthorized, verify_password
from app.models import (AuthSession, ChatSession, MatchingResult, PhoneVerification, PhoneVerificationDailyCount,
                        SessionToken, Survey, User, UserConsent, UserPledgeAcknowledgement, VolunteerOnboardingBasic)
from app.onboarding_common import age_years, first_incomplete_step, kst_date, step1_completed
from app.walk_models import VolunteerProfile, WalkIncident, WalkRequest

logger = logging.getLogger(__name__)

MINIMUM_AGE, MAXIMUM_AGE = 14, 120
RECORD_POLICY_DELETE = "delete"
RECORD_DELETION_ENVIRONMENTS = {"local", "development", "test"}


def correction_not_required() -> ContractError:
    return ContractError(409, "AGE_REVIEW_NOT_REQUIRED", "연령 확인이 필요한 계정이 아닙니다.")


def exit_not_required() -> ContractError:
    return ContractError(409, "AGE_REVIEW_NOT_REQUIRED", "현재 가입 종료 절차의 대상이 아닙니다.")


def retained_activity() -> ContractError:
    return ContractError(409, "ACCOUNT_HAS_RETAINED_ACTIVITY", "계정 정리를 위해 추가 확인이 필요합니다.")


def check_corrected_birth_date(birth: date, now: datetime) -> None:
    """422 rules of the correction. A refused date changes nothing and is not stored."""
    today = kst_date(now)
    if birth > today:
        raise ContractError(422, "INVALID_BIRTH_DATE", "생년월일을 확인해 주세요.", {"field": "birth_date"})
    age = age_years(birth, today)
    if age < MINIMUM_AGE:
        raise ContractError(422, "AGE_BELOW_MINIMUM", "만 14세 이상부터 이용할 수 있습니다.",
                            {"field": "birth_date", "minimum_age": MINIMUM_AGE})
    if age > MAXIMUM_AGE:
        raise ContractError(422, "AGE_ABOVE_MAXIMUM", "생년월일을 다시 확인해 주세요.",
                            {"field": "birth_date", "maximum_age": MAXIMUM_AGE})


def correct_birth_date(db: Session, user_id: int, birth: date, now: datetime) -> dict:
    """Lift AGE_REVIEW_REQUIRED. The birth date is only checked, never stored."""
    check_corrected_birth_date(birth, now)
    try:
        user = lock_user(db, user_id, for_update=True)
        if user is None:  # deleted by a concurrent sign-up exit
            raise contract_unauthorized("USER_NOT_FOUND")
        if user.account_status != ACCOUNT_AGE_REVIEW_REQUIRED or step1_completed(db, user_id):
            raise correction_not_required()
        user.account_status = ACCOUNT_ACTIVE
        user.age_review_required_at = None
        db.commit()
    except Exception:
        db.rollback()
        raise
    user = db.get(User, user_id)
    # PLEDGE if the pledge version changed meanwhile, otherwise BASIC_INFORMATION.
    return {"account_status": user.account_status, "age_review_required": False,
            "step1_completed": step1_completed(db, user_id),
            "next_onboarding_step": first_incomplete_step(db, user)}


def record_deletion_enabled() -> bool:
    policy = (os.getenv("ACCOUNT_EXIT_RECORD_POLICY") or "").strip().lower()
    environment = (os.getenv("APP_ENV") or "").strip().lower()
    return policy == RECORD_POLICY_DELETE and environment in RECORD_DELETION_ENVIRONMENTS


def _exists(db: Session, model, *conditions) -> bool:
    return db.query(model).filter(*conditions).first() is not None


def has_retained_activity(db: Session, user_id: int) -> bool:
    """Activity this API never deletes (Issue #41 base list; final list to be confirmed)."""
    return any((
        _exists(db, WalkRequest, or_(WalkRequest.user_id == user_id, WalkRequest.reviewed_by == user_id)),
        _exists(db, WalkIncident, or_(WalkIncident.reported_by == user_id, WalkIncident.acknowledged_by == user_id)),
        _exists(db, ChatSession, ChatSession.user_id == user_id),
        _exists(db, MatchingResult, MatchingResult.user_id == user_id),
        _exists(db, Survey, Survey.user_id == user_id),
        _exists(db, VolunteerProfile, VolunteerProfile.user_id == user_id),
    ))


def has_policy_pending_records(db: Session, user_id: int) -> bool:
    return (_exists(db, UserConsent, UserConsent.user_id == user_id)
            or _exists(db, UserPledgeAcknowledgement, UserPledgeAcknowledgement.user_id == user_id))


def delete_account(db: Session, user_id: int) -> None:
    """One transaction. Any error rolls everything back; the account and sessions stay."""
    try:
        user = lock_user(db, user_id, for_update=True)
        if user is None:
            raise contract_unauthorized("USER_NOT_FOUND")
        if user.account_status != ACCOUNT_AGE_REVIEW_REQUIRED:
            raise exit_not_required()
        # Both refusals share the contract code (Notion). The log keeps them apart for
        # operations; it names only the reason, never the account.
        if has_retained_activity(db, user_id):
            logger.info("account exit refused: activity records")
            raise retained_activity()
        if has_policy_pending_records(db, user_id) and not record_deletion_enabled():
            logger.info("account exit refused: consent or pledge retention policy pending")
            raise retained_activity()
        # Rows of other tables first, in FK order; users last. Sessions are deleted
        # after the users lock, so a login racing this request either committed its
        # session before (deleted here) or waits for the lock and then fails the FK.
        for model, condition in (
            (AuthSession, AuthSession.user_id == user_id),
            (SessionToken, SessionToken.user_id == user_id),
            (PhoneVerification, PhoneVerification.user_id == user_id),
            (PhoneVerificationDailyCount, (PhoneVerificationDailyCount.scope == "USER")
             & (PhoneVerificationDailyCount.subject == str(user_id))),
            (VolunteerOnboardingBasic, VolunteerOnboardingBasic.user_id == user_id),
            (UserPledgeAcknowledgement, UserPledgeAcknowledgement.user_id == user_id),
            (UserConsent, UserConsent.user_id == user_id),
            (User, User.id == user_id),
        ):
            db.query(model).filter(condition).delete(synchronize_session=False)
        db.commit()
    except Exception:
        db.rollback()
        raise


def password_confirmed(user: User, password: str) -> bool:
    """Reuses the scrypt check. Accounts without a usable hash can never confirm."""
    return verify_password(password, user.password)
