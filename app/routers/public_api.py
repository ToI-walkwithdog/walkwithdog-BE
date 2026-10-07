import hmac
import os

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import bearer, get_current_session, get_current_user
from app.database import get_db
from app.models import UserRole
from app.public_data import BASE_URL, fetch_pages, synchronize

router = APIRouter(prefix="/public-api", tags=["public-api"])


class ImportRequest(BaseModel):
    dry_run: bool = True
    max_pages: int = Field(default=2, ge=1, le=10)
    rows: int = Field(default=20, ge=1, le=100)


def require_import_key(x_import_key: str | None = Header(default=None)):
    configured = os.getenv("PUBLIC_IMPORT_KEY")
    if not configured:
        raise HTTPException(503, "관리용 PUBLIC_IMPORT_KEY 설정이 필요합니다.")
    if not x_import_key or not hmac.compare_digest(x_import_key, configured):
        raise HTTPException(403, "데이터 수집 관리 권한이 필요합니다.")


def require_import_access(x_import_key: str | None = Header(default=None),
                          credentials=Depends(bearer), db: Session = Depends(get_db)):
    # Retain the trusted import-key workflow and the incoming admin workflow.
    if x_import_key is not None or credentials is None:
        require_import_key(x_import_key)
        return
    session = get_current_session(credentials, db)
    user = get_current_user(session, db)
    if user.role != UserRole.ADMIN:
        raise HTTPException(403, "데이터 수집 관리 권한이 필요합니다.")


@router.post("/fetch-dogs", dependencies=[Depends(require_import_access)])
def fetch_dogs_from_public_api(request: ImportRequest = ImportRequest(), db: Session = Depends(get_db)):
    items, metadata = fetch_pages(request.max_pages, request.rows)
    try:
        counts = synchronize(db, items, request.dry_run)
        if request.dry_run:
            db.rollback()
        else:
            db.commit()
        return {"message": "수집 계획" if request.dry_run else "공공데이터 갱신 완료", **counts, **metadata}
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "데이터 고유번호 충돌: 자동 병합하지 않습니다.") from None
    except Exception:
        db.rollback()
        raise


@router.get("/test", dependencies=[Depends(require_import_access)])
def test_public_api():
    items, metadata = fetch_pages(1, 1)
    # No complete request URL, API key, remote error body or identifying raw records.
    return {"status_code": 200, "endpoint": BASE_URL, "item_count": len(items), **metadata}
