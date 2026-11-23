"""Region lists for walk area selection (Issue #43). Public: no authentication, read only.

Responses use the onboarding contract {code, data, message}. Cache-Control: no-store keeps
clients from showing a region after the master deactivated it (provisional choice; the
cache policy is an Issue #43 open item).
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api_contract import ContractError, ContractErrorResponse, ContractRoute, ok
from app.database import get_db
from app.region_service import SIDO_CODE_PATTERN, list_sido, list_sigungu

router = APIRouter(prefix="/regions", tags=["regions"], route_class=ContractRoute)

SERVER_ERRORS = {
    500: {"model": ContractErrorResponse, "description": "INTERNAL_SERVER_ERROR"},
    503: {"model": ContractErrorResponse, "description": "REGION_DATA_UNAVAILABLE(지역 마스터 미적재 또는 비정상)"},
}


def _no_store(response):
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/sido", responses=SERVER_ERRORS)
def get_sido(db: Session = Depends(get_db)):
    return _no_store(ok({"regions": list_sido(db)}, "시도 목록을 조회했습니다."))


@router.get("/sigungu", responses={
    **SERVER_ERRORS,
    404: {"model": ContractErrorResponse, "description": "REGION_NOT_FOUND(없거나 선택할 수 없는 시도)"},
    422: {"model": ContractErrorResponse, "description": "SIDO_CODE_REQUIRED, INVALID_SIDO_CODE"}})
def get_sigungu(sido_code: str | None = Query(None, description="시도 코드 2자리, 예: 11"),
                db: Session = Depends(get_db)):
    if sido_code is None or not sido_code.strip():
        raise ContractError(422, "SIDO_CODE_REQUIRED", "시도를 선택해 주세요.", {"field": "sido_code"})
    if SIDO_CODE_PATTERN.fullmatch(sido_code) is None:
        raise ContractError(422, "INVALID_SIDO_CODE", "시도 코드를 확인해 주세요.", {"field": "sido_code"})
    regions = list_sigungu(db, sido_code)
    return _no_store(ok({"sido_code": sido_code, "regions": regions}, "시군구 목록을 조회했습니다."))
