"""Read-only public region lists (#43) and authenticated location resolution (#46).

Responses use the onboarding contract {code, data, message}. Cache-Control: no-store keeps
clients from showing a region after the master deactivated it (provisional choice; the
cache policy is an Issue #43 open item).
"""
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.account_status import is_active
from app.api_contract import ContractError, ContractErrorResponse, ContractRoute, ok
from app.auth import contract_unauthorized, get_contract_user
from app.database import get_db
from app.models import User, UserRole
from app.region_service import SIDO_CODE_PATTERN, list_sido, list_sigungu, resolve_location

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


class LocationResolutionRoute(ContractRoute):
    def validation_error(self, exc):
        return ContractError(422, "INVALID_COORDINATES", "위치 정보를 확인해 주세요.")

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def location_handler(request):
            response = await handler(request)
            if response.status_code == 401:
                response = contract_unauthorized("AUTHENTICATION_REQUIRED").response()
            return _no_store(response)

        return location_handler


class LocationCoordinates(BaseModel):
    latitude: Annotated[float, Field(strict=True, allow_inf_nan=False, ge=-90, le=90)]
    longitude: Annotated[float, Field(strict=True, allow_inf_nan=False, ge=-180, le=180)]


class ResolvedRegion(BaseModel):
    sido_code: str
    sido_name: str
    region_code: str
    region_name: str


class ResolvedLocationData(BaseModel):
    region: ResolvedRegion


class ResolvedLocationResponse(BaseModel):
    code: Literal["OK"] = "OK"
    data: ResolvedLocationData
    message: str = "현재 위치에 해당하는 지역을 확인했습니다."


def require_location_user(user: User = Depends(get_contract_user)):
    if user.role != UserRole.USER or not is_active(user):
        raise ContractError(403, "ONBOARDING_NOT_ALLOWED", "위치 정보를 사용할 수 없습니다.")


location_router = APIRouter(route_class=LocationResolutionRoute)


@location_router.post("/resolve-location", dependencies=[Depends(require_location_user)],
                      response_model=ResolvedLocationResponse, responses={
    401: {"model": ContractErrorResponse, "description": "AUTHENTICATION_REQUIRED"},
    403: {"model": ContractErrorResponse, "description": "ONBOARDING_NOT_ALLOWED"},
    422: {"model": ContractErrorResponse, "description": "INVALID_COORDINATES, REGION_NOT_SUPPORTED"},
    500: SERVER_ERRORS[500],
    503: {"model": ContractErrorResponse, "description": "GEOCODING_UNAVAILABLE"},
})
def post_resolve_location(coordinates: LocationCoordinates, db: Session = Depends(get_db)):
    region = resolve_location(db, coordinates.latitude, coordinates.longitude)
    return ResolvedLocationResponse(data=ResolvedLocationData(region=ResolvedRegion(**region)))


router.include_router(location_router)
