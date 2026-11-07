"""PR #15 review regressions, run unchanged on SQLite and PostgreSQL."""
from datetime import timedelta
from decimal import Decimal
from math import degrees
from unittest.mock import patch
from sqlalchemy.exc import SQLAlchemyError

from app.gps_geometry import EARTH_RADIUS_M, distance_m
from app.models import VolunteerProfile, Walk, WalkPoint
from app.volunteer_stats import refresh_volunteer_profile
from app.walk_map_service import Point, PUBLIC_TRIM_M
from scripts.backfill_volunteer_stats import rebuild_profiles
from walk_record_test_support import START

MOTION_FIELDS = ('moving_duration_seconds', 'stopped_duration_seconds', 'unknown_duration_seconds', 'moving_distance_m')


class MapIntegrationAssertions:
    def test_motion_metrics_reach_map_history_and_elapsed_volunteer_time(self):
        walk = self.begin()
        self.advance(90)
        points = [self.point(), self.point(1, 10, longitude='.0001'), self.point(2, 20, longitude='.0002')]
        points += [self.point(i + 2, 20 + i * 10, longitude='.0002') for i in range(1, 7)]
        self.assertEqual(self.upload(walk['id'], points).status_code, 200)
        self.assertEqual(self.end(walk['id']).status_code, 200)
        raw_before = self.rows(WalkPoint)
        detail = self.detail(walk['id']).json()['walk']
        self.assertEqual(tuple(detail[k] for k in MOTION_FIELDS), (20, 60, 10, '22.24'))
        self.assertEqual(detail['average_pace'], '899.28')
        for path in ('/users/1/map', '/users/1/walks'):
            item = self.get(path).json()['walks'][0]
            for key in (*MOTION_FIELDS, 'average_pace'):
                self.assertEqual(item[key], detail[key])
        self.assertEqual(self.get('/volunteers/me').json()['total_duration_seconds'], 90)
        self.assertEqual(self.rows(WalkPoint), raw_before)

    def test_map_history_serialize_legacy_null_metrics_and_document_pace(self):
        self.seed_walk(points=3)
        for path in ('/users/1/map', '/users/1/walks'):
            item = self.get(path).json()['walks'][0]
            self.assertEqual({k: item[k] for k in MOTION_FIELDS}, dict.fromkeys(MOTION_FIELDS))
        schemas = self.app.openapi()['components']['schemas']
        for name in ('UserMapWalk', 'UserWalkItem'):
            self.assertIn('moving_duration_seconds', schemas[name]['properties']['average_pace']['description'])
            self.assertIn('레거시', schemas[name]['properties']['average_pace']['description'])

    def test_statistics_failure_rolls_back_gps_metrics_end_and_profile_together(self):
        walk = self.begin()
        self.advance(30)
        self.upload(walk['id'], [self.point(), self.point(1, 10, longitude='.0001'), self.point(2, 20, longitude='.0002')])
        before = self.rows(VolunteerProfile)
        def fail_after_refresh(db, user_id):
            refresh_volunteer_profile(db, user_id)
            raise SQLAlchemyError('synthetic failure after statistics flush')
        with patch('app.walk_service.refresh_volunteer_profile', side_effect=fail_after_refresh):
            self.assertEqual(self.end(walk['id']).status_code, 500)
        data = self.detail(walk['id']).json()['walk']
        self.assertEqual((data['status'], data['ended_at'], data['average_pace']), ('IN_PROGRESS', None, None))
        self.assertEqual({k: data[k] for k in MOTION_FIELDS}, dict.fromkeys(MOTION_FIELDS))
        self.assertEqual(self.rows(VolunteerProfile), before)
        self.assertEqual(self.end(walk['id']).status_code, 200)
        self.assertEqual(self.profile_row(1)[1], 1)

    def test_level_one_experienced_walk_requires_shelter_approval_and_revalidation(self):
        self.advance(-60)
        request = self.create(dog=2)
        self.assertEqual(request['status'], 'PENDING')
        self.assertEqual(self.start(request['id']).status_code, 409)
        self.assertEqual(self.act(request['id'], 'APPROVE', user=4).status_code, 403)
        self.assertEqual(self.act(request['id'], 'APPROVE', user=3).status_code, 200)
        self.advance(0)
        started = self.start(request['id'])
        self.assertEqual(started.status_code, 201, started.text)
        self.assertEqual(self.end(started.json()['id']).status_code, 200)
        self.assertEqual(self.profile_row(1)[:2], (1, 1))

    def test_backfill_dry_run_apply_idempotence_and_legacy_level_preservation(self):
        for day, dog in enumerate((1, 1, 2), 1):
            self.seed_walk(dog=dog, day=day, duration=600, distance='100.25')
        for day, state in enumerate(('INTERRUPTED', 'CANCELLED', 'IN_PROGRESS'), 4):
            self.seed_walk(day=day, status=state)
        profiles_before, walks_before = self.rows(VolunteerProfile), self.rows(Walk)
        preview = rebuild_profiles(self.Session)
        self.assertFalse(preview['applied'])
        self.assertEqual(self.rows(VolunteerProfile), profiles_before)
        first = rebuild_profiles(self.Session, apply=True)
        self.assertTrue(first['applied'])
        self.assertEqual(self.profile_row(1), (1, 3, Decimal('300.75'), 1800, 2))
        self.assertEqual(self.profile_row(6), (2, 0, Decimal('0.00'), 0, 0))
        after = self.rows(VolunteerProfile)
        rebuild_profiles(self.Session, apply=True)
        self.assertEqual(self.rows(VolunteerProfile), after)
        self.assertEqual(self.rows(Walk), walks_before)

    def test_backfill_failure_rolls_back_user_and_can_be_retried(self):
        self.seed_walk()
        before = self.rows(VolunteerProfile)
        def fail(db, user_id):
            refresh_volunteer_profile(db, user_id)
            raise SQLAlchemyError('synthetic backfill failure')
        with patch('scripts.backfill_volunteer_stats.refresh_volunteer_profile', side_effect=fail):
            with self.assertRaises(SQLAlchemyError):
                rebuild_profiles(self.Session, apply=True, user_id=1)
        self.assertEqual(self.rows(VolunteerProfile), before)
        rebuild_profiles(self.Session, apply=True, user_id=1)
        self.assertEqual(self.profile_row(1)[1], 1)

    def test_public_map_hides_return_to_start_without_modifying_owner_route(self):
        identifier = self.seed_walk(points=0)
        offsets = [0, 100, 200, 300, 400, 300, 200, 100, 0, 100, 200, 300, 400, 500, 600, 700, 800]
        with self.Session.begin() as db:
            start = db.get(Walk, identifier).started_at
            db.add_all([WalkPoint(walk_id=identifier, latitude=Decimal(str(37.5 + degrees(x / EARTH_RADIUS_M))),
                                  longitude=127, sequence_no=i, accuracy_m=5,
                                  recorded_at=start + timedelta(seconds=i * 30)) for i, x in enumerate(offsets)])
        before = self.rows(WalkPoint)
        owner = self.get('/users/1/map').json()['walks'][0]['route']
        self.assertEqual(len(owner), len(offsets))
        self.assertEqual(self.get('/dogs/1/map', user=2).json()['walks'][0]['route'], [])
        self.assertEqual(self.rows(WalkPoint), before)

    def test_public_rounded_points_and_connectors_stay_outside_endpoint_zones(self):
        self.seed_walk(points=100)
        owner = self.get('/users/1/map').json()['walks'][0]['route']
        public = self.get('/dogs/1/map', user=2).json()['walks'][0]['route']
        self.assertGreater(len(public), 1)
        points = [Point(Decimal(p['latitude']), Decimal(p['longitude'])) for p in public]
        anchors = [Point(Decimal(p['latitude']), Decimal(p['longitude'])) for p in (owner[0], owner[-1])]
        for anchor in anchors:
            for point in points:
                self.assertGreater(distance_m(anchor, point), PUBLIC_TRIM_M)
            for first, second in zip(points, points[1:]):
                lower_bound = (distance_m(anchor, first) + distance_m(anchor, second) - distance_m(first, second)) / 2
                self.assertGreater(lower_bound, PUBLIC_TRIM_M)
