"""Shelter start code issue and verification (Issue #87). Responses use {code, data, message}."""
from fastapi import APIRouter, Body, Depends, Path
from fastapi.exceptions import RequestValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import walk_request_service
from app.api_contract import ContractError, ContractErrorResponse, ContractRoute, contract_response
from app.auth import get_contract_user
from app.database import get_db
from app.models import User
from app.shelter_code_service import (invalid_format, invalid_request_id, issue_code, retryable, verify_code)

NO_STORE = {"Cache-Control": "no-store"}
RETRYABLE_CODES = ("55P03", "40P01", "40001")


class ShelterCodeRoute(ContractRoute):
    def validation_error(self, exc: RequestValidationError) -> ContractError:
        location = exc.errors()[0].get("loc", ())
        if location[:1] == ("path",):
            return invalid_request_id()
        if location[:1] == ("body",):
            return invalid_format()
        return super().validation_error(exc)


router = APIRouter(tags=["walk-requests"], route_class=ShelterCodeRoute)


# The body is read as a plain object so role and account status (403) are answered before the
# code format (422); the service validates `shelter_code` as exactly six ASCII digits.
VERIFY_BODY = Body(None, examples=[{"shelter_code": "012345"}],
                   description='{"shelter_code": "6자리 숫자 문자열"}. 앞자리 0을 유지한다.')


def common_errors(extra):
    return {
        401: {"model": ContractErrorResponse, "description": "AUTHENTICATION_REQUIRED, INVALID_ACCESS_TOKEN, "
                                                            "SESSION_EXPIRED, USER_NOT_FOUND"},
        403: {"model": ContractErrorResponse, "description": "WALK_REQUEST_NOT_ALLOWED, AGE_REVIEW_REQUIRED"},
        404: {"model": ContractErrorResponse, "description": "WALK_REQUEST_NOT_FOUND"},
        409: {"model": ContractErrorResponse, "description": "WALK_REQUEST_NOT_ELIGIBLE(승인 아님, 예약 시간대 밖, "
                                                            "산책 기록 있음), WALK_REQUEST_RETRYABLE"},
        500: {"model": ContractErrorResponse, "description": "INTERNAL_SERVER_ERROR"},
        **extra,
    }


def write(db, operation):
    """Commit the operation; DB lock conflicts and a lost race for the active issuance retry."""
    try:
        result = operation()
        db.commit()
        return result
    except SQLAlchemyError as exc:
        db.rollback()
        original = getattr(exc, "orig", None)
        code = getattr(original, "pgcode", None)
        constraint = getattr(getattr(original, "diag", None), "constraint_name", None)
        if code in RETRYABLE_CODES or constraint == "uq_shelter_code_issuances_active":
            raise retryable() from None
        raise
    except Exception:
        db.rollback()
        raise


@router.post("/walk-requests/{request_id}/shelter-code", status_code=201,
             responses=common_errors({422: {"model": ContractErrorResponse, "description": "INVALID_WALK_REQUEST_ID"}}))
def issue(request_id: int = Path(gt=0), actor: User = Depends(get_contract_user), db: Session = Depends(get_db)):
    """보호소 담당자(해당 예약 보호소)와 관리자. 원문 코드는 이 응답에만 포함된다."""
    data = write(db, lambda: issue_code(db, actor, request_id, walk_request_service.utc_now()))
    return contract_response(201, "SHELTER_CODE_ISSUED", "보호소 코드를 발급했어요.", data, NO_STORE)


@router.post("/walk-requests/{request_id}/shelter-code/verify",
             responses=common_errors({
                 422: {"model": ContractErrorResponse,
                       "description": "INVALID_WALK_REQUEST_ID, INVALID_SHELTER_CODE_FORMAT, "
                                      "SHELTER_CODE_INVALID(불일치, 미발급, 만료)"},
                 429: {"model": ContractErrorResponse,
                       "description": "SHELTER_CODE_RATE_LIMITED. data.retry_after_seconds와 Retry-After"}}))
def verify(request_id: int = Path(gt=0), payload: dict | None = VERIFY_BODY,
           actor: User = Depends(get_contract_user), db: Session = Depends(get_db)):
    """예약 본인. 실패 횟수는 커밋한 뒤 422 또는 429로 응답한다."""
    raw_code = (payload or {}).get("shelter_code")
    outcome = write(db, lambda: verify_code(db, actor, request_id, raw_code, walk_request_service.utc_now()))
    if outcome.error is not None:
        raise outcome.error
    return contract_response(200, "SHELTER_CODE_VERIFIED", "보호소 코드가 확인됐어요.", outcome.data, NO_STORE)
