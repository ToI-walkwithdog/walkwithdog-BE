from datetime import datetime
from fastapi import HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import and_, or_
from app.models import Dog, Shelter, DogWalkDateSlot, WalkRequest

def get_available_dogs_by_region(db: Session, region: str):
    """
    주어진 지역(텍스트) 기반으로 예약 가능한 슬롯이 있는 강아지 목록 반환
    """
    # 예약된 슬롯 ID 서브쿼리 (PENDING, APPROVED 상태)
    booked_slots_subquery = db.query(WalkRequest.date_slot_id).filter(
        WalkRequest.status.in_(['PENDING', 'APPROVED']),
        WalkRequest.date_slot_id.isnot(None)
    ).subquery()

    # 지역 필터 및 예약되지 않은 슬롯을 가진 강아지 조회
    available_dogs = db.query(Dog).join(Shelter).join(DogWalkDateSlot).filter(
        Shelter.address.ilike(f"%{region}%"),
        DogWalkDateSlot.id.notin_(booked_slots_subquery),
        DogWalkDateSlot.slot_date >= datetime.now().date()
    ).distinct().all()
    
    return available_dogs

def create_walk_request_with_lock(db: Session, user_id: int, slot_id: int):
    """
    비관적 락(Pessimistic Lock)을 사용하여 동시성 제어 및 예약 생성
    """
    # 1. 슬롯 ROW에 Lock 걸기
    slot = db.query(DogWalkDateSlot).with_for_update().filter(DogWalkDateSlot.id == slot_id).first()
    if not slot:
        raise HTTPException(status_code=404, detail="Slot not found.")
        
    # 2. 해당 슬롯이 이미 예약되었는지 확인
    existing_request = db.query(WalkRequest).filter(
        WalkRequest.date_slot_id == slot_id,
        WalkRequest.status.in_(['PENDING', 'APPROVED'])
    ).first()
    
    if existing_request:
        raise HTTPException(status_code=409, detail="이미 신청된 시간이에요.")
        
    # 3. 동일 사용자의 시간대 중복 예약 검사
    overlapping_user_request = db.query(WalkRequest).join(DogWalkDateSlot).filter(
        WalkRequest.user_id == user_id,
        WalkRequest.status.in_(['PENDING', 'APPROVED']),
        DogWalkDateSlot.slot_date == slot.slot_date,
        DogWalkDateSlot.end_time > slot.start_time,
        DogWalkDateSlot.start_time < slot.end_time
    ).first()
    
    if overlapping_user_request:
        raise HTTPException(status_code=400, detail="해당 시간에 이미 다른 예약이 존재합니다.")
        
    # 4. 예약 생성
    new_request = WalkRequest(
        user_id=user_id,
        dog_id=slot.dog_id,
        shelter_id=slot.dog.shelter_id,
        date_slot_id=slot.id,
        requested_start_at=datetime.combine(slot.slot_date, slot.start_time),
        requested_end_at=datetime.combine(slot.slot_date, slot.end_time),
        status='PENDING'
    )
    
    db.add(new_request)
    db.commit()
    db.refresh(new_request)
    return new_request