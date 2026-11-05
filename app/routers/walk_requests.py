"""Walk application, review, historical shelter inbox, and read-only volunteer profile."""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import get_db
from app.dependencies import require_role, require_shelter, require_shelter_access
from app.models import Shelter, User, UserRole, VolunteerProfile, WalkRequest
from app.walk_request_schemas import (
    RequestStatus, VolunteerProfileResponse, WalkRequestAction, WalkRequestCreate, WalkRequestResponse,
)
from app.walk_request_service import create_request, request_list_query, transition_request

router = APIRouter(tags=["walk-requests"])
require_volunteer = require_role(UserRole.USER)


def database_error(exc):
    original = getattr(exc, "orig", None)
    code = getattr(original, "pgcode", None)
    constraint = getattr(getattr(original, "diag", None), "constraint_name", None)
    if code in ("55P03", "40P01", "40001"):
        return HTTPException(409, "다른 작업과 충돌했습니다. 다시 조회한 뒤 재시도하세요.")
    if code == "23505" and constraint == "uq_walk_requests_active":
        return HTTPException(409, "활성 신청이 중복됩니다.")
    if code == "23505" and constraint == "volunteer_profiles_pkey":
        return HTTPException(409, "봉사자 프로필 생성이 다른 요청과 충돌했습니다. 다시 시도하세요.")
    if code == "23P01" and constraint in ("ex_walk_requests_dog_booking", "ex_walk_requests_user_booking"):
        subject = "강아지" if constraint == "ex_walk_requests_dog_booking" else "봉사자"
        return HTTPException(409, f"{subject}의 승인 예약이 DB 제약과 충돌합니다.")
    return HTTPException(500, "산책 신청을 저장하지 못했습니다.")


def persist_request(db, operation):
    try:
        request = operation()
        response = WalkRequestResponse.model_validate(request)
        db.commit()
        return response
    except HTTPException:
        db.rollback()
        raise
    except SQLAlchemyError as exc:
        db.rollback()
        raise database_error(exc) from None


@router.post("/walk-requests", response_model=WalkRequestResponse, status_code=201)
def apply_for_walk(payload: WalkRequestCreate, actor: User = Depends(require_volunteer),
                   db: Session = Depends(get_db)):
    return persist_request(db, lambda: create_request(db, actor, payload))


@router.patch("/walk-requests/{request_id}", response_model=WalkRequestResponse)
def review_or_cancel(request_id: int, payload: WalkRequestAction,
                     actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return persist_request(db, lambda: transition_request(db, request_id, actor, payload))


@router.get("/walk-requests/my", response_model=list[WalkRequestResponse])
def my_requests(status: RequestStatus | None = None, limit: int = Query(20, ge=1, le=100),
                offset: int = Query(0, ge=0), actor: User = Depends(get_current_user),
                db: Session = Depends(get_db)):
    return request_list_query(db, status).filter(WalkRequest.user_id == actor.id).limit(limit).offset(offset).all()


@router.get("/shelters/{shelter_id}/walk-requests", response_model=list[WalkRequestResponse])
def shelter_requests(shelter_id: int, status: RequestStatus | None = None,
                     dog_id: int | None = Query(None, gt=0), limit: int = Query(20, ge=1, le=100),
                     offset: int = Query(0, ge=0), actor: User = Depends(require_shelter),
                     db: Session = Depends(get_db)):
    require_shelter_access(actor, shelter_id)
    if db.get(Shelter, shelter_id) is None:
        raise HTTPException(404, "Shelter not found")
    query = request_list_query(db, status).filter(WalkRequest.shelter_id == shelter_id)
    if dog_id is not None:
        query = query.filter(WalkRequest.dog_id == dog_id)
    return query.limit(limit).offset(offset).all()


@router.get("/volunteers/me", response_model=VolunteerProfileResponse, tags=["volunteers"])
def my_volunteer_profile(actor: User = Depends(require_volunteer), db: Session = Depends(get_db)):
    profile = db.get(VolunteerProfile, actor.id)
    return profile if profile is not None else VolunteerProfileResponse(user_id=actor.id)
