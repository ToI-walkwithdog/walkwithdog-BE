import os
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from fastapi import UploadFile, HTTPException
from sqlalchemy.orm import Session
from app.models import DogWalkDateSlot

KST = ZoneInfo("Asia/Seoul")
MAX_FILE_SIZE = 5 * 1024 * 1024 # 5MB

def save_dog_image(file: UploadFile) -> str:
    # 파일 시그니처 (매직 넘버) 검증 - 간단한 구현 예시
    header = file.file.read(8)
    file.file.seek(0)
    
    is_jpg = header.startswith(b'\xff\xd8\xff')
    is_png = header.startswith(b'\x89PNG\r\n\x1a\n')
    
    if not (is_jpg or is_png):
        raise HTTPException(status_code=400, detail="Only JPG and PNG images are allowed.")
        
    file.file.seek(0, 2)
    file_size = file.file.tell()
    file.file.seek(0)
    
    if file_size > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="File size exceeds 5MB limit.")
        
    ext = ".jpg" if is_jpg else ".png"
    filename = f"{uuid.uuid4()}{ext}"
    
    upload_dir = os.getenv("UPLOAD_DIR", "./uploads/dogs")
    os.makedirs(upload_dir, exist_ok=True)
    
    filepath = os.path.join(upload_dir, filename)
    with open(filepath, "wb") as f:
        f.write(file.file.read())
        
    return f"/media/dogs/{filename}"

def validate_and_create_date_slots(db: Session, dog_id: int, slots_data: list):
    now_kst = datetime.now(KST)
    today = now_kst.date()
    max_date = today + timedelta(days=180)
    
    for slot in slots_data:
        # 1. 날짜 범위 검증
        if slot.slot_date < today or slot.slot_date > max_date:
            raise HTTPException(status_code=400, detail="Date must be today or within 6 months.")
            
        # 2. 오늘 날짜의 지난 시간대 검증
        if slot.slot_date == today and slot.start_time < now_kst.time():
            raise HTTPException(status_code=400, detail="Cannot register past times for today.")
            
        # 3. 소요시간 및 시간 범위 검증
        start_dt = datetime.combine(slot.slot_date, slot.start_time)
        end_dt = datetime.combine(slot.slot_date, slot.end_time)
        if (end_dt - start_dt).total_seconds() < slot.duration_minutes * 60:
            raise HTTPException(status_code=400, detail="Duration exceeds the time slot range.")
            
        if slot.duration_minutes not in [30, 60, 90]:
            raise HTTPException(status_code=400, detail="Duration must be 30, 60, or 90 minutes.")
            
        # 4. 시간대 겹침 검증 (동일 강아지)
        overlap = db.query(DogWalkDateSlot).filter(
            DogWalkDateSlot.dog_id == dog_id,
            DogWalkDateSlot.slot_date == slot.slot_date,
            DogWalkDateSlot.end_time > slot.start_time,
            DogWalkDateSlot.start_time < slot.end_time
        ).first()
        
        if overlap:
            raise HTTPException(status_code=400, detail="Time slot overlaps with an existing slot.")
            
        new_slot = DogWalkDateSlot(
            dog_id=dog_id,
            slot_date=slot.slot_date,
            start_time=slot.start_time,
            end_time=slot.end_time,
            duration_minutes=slot.duration_minutes
        )
        db.add(new_slot)
    
    db.commit()