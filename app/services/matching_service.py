from sqlalchemy.orm import Session
# 실제 프로젝트의 모델 경로에 맞게 수정 필요
# from app.models.user import User
# from app.models.walk_data import WalkData

def calculate_matching_score(user_id: int, limit: int = 10) -> list:
    """
    사용자의 과거 산책 데이터를 분석하여 적합도가 높은 타 사용자를 반환합니다.
    (거리, 평균 속도, 선호 산책 시간대 고려)
    """
    # TODO: DB 세션을 넘겨받아 실제 모델 연동 후 쿼리 작업 진행
    
    # 1. 요청한 사용자의 산책 성향 분석 (예: 평균 시속 4km, 1시간 산책, 저녁 시간대 선호)
    # user_walk_profile = db.query(WalkData).filter(WalkData.user_id == user_id).all()
    
    # 2. 다른 사용자들의 산책 성향 데이터 조회
    # potential_matches = db.query(User).filter(User.id != user_id).all()
    
    # 3. 적합도(스코어) 계산 알고리즘 적용 (목업 데이터)
    mock_matched_result = [
        {
            "matched_user_id": 2,
            "score": 92.5,
            "common_factors": ["저녁 시간대", "비슷한 평균 속도(4.2km/h)"]
        },
        {
            "matched_user_id": 5,
            "score": 88.0,
            "common_factors": ["긴 산책 시간(1.5시간)"]
        }
    ]
    
    # 4. 스코어 순 정렬 및 limit 적용하여 반환
    sorted_matches = sorted(mock_matched_result, key=lambda x: x["score"], reverse=True)
    
    return sorted_matches[:limit]
