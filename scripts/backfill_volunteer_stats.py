"""Rebuild completed-walk statistics. Default: read-only preview; --apply writes.

One transaction per user, User -> Profile locks (shared with live end_walk).
A conflict rolls that user's transaction back and stops the run; reruns are safe.
No schema changes, level changes, GPS recalculation or database resets.
"""
import argparse
import json

from sqlalchemy import select

from app.models import User, VolunteerProfile, Walk, WalkRequest
from app.volunteer_stats import completed_totals, refresh_volunteer_profile
from app.walk_request_service import lock_row

METRICS = ('total_walk_count', 'total_distance_m', 'total_duration_seconds', 'unique_dog_count')


def candidate_users(db, user_id=None):
    query = select(VolunteerProfile.user_id).union(
        select(WalkRequest.user_id).join(Walk, Walk.walk_request_id == WalkRequest.id)
        .where(Walk.status == 'COMPLETED'))
    users = sorted(db.execute(query).scalars().all())
    return [user_id] if user_id is not None and user_id in users else users if user_id is None else []


def rebuild_profiles(session_factory, *, apply=False, user_id=None):
    with session_factory() as db:
        users = candidate_users(db, user_id)
    results = []
    for identifier in users:
        with session_factory.begin() as db:
            if apply:
                if lock_row(db, User, identifier) is None:
                    raise ValueError('User not found')
            profile = db.get(VolunteerProfile, identifier)
            before = {name: getattr(profile, name) for name in METRICS} if profile else None
            level = profile.level if profile else 1
            if apply:
                profile = refresh_volunteer_profile(db, identifier)
                after = {name: getattr(profile, name) for name in METRICS}
            else:
                after = completed_totals(db, identifier)
            results.append({'user_id': identifier, 'level': level, 'before': before, 'after': after})
    return {'applied': apply, 'users': results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='명시적으로 통계 갱신 (기본: 읽기 전용)')
    parser.add_argument('--user-id', type=int, help='한 사용자만 처리')
    args = parser.parse_args()
    if args.user_id is not None and args.user_id <= 0:
        parser.error('--user-id must be positive')
    from app.database import SessionLocal
    try:
        print(json.dumps(rebuild_profiles(SessionLocal, apply=args.apply, user_id=args.user_id),
                         ensure_ascii=False, indent=2, default=str))
    except Exception:
        print('중단: 현재 사용자 트랜잭션을 롤백했습니다. 이전 사용자까지 반영되었을 수 있습니다. 상태 확인 후 재실행하세요.')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
