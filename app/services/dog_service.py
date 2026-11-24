import os
import uuid
from datetime import datetime, timezone
import logging
from pathlib import Path
from fastapi import UploadFile, HTTPException
from sqlalchemy.orm import Session
from app.models import DogWalkDateSlot
from app.walk_date_slots import validate_date_slot

logger = logging.getLogger(__name__)
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
    image_url = f"/media/dogs/{filename}"
    filepath = Path(os.getenv("UPLOAD_DIR", "./uploads/dogs")) / filename
    created = False
    try:
        filepath.parent.mkdir(parents=True, exist_ok=True)
        with filepath.open("xb") as stream:
            created = True
            stream.write(file.file.read())
    except OSError as exc:
        if created:
            cleanup_dog_image(image_url)
        logger.error("Dog image upload failed: %s", type(exc).__name__)
        raise HTTPException(500, "강아지 사진을 저장하지 못했습니다.") from None
    return image_url


def cleanup_dog_image(image_url):
    """Clean up a file created by this request, without hiding the original error."""
    filename = image_url.rsplit("/", 1)[-1]
    try:
        (Path(os.getenv("UPLOAD_DIR", "./uploads/dogs")) / filename).unlink(missing_ok=True)
    except OSError as exc:
        logger.error("Dog image cleanup failed for %s: %s", filename, type(exc).__name__)


def validate_and_create_date_slots(db: Session, dog_id: int, slots_data: list):
    """Insert slots under the existing parent lock. Only the caller commits."""
    now = datetime.now(timezone.utc)
    created = []
    for slot in slots_data:
        validate_date_slot(db, dog_id, slot, now)
        row = DogWalkDateSlot(dog_id=dog_id, **slot.model_dump())
        db.add(row)
        db.flush()
        created.append(row)
    return created
