"""My page walk preferences on /10-3 (Issue #105). Responses use {code, data, message} and are
never cached. Both routes: Bearer session (401), then role=user and ACTIVE (403); no pledge
or onboarding completion is required, and saving never changes onboarding status.
"""
from fastapi import APIRouter, Body, Depends
from fastapi.exceptions import RequestValidationError
from sqlalchemy.orm import Session

from app.api_contract import ContractError, ContractErrorResponse, ContractRoute, ok
from app.auth import get_contract_user
from app.database import get_db
from app.models import User
from app.onboarding_common import get_clock
from app.walk_preference_service import invalid_payload, read_preferences, require_preference_user, update_preferences


class PreferenceRoute(ContractRoute):
    def validation_error(self, exc: RequestValidationError) -> ContractError:
        if "PATCH" in self.methods:  # PATCH reads a plain object; any unreadable body is one code
            return invalid_payload()
        return super().validation_error(exc)


router = APIRouter(prefix="/users/me/walk-preferences", tags=["walk-preferences"], route_class=PreferenceRoute)

NO_STORE = "private, no-store"
COMMON_ERRORS = {
    401: {"model": ContractErrorResponse, "description": "AUTHENTICATION_REQUIRED, INVALID_ACCESS_TOKEN, SESSION_EXPIRED"},
    403: {"model": ContractErrorResponse, "description": "PREFERENCE_NOT_ALLOWED(일반 사용자 외), AGE_REVIEW_REQUIRED"},
    500: {"model": ContractErrorResponse, "description": "INTERNAL_SERVER_ERROR(DB 잠금 시간 초과, 교착, 직렬화 실패 포함)"},
}
PATCH_BODY = Body(None, examples=[{"expected_version": 0, "duration_code": "UP_TO_30_MIN", "size_code": "SMALL"}],
                  description="expected_version 필수. duration_code, size_code 중 하나 이상. 보내지 않은 필드는 "
                              "변경 없음, null과 추가 필드(region_code 포함)는 422")


def get_preference_user(user: User = Depends(get_contract_user)) -> User:
    """Role and status (403) as a dependency, so they are answered before body validation (422)."""
    require_preference_user(user)
    return user


def no_store(response):
    response.headers["Cache-Control"] = NO_STORE
    return response


@router.get("", responses=COMMON_ERRORS)
def get_walk_preferences(user: User = Depends(get_preference_user), db: Session = Depends(get_db)):
    """저장된 선호(preferences), 필드별 적용값(effective_preferences: 저장값, 완료된 Step 3 값, null 순)과
    출처(sources: PREFERENCE, ONBOARDING, NONE), version. 미설정이면 configured=false, version=0."""
    return no_store(ok(read_preferences(db, user), "선호 설정을 조회했습니다."))


@router.patch("", responses={**COMMON_ERRORS,
                             409: {"model": ContractErrorResponse,
                                   "description": "PREFERENCE_VERSION_MISMATCH(값이 같아도 버전이 다르면)"},
                             422: {"model": ContractErrorResponse,
                                   "description": "INVALID_PREFERENCE_PAYLOAD, INVALID_DURATION, INVALID_DOG_SIZE"}})
def patch_walk_preferences(payload: dict | None = PATCH_BODY, user: User = Depends(get_preference_user),
                           db: Session = Depends(get_db), clock=Depends(get_clock)):
    return no_store(ok(update_preferences(db, user.id, payload, clock()), "선호 설정이 저장되었습니다."))
