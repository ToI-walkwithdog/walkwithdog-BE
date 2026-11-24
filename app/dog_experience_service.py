"""Onboarding Step 2 dog experience (Issue #42): GET and PUT /users/me/onboarding/dog-experience.

Contract: Issue #42 (2026-11-23 user decision: one dog_size_code instead of the Notion
dog_size_codes array, no size link table). Messages: Notion Step 2 specs.

Save transaction (same rule as the pledge and Step 1 save):
  lock the users row FOR SHARE -> re-check ACTIVE, the current pledge and Step 1 on that
  row -> insert the main row and the challenge rows -> commit. A Step 1 status change
  (FOR UPDATE) and this save wait for each other. Any failure rolls everything back.
Concurrent saves of one account: the primary keys keep one row; the loser re-reads
it and answers 200 for the same answer or 409 for a different one.
"""
from datetime import datetime
import unicodedata

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.account_status import ensure_active, lock_user
from app.api_contract import ContractError
from app.auth import contract_unauthorized
from app.models import (CHALLENGE_CODES, DIFFICULTY_CODES, DOG_SIZE_CODES, WALKING_FREQUENCY_CODES, User,
                        VolunteerExperienceChallenge, VolunteerOnboardingExperience)
from app.onboarding_common import (STEP_BASIC_INFORMATION, STEP_PET_EXPERIENCE, STEP_PLEDGE, as_utc,
                                   first_incomplete_step, previous_step_required)

# Notion proposal, not final (Issue #42 open item); the column is VARCHAR(200).
BREED_MAX_LENGTH = 200
DETAIL_FIELDS = ("experienced_breeds_text", "dog_size_code", "walking_frequency", "perceived_difficulty",
                 "challenge_codes")


def _error(code, message, field=None, status=422):
    return ContractError(status, code, message, {"field": field} if field else {})


def _present(value) -> bool:
    """A detail counts as sent unless it is missing, null, an empty string or an empty list."""
    return not (value is None or (isinstance(value, str) and not value.strip()) or value == [])


def _code(value, allowed, field, required_code, required_message, invalid_code, invalid_message):
    if value is None or (isinstance(value, str) and not value.strip()):
        raise _error(required_code, required_message, field)
    if not isinstance(value, str) or value not in allowed:
        raise _error(invalid_code, invalid_message, field)
    return value


def validate_experience(body) -> dict:
    """Return the normalized answer or raise the first 422 in the Issue #42 order."""
    if not isinstance(body, dict) or not isinstance(body.get("has_experience"), bool):
        raise _error("EXPERIENCE_REQUIRED", "반려견 경험 여부를 선택해 주세요.", "has_experience")
    if not body["has_experience"]:
        if any(_present(body.get(field)) for field in DETAIL_FIELDS):
            raise _error("EXPERIENCE_DETAILS_NOT_ALLOWED", "경험 없음 선택 시 추가 정보는 입력하지 않아야 합니다.")
        return {"has_experience": False, "experienced_breeds_text": None, "dog_size_code": None,
                "walking_frequency": None, "perceived_difficulty": None, "challenge_codes": []}
    breed = body.get("experienced_breeds_text")
    if breed is not None and not isinstance(breed, str):
        raise _error("INVALID_EXPERIENCED_BREED", "견종 입력을 확인해 주세요.", "experienced_breeds_text")
    breed = unicodedata.normalize("NFC", breed).strip() if breed else ""
    if not breed:
        raise _error("EXPERIENCED_BREED_REQUIRED", "경험한 견종을 입력해 주세요.", "experienced_breeds_text")
    if len(breed) > BREED_MAX_LENGTH or not breed.isprintable():
        raise _error("INVALID_EXPERIENCED_BREED", "견종 입력을 확인해 주세요.", "experienced_breeds_text")
    size = _code(body.get("dog_size_code"), DOG_SIZE_CODES, "dog_size_code",
                 "DOG_SIZE_REQUIRED", "반려견 크기를 선택해 주세요.", "INVALID_DOG_SIZE", "반려견 크기 선택을 확인해 주세요.")
    frequency = _code(body.get("walking_frequency"), WALKING_FREQUENCY_CODES, "walking_frequency",
                      "WALKING_FREQUENCY_REQUIRED", "산책 빈도를 선택해 주세요.",
                      "INVALID_WALKING_FREQUENCY", "산책 빈도를 확인해 주세요.")
    difficulty = _code(body.get("perceived_difficulty"), DIFFICULTY_CODES, "perceived_difficulty",
                       "DIFFICULTY_REQUIRED", "산책 난이도를 선택해 주세요.",
                       "INVALID_DIFFICULTY", "산책 난이도를 확인해 주세요.")
    challenges = body.get("challenge_codes")
    challenges = [] if challenges is None else challenges
    invalid_challenge = _error("INVALID_CHALLENGE", "어려웠던 상황을 확인해 주세요.", "challenge_codes")
    if not isinstance(challenges, list) or any(not isinstance(code, str) or code not in CHALLENGE_CODES
                                               for code in challenges):
        raise invalid_challenge
    if len(set(challenges)) != len(challenges):
        raise invalid_challenge
    if "NONE" in challenges and len(challenges) > 1:
        raise _error("CHALLENGE_NONE_CONFLICT", "특별히 없음은 다른 항목과 함께 선택할 수 없습니다.", "challenge_codes")
    return {"has_experience": True, "experienced_breeds_text": breed, "dog_size_code": size,
            "walking_frequency": frequency, "perceived_difficulty": difficulty, "challenge_codes": challenges}


def _stored(db: Session, experience: VolunteerOnboardingExperience) -> dict:
    codes = [row.challenge_code for row in db.query(VolunteerExperienceChallenge)
             .filter(VolunteerExperienceChallenge.user_id == experience.user_id)]
    order = {code: index for index, code in enumerate(CHALLENGE_CODES)}
    return {"has_experience": experience.has_experience, "experienced_breeds_text": experience.experienced_breeds_text,
            "dog_size_code": experience.dog_size_code, "walking_frequency": experience.walking_frequency,
            "perceived_difficulty": experience.perceived_difficulty,
            "challenge_codes": sorted(codes, key=order.get),
            "completed_at": as_utc(experience.completed_at).isoformat()}


def _next_step(db: Session, user_id: int) -> str | None:
    """The shared first incomplete step (#45) instead of a fixed WALKING_ROUTINE."""
    user = db.get(User, user_id)
    if user is None:  # deleted by a concurrent sign-up exit
        raise contract_unauthorized("USER_NOT_FOUND")
    return first_incomplete_step(db, user)


def read_experience(db: Session, user_id: int) -> dict:
    """Read only: no write."""
    experience = db.get(VolunteerOnboardingExperience, user_id)
    if experience is None:
        return {"step2_completed": False, "experience": None, "next_onboarding_step": STEP_PET_EXPERIENCE}
    return {"step2_completed": True, "experience": _stored(db, experience), "next_onboarding_step": _next_step(db, user_id)}


def _same(stored: dict, answer: dict) -> bool:
    keys = ("has_experience", "experienced_breeds_text", "dog_size_code", "walking_frequency", "perceived_difficulty")
    return (all(stored[key] == answer[key] for key in keys)
            and set(stored["challenge_codes"]) == set(answer["challenge_codes"]))


def _replay(db: Session, experience: VolunteerOnboardingExperience, answer: dict) -> dict:
    stored = _stored(db, experience)
    if not _same(stored, answer):
        raise ContractError(409, "DOG_EXPERIENCE_ALREADY_COMPLETED", "반려견 경험 입력이 이미 완료되었습니다.")
    return {"step2_completed": True, "completed_at": stored["completed_at"],
            "next_onboarding_step": _next_step(db, experience.user_id)}


def save_experience(db: Session, user_id: int, answer: dict, now: datetime) -> dict:
    existing = db.get(VolunteerOnboardingExperience, user_id)
    if existing is not None:
        return _replay(db, existing, answer)
    try:
        user = lock_user(db, user_id)  # FOR SHARE, then the current state of the locked row
        if user is None:
            raise contract_unauthorized("USER_NOT_FOUND")
        ensure_active(user)
        step = first_incomplete_step(db, user)
        if step in (STEP_PLEDGE, STEP_BASIC_INFORMATION):
            raise previous_step_required(step)
        experience = VolunteerOnboardingExperience(
            user_id=user_id, has_experience=answer["has_experience"],
            experienced_breeds_text=answer["experienced_breeds_text"], dog_size_code=answer["dog_size_code"],
            walking_frequency=answer["walking_frequency"], perceived_difficulty=answer["perceived_difficulty"],
            completed_at=now, updated_at=now)
        db.add(experience)
        db.flush()
        db.add_all(VolunteerExperienceChallenge(user_id=user_id, challenge_code=code)
                   for code in answer["challenge_codes"])
        next_step = first_incomplete_step(db, user)  # sees the flushed Step 2 row
        db.commit()
        return {"step2_completed": True, "completed_at": as_utc(now).isoformat(), "next_onboarding_step": next_step}
    except IntegrityError:
        db.rollback()
        # A concurrent save of this account committed first (primary key).
        existing = db.get(VolunteerOnboardingExperience, user_id)
        if existing is None:
            raise
        return _replay(db, existing, answer)
    except Exception:
        db.rollback()
        raise
