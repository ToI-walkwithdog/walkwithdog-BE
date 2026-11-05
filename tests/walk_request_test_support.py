"""Shared synthetic fixtures for SQLite and disposable PostgreSQL request tests."""
from datetime import datetime, time, timedelta, timezone
import os
from unittest.mock import patch

with patch.dict(os.environ, {"DATABASE_URL": "sqlite://"}):
    from app.auth import token_digest
    from app.database import Base, get_db
    from app.models import (
        AuthSession, Dog, DogWalkSetting, DogWalkSlot, Shelter, User, UserRole,
        VolunteerProfile, WalkRequest,
    )
    from app.routers import dogs, shelters, walk_requests

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

NOW = datetime(2030, 1, 6, tzinfo=timezone.utc)  # Sunday; next Seoul Monday is Jan 7.
START = datetime(2030, 1, 7, tzinfo=timezone.utc)  # Monday 09:00 Seoul.
END = START + timedelta(hours=1)


class RequestFixture:
    def setup_api(self, engine):
        self.engine = engine
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)
        self.app = FastAPI()
        for router in (dogs.router, shelters.router, walk_requests.router):
            self.app.include_router(router)

        def test_db():
            with self.Session() as db:
                yield db

        for dependency in (get_db, dogs.get_db, shelters.get_db):
            self.app.dependency_overrides[dependency] = test_db
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.clock = patch("app.walk_request_service.utc_now", return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        with self.Session.begin() as db:
            db.add_all([Shelter(id=1, name="첫 보호소"), Shelter(id=2, name="다른 보호소"), Shelter(id=3, name="빈 보호소")])
            db.flush()
            for identifier, role, shelter_id in (
                (1, UserRole.USER, None), (2, UserRole.USER, None),
                (3, UserRole.SHELTER, 1), (4, UserRole.SHELTER, 2),
                (5, UserRole.ADMIN, None), (6, UserRole.USER, None), (7, UserRole.SHELTER, None),
            ):
                db.add(User(id=identifier, email=f"req-{identifier}@example.com", nickname=f"사용자{identifier}",
                            role=role, shelter_id=shelter_id))
            for identifier in range(1, 10):
                db.add(Dog(id=identifier, name=f"강아지{identifier}",
                           shelter_id=None if identifier == 4 else (2 if identifier == 3 else 1), energy_level=2))
            db.flush()
            for identifier in range(1, 8):
                db.add(AuthSession(token_hash=token_digest(f"req-{identifier}"), user_id=identifier,
                                   expires_at=datetime(2035, 1, 1, tzinfo=timezone.utc)))
            db.add(VolunteerProfile(user_id=6, level=2, total_walk_count=3, unique_dog_count=2))
            for dog_id in (1, 2, 3, 4, 6, 7, 8, 9):
                setting = DogWalkSetting(dog_id=dog_id, walkable=dog_id != 6,
                                         difficulty="EXPERIENCED" if dog_id == 2 else "BEGINNER",
                                         recommended_duration_minutes=30)
                db.add(setting)
                db.flush()
                if dog_id == 7:
                    continue
                ranges = [(time(9), time(10)), (time(10), time(12))] if dog_id == 8 else [(time(9), time(12))]
                if dog_id == 9:
                    ranges = [(time(0), time(23, 59, 59))]
                db.add_all([DogWalkSlot(walk_setting_id=setting.id, day_of_week=0, start_time=start, end_time=end)
                            for start, end in ranges])

    @staticmethod
    def auth(user=1):
        return {"Authorization": f"Bearer req-{user}"} if user else {}

    def post(self, user=1, dog=1, start=START, end=END, **extra):
        body = {"dog_id": dog, "requested_start_at": start.isoformat() if isinstance(start, datetime) else start,
                "requested_end_at": end.isoformat() if isinstance(end, datetime) else end, **extra}
        return self.client.post("/walk-requests", headers=self.auth(user), json=body)

    def create(self, **kwargs):
        response = self.post(**kwargs)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def act(self, identifier, action, user=3, **extra):
        return self.client.patch(f"/walk-requests/{identifier}", headers=self.auth(user), json={"action": action, **extra})

    def seed_request(self, user=1, dog=1, status="PENDING", start=START, end=END):
        with self.Session.begin() as db:
            request = WalkRequest(user_id=user, dog_id=dog, shelter_id=2 if dog == 3 else 1,
                                  requested_start_at=start, requested_end_at=end, status=status)
            if status in ("APPROVED", "REJECTED"):
                request.reviewed_by, request.reviewed_at = 3, NOW
            if status == "REJECTED":
                request.rejection_reason = "기존 거절"
            db.add(request)
            db.flush()
            return request.id

    def rows(self, model):
        with self.Session() as db:
            return [tuple(getattr(row, c.name) for c in model.__table__.columns)
                    for row in db.query(model).order_by(*model.__table__.primary_key.columns)]
