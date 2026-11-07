"""Review API contract and permissions, also reused unchanged on PostgreSQL."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from walk_record_test_support import WalkFixture, START
from app.database import Base, get_db
from app.models import Dog, User, VolunteerProfile, Walk, WalkPoint, WalkReview
from app.walk_models import REVIEW_METRICS
from app.walk_review_schemas import WalkReviewResponse
from app.routers import walk_maps
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.pool import StaticPool

REVIEW_KEYS = {'id', 'walk_id', *REVIEW_METRICS, 'memo', 'created_at', 'updated_at'}


class ReviewAssertions(WalkFixture):
    def completed(self):
        walk = self.begin()
        self.advance(120)
        self.assertEqual(self.upload(walk['id'], [self.point(), self.point(1, 10, longitude='.0001'),
                                                 self.point(2, 20, longitude='.0002')]).status_code, 200)
        self.assertEqual(self.end(walk['id']).status_code, 200)
        return walk['id']

    def post_review(self, walk_id, user=1, payload=None):
        return self.client.post(f'/walks/{walk_id}/review', headers=self.auth(user),
                                json={'leash_pulling': 4} if payload is None else payload)

    def read_review(self, walk_id, user=1):
        return self.client.get(f'/walks/{walk_id}/review', headers=self.auth(user))

    def database_snapshot(self, *, include_reviews=True):
        return {mapper.class_: self.rows(mapper.class_) for mapper in Base.registry.mappers
                if include_reviews or mapper.class_ is not WalkReview}

    def test_register_all_scores_and_read_exact_original_values_in_utc(self):
        identifier = self.completed()
        payload = dict(leash_pulling=4, handling_difficulty=2, sociability_with_people=5,
                       sociability_with_dogs=None, barking_level=2, memo='  관찰한 원문을 보존합니다.\n')
        response = self.post_review(identifier, payload=payload)
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(set(body), REVIEW_KEYS)
        self.assertEqual(body['walk_id'], identifier)
        self.assertEqual({k: body[k] for k in payload}, payload)
        for field in ('created_at', 'updated_at'):
            self.assertTrue(body[field].endswith('Z'), body)
        self.assertEqual(self.read_review(identifier).status_code, 200)
        self.assertEqual(self.read_review(identifier).json(), body)

    def test_single_observed_score_accepts_one_and_preserves_other_nulls(self):
        identifier = self.completed()
        response = self.post_review(identifier, payload={'barking_level': 1, 'leash_pulling': None})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()['barking_level'], 1)
        self.assertIsNone(response.json()['memo'])
        for field in REVIEW_METRICS:
            if field != 'barking_level':
                self.assertIsNone(response.json()[field])

    def test_empty_all_null_and_memo_only_requests_are_422(self):
        identifier = self.completed()
        for payload in ({}, dict.fromkeys(REVIEW_METRICS), {'memo': '관찰 점수 없음'}, {'memo': None}):
            with self.subTest(payload=payload):
                self.assertEqual(self.post_review(identifier, payload=payload).status_code, 422)
        self.assertEqual(self.rows(WalkReview), [])

    def test_every_score_rejects_out_of_range_and_non_strict_integers(self):
        identifier = self.completed()
        for field in REVIEW_METRICS:
            for value in (0, 6, -1, 1.0, 1.5, True, False, '3', [], {}):
                with self.subTest(field=field, value=value):
                    self.assertEqual(self.post_review(identifier, payload={field: value}).status_code, 422)
        self.assertEqual(self.rows(WalkReview), [])

    def test_server_managed_and_unknown_fields_are_rejected(self):
        identifier = self.completed()
        for field, value in (('id', 7), ('walk_id', identifier), ('user_id', 2), ('status', 'COMPLETED'),
                             ('created_at', START.isoformat()), ('updated_at', START.isoformat()),
                             ('reviewed_by', 5), ('author_id', 1)):
            with self.subTest(field=field):
                self.assertEqual(self.post_review(identifier, payload={'leash_pulling': 2, field: value}).status_code, 422)
        self.assertEqual(self.rows(WalkReview), [])

    def test_both_endpoints_require_authentication(self):
        identifier = self.completed()
        self.assertEqual(self.post_review(identifier, user=None).status_code, 401)
        self.assertEqual(self.read_review(identifier, user=None).status_code, 401)
        self.assertEqual(self.rows(WalkReview), [])

    def test_other_volunteers_shelters_and_admin_cannot_register(self):
        identifier = self.completed()
        for user in (2, 3, 4, 5, 7):
            self.assertEqual(self.post_review(identifier, user=user).status_code, 403)
        self.assertEqual(self.rows(WalkReview), [])

    def test_missing_walk_register_is_404(self):
        self.assertEqual(self.post_review(999).status_code, 404)
        self.assertEqual(self.rows(WalkReview), [])

    def test_in_progress_walk_register_is_409(self):
        identifier = self.begin()['id']
        self.assertEqual(self.post_review(identifier).status_code, 409)
        self.assertEqual(self.rows(WalkReview), [])
        self.assertEqual(self.detail(identifier).json()['walk']['status'], 'IN_PROGRESS')

    def test_interrupted_walk_register_is_409(self):
        identifier = self.begin()['id']
        self.advance(120)
        self.assertEqual(self.end(identifier, status='INTERRUPTED').status_code, 200)
        self.assertEqual(self.post_review(identifier).status_code, 409)
        self.assertEqual(self.rows(WalkReview), [])

    def test_cancelled_walk_register_is_409(self):
        identifier = self.begin()['id']
        with self.Session.begin() as db:
            record = db.get(Walk, identifier)
            record.status, record.ended_at = 'CANCELLED', START + timedelta(seconds=120)
        self.assertEqual(self.post_review(identifier).status_code, 409)
        self.assertEqual(self.rows(WalkReview), [])

    def test_duplicate_identical_or_changed_post_is_409_and_preserves_original(self):
        identifier = self.completed()
        self.assertEqual(self.post_review(identifier).status_code, 201)
        before = self.rows(WalkReview)
        self.assertEqual(self.post_review(identifier).status_code, 409)
        self.assertEqual(self.post_review(identifier, payload={'leash_pulling': 1}).status_code, 409)
        self.assertEqual(self.rows(WalkReview), before)

    def test_read_without_review_is_404_after_authorization(self):
        identifier = self.completed()
        for user in (1, 3, 5):
            self.assertEqual(self.read_review(identifier, user=user).status_code, 404)
        for user in (2, 4, 7):
            self.assertEqual(self.read_review(identifier, user=user).status_code, 403)
        self.assertEqual(self.rows(WalkReview), [])

    def test_missing_walk_read_is_404(self):
        for user in (1, 3, 5):
            self.assertEqual(self.read_review(999, user=user).status_code, 404)

    def test_only_owner_historical_shelter_and_admin_can_read_review(self):
        identifier = self.completed()
        created = self.post_review(identifier).json()
        for user in (1, 3, 5):
            response = self.read_review(identifier, user=user)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), created)
        for user in (2, 4, 7):
            self.assertEqual(self.read_review(identifier, user=user).status_code, 403)

    def test_transfer_does_not_change_registration_or_historical_shelter_read_access(self):
        identifier = self.completed()
        with self.Session.begin() as db:
            db.get(Dog, 1).shelter_id = 2
        self.assertEqual(self.post_review(identifier).status_code, 201)
        self.assertEqual(self.read_review(identifier, user=3).status_code, 200)
        self.assertEqual(self.read_review(identifier, user=4).status_code, 403)

    def test_reads_never_write_or_auto_create_and_only_return_review_fields(self):
        identifier = self.completed()
        before = self.database_snapshot()
        self.assertEqual(self.read_review(identifier).status_code, 404)
        self.assertEqual(self.database_snapshot(), before)
        self.assertEqual(self.post_review(identifier).status_code, 201)
        before = self.database_snapshot()
        for user in (1, 3, 5, 2, 4):
            response = self.read_review(identifier, user=user)
            if response.status_code == 200:
                self.assertEqual(set(response.json()), REVIEW_KEYS)
        self.assertEqual(self.database_snapshot(), before)

    def test_review_registration_does_not_modify_gps_stats_dog_traits_or_requests(self):
        identifier = self.completed()
        before = self.database_snapshot(include_reviews=False)
        self.assertEqual(self.post_review(identifier).status_code, 201)
        self.assertEqual(self.database_snapshot(include_reviews=False), before)

    def test_public_dog_map_does_not_expose_review_and_raw_gps_stays_private(self):
        self.app.include_router(walk_maps.router)
        identifier = self.completed()
        self.assertEqual(self.post_review(identifier, payload={'leash_pulling': 5, 'memo': '개인 평가 원문'}).status_code, 201)
        public = self.client.get('/dogs/1/map', headers=self.auth(2))
        self.assertEqual(public.status_code, 200)
        for field in (*REVIEW_METRICS, 'memo', '개인 평가 원문', 'review'):
            self.assertNotIn(field, public.text)
        self.assertEqual(self.detail(identifier, user=2).status_code, 403)

    def test_completed_gps_writes_and_repeat_end_still_rejected_after_review(self):
        identifier = self.completed()
        self.assertEqual(self.post_review(identifier).status_code, 201)
        before = self.database_snapshot()
        for response in (self.upload(identifier, [self.point(3, 30, longitude='.0003')]),
                         self.marker(identifier), self.incident(identifier), self.end(identifier)):
            self.assertEqual(response.status_code, 409)
        self.assertEqual(self.database_snapshot(), before)

    def test_commit_failure_explicitly_rolls_back_and_exposes_no_database_details(self):
        identifier = self.completed()
        before = self.database_snapshot()
        rolled_back = []
        def fail_db():
            with self.Session() as db, patch.object(db, 'commit', side_effect=SQLAlchemyError('private SQL')), \
                    patch.object(db, 'rollback', wraps=db.rollback) as rollback:
                try:
                    yield db
                finally:
                    rolled_back.append(rollback.called)
        self.app.dependency_overrides[get_db] = fail_db
        response = self.post_review(identifier)
        self.assertEqual(response.status_code, 500)
        self.assertNotIn('private', response.text)
        self.assertEqual(rolled_back, [True])
        self.assertEqual(self.database_snapshot(), before)

    def test_response_validation_happens_before_commit_and_failure_is_not_persisted(self):
        identifier = self.completed()
        before = self.database_snapshot()
        validate = WalkReviewResponse.model_validate
        committed = []
        def invalid_response(value):
            return validate({'id': 'invalid'})
        def track_db():
            with self.Session() as db, patch.object(db, 'commit', wraps=db.commit) as commit:
                try:
                    yield db
                finally:
                    committed.append(commit.called)
        self.app.dependency_overrides[get_db] = track_db
        with TestClient(self.app, raise_server_exceptions=False) as client, \
                patch.object(WalkReviewResponse, 'model_validate', side_effect=invalid_response):
            response = client.post(f'/walks/{identifier}/review', headers=self.auth(), json={'leash_pulling': 4})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(committed, [False])
        self.assertEqual(self.database_snapshot(), before)

    def test_exact_unique_constraint_mapping_and_other_database_errors(self):
        identifier = self.completed()
        class PGError(Exception):
            def __init__(self, code, name):
                self.pgcode, self.diag = code, SimpleNamespace(constraint_name=name)
        for code, name, expected in (('23505', 'uq_walk_reviews_walk', 409),
                                     ('23505', 'other_unique', 500), ('23514', 'ck_leash_pulling', 500),
                                     ('55P03', None, 409), ('40P01', None, 409), ('40001', None, 409)):
            def fail_db():
                with self.Session() as db, patch.object(db, 'commit', side_effect=IntegrityError('secret SQL', {}, PGError(code, name))):
                    yield db
            self.app.dependency_overrides[get_db] = fail_db
            response = self.post_review(identifier)
            self.assertEqual(response.status_code, expected, response.text)
            self.assertNotIn('secret', response.text)
            self.assertEqual(self.rows(WalkReview), [])

    def test_only_post_and_get_are_available_and_openapi_is_separate(self):
        identifier = self.completed()
        for method in ('put', 'patch', 'delete'):
            response = getattr(self.client, method)(f'/walks/{identifier}/review', headers=self.auth())
            self.assertEqual(response.status_code, 405)
        spec = self.app.openapi()
        self.assertEqual(set(spec['paths']['/walks/{walk_id}/review']), {'post', 'get'})
        request = spec['components']['schemas']['WalkReviewCreate']
        self.assertFalse(request['additionalProperties'])
        self.assertEqual(set(request['properties']), {*REVIEW_METRICS, 'memo'})
        self.assertEqual(set(spec['components']['schemas']['WalkReviewResponse']['properties']), REVIEW_KEYS)
        self.assertIn('미정', spec['paths']['/walks/{walk_id}/review']['post']['description'])


class WalkReviewTests(ReviewAssertions, unittest.TestCase):
    def setUp(self):
        engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        event.listen(engine, 'connect', lambda connection, _: connection.execute('PRAGMA foreign_keys=ON'))
        self.setup_walk_api(engine)


if __name__ == '__main__':
    unittest.main()
