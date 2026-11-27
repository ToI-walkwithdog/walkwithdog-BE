from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import get_db
from app.models import Dog, Shelter, User, UserRole, Walk, WalkIncident, WalkRequest
from app.walk_record_schemas import ShelterIncidentDog, ShelterIncidentItem
from app.walk_service import utc_now

router = APIRouter(prefix="/shelters/me", tags=["shelter-incidents"])


@router.get("/incident-alerts", response_model=list[ShelterIncidentItem])
def get_incident_alerts(
    after_id: Optional[int] = Query(None, ge=0),
    limit: int = Query(50, ge=1, le=100),
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """보호소 계정 전용 미확인 알림 조회 (after_id 기준 오름차순 폴링)."""
    if actor.role != UserRole.SHELTER or not actor.shelter_id:
        raise HTTPException(403, "보호소 계정만 알림을 조회할 수 있습니다.")

    query = (
        db.query(WalkIncident, Dog.id, Dog.name, Shelter.phone, User.nickname)
        .join(Walk, WalkIncident.walk_id == Walk.id)
        .join(WalkRequest, Walk.walk_request_id == WalkRequest.id)
        .join(Dog, WalkRequest.dog_id == Dog.id)
        .join(Shelter, WalkRequest.shelter_id == Shelter.id)
        .join(User, WalkIncident.reported_by == User.id)
        .filter(WalkRequest.shelter_id == actor.shelter_id)
        .filter(WalkIncident.acknowledged_at.is_(None))
    )
    if after_id is not None:
        query = query.filter(WalkIncident.id > after_id)

    rows = query.order_by(WalkIncident.id.asc()).limit(limit).all()
    return [
        ShelterIncidentItem(
            id=inc.id,
            walk_id=inc.walk_id,
            incident_type=inc.incident_type,
            description=inc.description,
            dog=ShelterIncidentDog(id=dog_id, name=dog_name),
            latitude=inc.latitude,
            longitude=inc.longitude,
            occurred_at=inc.occurred_at,
            created_at=inc.created_at,
            reporter_nickname=reporter_nick,
            shelter_phone=phone,
            acknowledged_at=inc.acknowledged_at,
        )
        for inc, dog_id, dog_name, phone, reporter_nick in rows
    ]


@router.post("/incident-alerts/{incident_id}/ack", response_model=ShelterIncidentItem)
def acknowledge_incident(
    incident_id: int,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """보호소 알림 확인 처리 (이미 확인된 신고는 최초 확인 기록 유지하며 200 반환)."""
    if actor.role != UserRole.SHELTER or not actor.shelter_id:
        raise HTTPException(403, "보호소 계정만 알림을 확인할 수 있습니다.")

    row = (
        db.query(WalkIncident, WalkRequest.shelter_id, Dog.id, Dog.name, Shelter.phone, User.nickname)
        .join(Walk, WalkIncident.walk_id == Walk.id)
        .join(WalkRequest, Walk.walk_request_id == WalkRequest.id)
        .join(Dog, WalkRequest.dog_id == Dog.id)
        .join(Shelter, WalkRequest.shelter_id == Shelter.id)
        .join(User, WalkIncident.reported_by == User.id)
        .filter(WalkIncident.id == incident_id)
        .first()
    )
    if row is None:
        raise HTTPException(404, "신고를 찾을 수 없습니다.")

    inc, shelter_id, dog_id, dog_name, phone, reporter_nick = row
    if shelter_id != actor.shelter_id:
        raise HTTPException(403, "다른 보호소의 신고는 확인할 수 없습니다.")

    if inc.acknowledged_at is None:
        inc.acknowledged_at = utc_now()
        inc.acknowledged_by = actor.id
        db.commit()
        db.refresh(inc)

    return ShelterIncidentItem(
        id=inc.id,
        walk_id=inc.walk_id,
        incident_type=inc.incident_type,
        description=inc.description,
        dog=ShelterIncidentDog(id=dog_id, name=dog_name),
        latitude=inc.latitude,
        longitude=inc.longitude,
        occurred_at=inc.occurred_at,
        created_at=inc.created_at,
        reporter_nickname=reporter_nick,
        shelter_phone=phone,
        acknowledged_at=inc.acknowledged_at,
    )


@router.get("/incidents", response_model=list[ShelterIncidentItem])
def get_shelter_incidents(
    from_date: Optional[datetime] = Query(None, alias="from"),
    to_date: Optional[datetime] = Query(None, alias="to"),
    incident_type: Optional[str] = Query(None),
    acknowledged: Optional[bool] = Query(None),
    shelter_id: Optional[int] = Query(None),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """보호소 위험 신고 이력 조회 (보호소는 본인 보호소만, 관리자는 shelter_id 필수)."""
    if actor.role == UserRole.USER:
        raise HTTPException(403, "일반 사용자는 보호소 신고 이력을 조회할 수 없습니다.")

    if actor.role == UserRole.SHELTER:
        if not actor.shelter_id:
            raise HTTPException(403, "소속 보호소가 설정되지 않았습니다.")
        target_shelter_id = actor.shelter_id
    elif actor.role == UserRole.ADMIN:
        if shelter_id is None:
            raise HTTPException(422, "관리자는 shelter_id 쿼리 파라미터가 필수입니다.")
        target_shelter_id = shelter_id
    else:
        raise HTTPException(403, "권한이 없습니다.")

    query = (
        db.query(WalkIncident, Dog.id, Dog.name, Shelter.phone, User.nickname)
        .join(Walk, WalkIncident.walk_id == Walk.id)
        .join(WalkRequest, Walk.walk_request_id == WalkRequest.id)
        .join(Dog, WalkRequest.dog_id == Dog.id)
        .join(Shelter, WalkRequest.shelter_id == Shelter.id)
        .join(User, WalkIncident.reported_by == User.id)
        .filter(WalkRequest.shelter_id == target_shelter_id)
    )
    if from_date:
        query = query.filter(WalkIncident.occurred_at >= from_date)
    if to_date:
        query = query.filter(WalkIncident.occurred_at <= to_date)
    if incident_type:
        query = query.filter(WalkIncident.incident_type == incident_type)
    if acknowledged is not None:
        if acknowledged:
            query = query.filter(WalkIncident.acknowledged_at.is_not(None))
        else:
            query = query.filter(WalkIncident.acknowledged_at.is_(None))

    rows = (
        query.order_by(WalkIncident.occurred_at.desc(), WalkIncident.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return [
        ShelterIncidentItem(
            id=inc.id,
            walk_id=inc.walk_id,
            incident_type=inc.incident_type,
            description=inc.description,
            dog=ShelterIncidentDog(id=dog_id, name=dog_name),
            latitude=inc.latitude,
            longitude=inc.longitude,
            occurred_at=inc.occurred_at,
            created_at=inc.created_at,
            reporter_nickname=reporter_nick,
            shelter_phone=phone,
            acknowledged_at=inc.acknowledged_at,
        )
        for inc, dog_id, dog_name, phone, reporter_nick in rows
    ]
