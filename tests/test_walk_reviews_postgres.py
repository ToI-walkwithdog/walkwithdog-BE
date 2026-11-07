"""Real PostgreSQL review locks, unique guard, rollback and existing SQL compatibility.

Only a NEW EMPTY local toi_walk_test_* DB is allowed via WALK_REVIEWS_TEST_DATABASE_URL.
Each case uses a private schema. No database/table drop, reset or production DB access.
"""
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

from test_walk_reviews import ReviewAssertions
from app.database import Base
from app.models import Dog, User, UserRole, VolunteerProfile, Walk, WalkRequest, WalkReview
from app.walk_models import WALK_MODELS
from app.walk_review_service import locked_request
from sqlalchemy import create_engine, event, inspect
from scripts.prepare_demo_db import ROOT, PreparationError, validate_local_engine
from scripts.walk_schema import check_motion_schema

TEST_URL = os.getenv('WALK_REVIEWS_TEST_DATABASE_URL')


@unittest.skipUnless(TEST_URL, 'WALK_REVIEWS_TEST_DATABASE_URL의 새 빈 격리 PostgreSQL이 필요합니다')
class WalkReviewPostgresTests(ReviewAssertions, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.admin = create_engine(TEST_URL)
        validate_local_engine(cls.admin)
        if not cls.admin.url.database.startswith('toi_walk_test_'):
            raise PreparationError('테스트 DB 이름은 toi_walk_test_로 시작해야 합니다.')
        with cls.admin.connect() as connection:
            inspector = inspect(connection)
            custom = [s for s in inspector.get_schema_names() if s not in ('public', 'information_schema') and not s.startswith('pg_')]
            if inspector.get_table_names() or custom:
                raise PreparationError('새로운 빈 테스트 DB만 허용합니다. 기존 DB는 변경하지 않습니다.')

    @classmethod
    def tearDownClass(cls):
        cls.admin.dispose()

    def setUp(self):
        schema = 'reviews_' + uuid4().hex
        with self.admin.begin() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        self.setup_walk_api(create_engine(TEST_URL, connect_args={'options': f'-csearch_path={schema}'}))

    def test_concurrent_posts_store_exactly_one_review_and_retry_is_409(self):
        identifier = self.completed()
        winner, loser = self.recording_race(
            lambda: self.post_review(identifier),
            lambda: self.post_review(identifier, payload={'leash_pulling': 1}),
            hook='app.walk_review_service.locked_review_walk')
        self.assertEqual((winner.status_code, loser.status_code), (201, 409))
        self.assertEqual(len(self.rows(WalkReview)), 1)
        self.assertEqual(self.read_review(identifier).json(), winner.json())
        self.assertEqual(self.post_review(identifier).status_code, 409)

    def test_actual_named_unique_error_is_409_and_preserves_original(self):
        identifier = self.completed()
        self.assertEqual(self.post_review(identifier).status_code, 201)
        second = self.begin(user=2, dog=3)['id']
        self.assertEqual(self.end(second, user=2).status_code, 200)
        before = self.database_snapshot()
        errors = []
        def collide(mapper, connection, target):
            target.walk_id = identifier
        def capture(context):
            error = context.original_exception
            errors.append((error.pgcode, error.diag.constraint_name))
        event.listen(WalkReview, 'before_insert', collide)
        event.listen(self.engine, 'handle_error', capture)
        try:
            response = self.post_review(second, user=2)
        finally:
            event.remove(self.engine, 'handle_error', capture)
            event.remove(WalkReview, 'before_insert', collide)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(errors, [('23505', 'uq_walk_reviews_walk')])
        self.assertEqual(self.database_snapshot(), before)
        self.assertEqual(self.post_review(second, user=2).status_code, 201)

    def test_every_required_row_lock_is_real_and_conflicts_are_409(self):
        identifier = self.completed()
        with self.Session() as db:
            request_id = db.get(Walk, identifier).walk_request_id
        for model, key in ((Dog, 1), (User, 1), (WalkRequest, request_id), (Walk, identifier)):
            with self.subTest(model=model.__name__), self.Session.begin() as other:
                other.query(model).filter(model.id == key).with_for_update().one()
                self.assertEqual(self.post_review(identifier).status_code, 409)
                self.assertEqual(self.rows(WalkReview), [])
        self.assertEqual(self.post_review(identifier).status_code, 201)

    def test_actual_database_check_failure_rolls_back_without_touching_completed_metrics(self):
        identifier = self.completed()
        before = self.database_snapshot()
        def invalid_score(mapper, connection, target):
            target.leash_pulling = 0
        event.listen(WalkReview, 'before_insert', invalid_score)
        try:
            response = self.post_review(identifier)
        finally:
            event.remove(WalkReview, 'before_insert', invalid_score)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.database_snapshot(), before)
        self.assertEqual(self.post_review(identifier).status_code, 201)

    def test_end_wins_review_race_and_review_retry_uses_completed_walk(self):
        identifier = self.begin()['id']
        self.advance(120)
        self.upload(identifier, [self.point(), self.point(1, 10, longitude='.0001'), self.point(2, 20, longitude='.0002')])
        winner, loser = self.recording_race(lambda: self.end(identifier), lambda: self.post_review(identifier))
        self.assertEqual((winner.status_code, loser.status_code), (200, 409))
        before = self.database_snapshot(include_reviews=False)
        self.assertEqual(self.post_review(identifier).status_code, 201)
        self.assertEqual(self.database_snapshot(include_reviews=False), before)

    def test_review_attempt_before_end_holds_locks_but_cannot_review_in_progress(self):
        identifier = self.begin()['id']
        self.advance(120)
        first, second = self.recording_race(
            lambda: self.post_review(identifier), lambda: self.end(identifier),
            hook='app.walk_review_service.lock_row')
        self.assertEqual((first.status_code, second.status_code), (409, 409))
        self.assertEqual(self.rows(WalkReview), [])
        self.assertEqual(self.end(identifier).status_code, 200)
        self.assertEqual(self.post_review(identifier).status_code, 201)

    def test_get_during_uncommitted_registration_sees_no_review_without_writing(self):
        identifier = self.completed()
        winner, reader = self.recording_race(
            lambda: self.post_review(identifier), lambda: self.read_review(identifier),
            hook='app.routers.walks.create_review')
        self.assertEqual((winner.status_code, reader.status_code), (201, 404))
        self.assertEqual(self.read_review(identifier).json(), winner.json())
        self.assertEqual(len(self.rows(WalkReview)), 1)

    def test_role_change_after_authentication_is_rechecked_under_user_lock(self):
        identifier = self.completed()
        def changed_role(*args, **kwargs):
            with self.Session.begin() as other:
                other.get(User, 1).role = UserRole.SHELTER
            return locked_request(*args, **kwargs)
        with patch('app.walk_review_service.locked_request', side_effect=changed_role):
            self.assertEqual(self.post_review(identifier).status_code, 403)
        self.assertEqual(self.rows(WalkReview), [])

    def test_review_does_not_need_a_profile_write_lock(self):
        identifier = self.completed()
        before = self.rows(VolunteerProfile)
        with self.Session.begin() as other:
            other.query(VolunteerProfile).filter_by(user_id=1).with_for_update().one()
            self.assertEqual(self.post_review(identifier).status_code, 201)
        self.assertEqual(self.rows(VolunteerProfile), before)

    def test_api_works_on_existing_007_and_008_sql_without_new_migration(self):
        # The otherwise-unused public schema exercises the checked-in SQL itself;
        # all other cases remain in separate private schemas without resets.
        walk_names = {model.__tablename__ for model in WALK_MODELS}
        with self.admin.begin() as connection:
            self.assertEqual(inspect(connection).get_table_names(), [])
            Base.metadata.create_all(connection, tables=[table for table in Base.metadata.sorted_tables if table.name not in walk_names])
            for statement in (ROOT / 'migrations/007_walk_domain.sql').read_text().split(';'):
                statement = '\n'.join(line for line in statement.splitlines() if not line.startswith('--')).strip()
                if statement and statement.upper() not in ('BEGIN', 'COMMIT'):
                    connection.exec_driver_sql(statement)
            connection.exec_driver_sql((ROOT / 'migrations/008_gps_motion_analysis.sql').read_text())
            self.assertEqual(check_motion_schema(connection), [])
        self.setup_walk_api(self.admin)
        identifier = self.completed()
        self.assertEqual(self.post_review(identifier, payload={'leash_pulling': 5}).status_code, 201)
        self.assertEqual(self.read_review(identifier).json()['leash_pulling'], 5)
        with self.admin.connect() as connection:
            self.assertEqual(check_motion_schema(connection), [])


if __name__ == '__main__':
    unittest.main()
