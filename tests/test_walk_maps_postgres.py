"""Opt-in real PostgreSQL checks for Issue #6: SQL sampling/aggregation and statistic races."""
import os
import unittest
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from walk_map_test_support import WalkMapFixture
from app.models import VolunteerProfile
from app.volunteer_stats import refresh_volunteer_profile
from app.walk_map_service import OWNER_MAX_ROUTE_POINTS
from sqlalchemy import create_engine, inspect
from scripts.prepare_demo_db import PreparationError, validate_local_engine

TEST_URL = os.getenv("WALK_MAPS_TEST_DATABASE_URL")


@unittest.skipUnless(TEST_URL, "WALK_MAPS_TEST_DATABASE_URL의 빈 격리 PostgreSQL이 필요합니다")
class WalkMapPostgresTests(WalkMapFixture, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.admin = create_engine(TEST_URL)
        validate_local_engine(cls.admin)
        if not cls.admin.url.database.startswith("toi_walk_test_"):
            raise PreparationError("테스트 DB 이름은 toi_walk_test_로 시작해야 합니다.")
        with cls.admin.connect() as connection:
            inspector = inspect(connection)
            custom = [s for s in inspector.get_schema_names() if s not in ("public", "information_schema") and not s.startswith("pg_")]
            if inspector.get_table_names() or custom:
                raise PreparationError("새로운 빈 격리 테스트 DB만 허용합니다.")

    @classmethod
    def tearDownClass(cls):
        cls.admin.dispose()

    def setUp(self):
        schema = "maps_" + uuid4().hex
        with self.admin.begin() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        self.setup_maps_on(create_engine(TEST_URL, connect_args={"options": f"-csearch_path={schema}"}))

    def test_sql_sampling_keeps_endpoints_and_aggregates_on_postgresql(self):
        self.seed_walk(user=1, dog=1, points=450, day=1, distance="100.00", duration=600)
        self.seed_walk(user=1, dog=1, points=100, day=2, distance="50.25", duration=300)
        self.seed_walk(user=2, dog=1, points=100, day=3, distance="10.00", duration=60)
        self.seed_walk(user=1, dog=1, status="INTERRUPTED", points=100, day=4)
        owner = self.get("/users/1/map").json()
        self.assertEqual(owner["total"], 2)
        by_count = {w["point_count"]: w for w in owner["walks"]}
        long_route = by_count[450]["route"]
        self.assertLessEqual(len(long_route), OWNER_MAX_ROUTE_POINTS + 1)
        self.assertEqual(Decimal(long_route[0]["latitude"]), Decimal("37.5"))
        self.assertEqual(Decimal(long_route[-1]["latitude"]), Decimal("37.5") + Decimal(449) * Decimal("0.0001"))
        self.assertEqual(len(by_count[100]["route"]), 100)
        public = self.get("/dogs/1/map", 2).json()
        self.assertEqual(public["stats"], {"walk_count": 3, "total_distance_m": "160.25",
                                           "total_duration_seconds": 960, "volunteer_count": 2})
        self.assertEqual(len(public["walks"]), 3)

    def test_history_and_profile_match_on_postgresql(self):
        self.complete_via_api()
        profile, me = self.get("/users/1/profile").json(), self.get("/volunteers/me").json()
        profile.pop("nickname")
        self.assertEqual(profile, me)
        self.assertEqual((me["total_walk_count"], me["total_distance_m"], me["unique_dog_count"]), (1, "22.24", 1))
        history = self.get("/users/1/walks").json()
        self.assertEqual([(w["status"], w["counted_as_volunteer_record"]) for w in history["walks"]], [("COMPLETED", True)])

    def test_concurrent_double_end_counts_the_walk_exactly_once(self):
        walk = self.begin()
        self.advance(120)
        winner, loser = self.recording_race(lambda: self.end(walk["id"]), lambda: self.end(walk["id"]))
        self.assertEqual((winner.status_code, loser.status_code), (200, 409))
        self.assertEqual(self.profile_row(1), (1, 1, Decimal("0.00"), 120, 1))
        self.assertEqual(self.end(walk["id"]).status_code, 409)
        self.assertEqual(self.profile_row(1), (1, 1, Decimal("0.00"), 120, 1))

    def test_end_and_safety_interruption_race_never_counts_an_interrupted_walk(self):
        walk = self.begin()
        self.advance(120)
        winner, loser = self.recording_race(lambda: self.end(walk["id"], user=3, status="INTERRUPTED"),
                                            lambda: self.end(walk["id"]))
        self.assertEqual((winner.status_code, loser.status_code), (200, 409))
        self.assertIsNone(self.profile_row(1))

    def test_refresh_on_postgresql_is_idempotent_and_promotes_only_when_rule_is_met(self):
        for day, dog in enumerate((1, 1, 9, 8, 1), start=1):
            self.seed_walk(dog=dog, day=day)
        with self.Session.begin() as db:
            first = refresh_volunteer_profile(db, 1)
            snapshot = (first.level, first.total_walk_count, first.total_distance_m, first.unique_dog_count)
        with self.Session.begin() as db:
            again = refresh_volunteer_profile(db, 1)
            self.assertEqual((again.level, again.total_walk_count, again.total_distance_m, again.unique_dog_count), snapshot)
        self.assertEqual(snapshot, (2, 5, Decimal("500.00"), 3))


if __name__ == "__main__":
    unittest.main()
