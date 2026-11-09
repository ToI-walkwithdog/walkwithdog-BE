"""Shared synthetic fixtures for Issue #6 map/history tests (SQLite and PostgreSQL)."""
from datetime import timedelta
from decimal import Decimal

from walk_record_test_support import WalkFixture, START
from walk_request_test_support import NOW
from app.models import VolunteerProfile, Walk, WalkPoint, WalkRequest
from app.routers import walk_maps
from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool

PRIVATE_KEYS = {"user_id", "nickname", "email", "walk_id", "walk_request_id", "started_at", "ended_at",
                "recorded_at", "accuracy_m", "shelter_id", "reviewed_by", "sequence_no", "id"}


def all_keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from all_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from all_keys(child)


class WalkMapFixture(WalkFixture):
    def setup_maps(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        event.listen(engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
        self.setup_maps_on(engine)

    def setup_maps_on(self, engine):
        self.setup_walk_api(engine)
        self.app.include_router(walk_maps.router)

    def seed_walk(self, user=1, dog=1, status="COMPLETED", points=0, day=1, distance="100.00",
                  duration=600, step="0.0001"):
        start = START - timedelta(days=day)
        running = status == "IN_PROGRESS"
        with self.Session.begin() as db:
            request = WalkRequest(user_id=user, dog_id=dog, shelter_id=2 if dog == 3 else 1,
                                  requested_start_at=start, requested_end_at=start + timedelta(hours=1),
                                  status="APPROVED", reviewed_by=3, reviewed_at=NOW)
            db.add(request)
            db.flush()
            walk = Walk(walk_request_id=request.id, status=status, started_at=start,
                        ended_at=None if running else start + timedelta(seconds=duration),
                        total_distance_m=None if running else Decimal(distance),
                        duration_seconds=None if running else duration)
            db.add(walk)
            db.flush()
            db.add_all([WalkPoint(walk_id=walk.id, latitude=Decimal("37.5") + Decimal(i) * Decimal(step),
                                  longitude=Decimal("127.0"), recorded_at=start + timedelta(seconds=i),
                                  accuracy_m=Decimal(5), sequence_no=i) for i in range(points)])
            return walk.id

    def get(self, path, user=1):
        return self.client.get(path, headers=self.auth(user))

    def profile_row(self, user):
        with self.Session() as db:
            row = db.get(VolunteerProfile, user)
            return None if row is None else (row.level, row.total_walk_count, row.total_distance_m,
                                             row.total_duration_seconds, row.unique_dog_count)

    def complete_via_api(self, user=1, dog=1):
        walk = self.begin(user=user, dog=dog)
        self.advance(120)
        self.assertEqual(self.upload(walk["id"], [self.point(), self.point(1, 10, longitude="0.0001"),
                                                  self.point(2, 20, longitude="0.0002")], user).status_code, 200)
        ended = self.end(walk["id"], user)
        self.assertEqual(ended.status_code, 200, ended.text)
        return walk, ended.json()
