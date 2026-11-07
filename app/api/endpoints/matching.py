from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
# 실제 프로젝트의 DB 세션 의존성 경로에 맞게 수정 필요
# from app.api import deps 
from app.services.matching_service import calculate_matching_score

router = APIRouter()

@router.get("/suitability/{user_id}", summary="산책 데이터 기반 적합도 매칭 조회")
def get_suitability_matching(
    user_id: int,
    # db: Session = Depends(deps.get_db),
    limit: int = 10
):
    """
    #7 산책 데이터(속도, 거리, 시간대 등) 기반 사용자 간 적합도 매칭 리스트 반환
    """
    try:
        matched_users = calculate_matching_score(user_id=user_id, limit=limit)
        if not matched_users:
            return {"message": "적합한 매칭 상대를 찾을 수 없습니다.", "data": []}
            
        return {
            "message": "매칭 조회가 완료되었습니다.",
            "data": matched_users
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
