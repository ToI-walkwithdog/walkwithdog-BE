"""Real PostgreSQL migration, locking, and transaction tests for date booking."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import os
from pathlib import Path
from threading import Event
import unittest
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from test_date_slot_booking import DateBookingFixture, LIST_URL, slot
from walk_request_test_support import END, START
from scripts.prepare_demo_db import PreparationError, validate_local_engine
from app.models import DogWalkDateSlot, WalkRequest
from app.walk_request_service import create_date_request
from app.walk_service import start_walk

TEST_URL = os.getenv("SIGNUP_TEST_DATABASE_URL")
MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


class IsolatedPostgres:
    def isolated_engine(self):
        admin = create_engine(TEST_URL)
        self.addCleanup(admin.dispose)
        validate_local_engine(admin)
        if not admin.url.database.startswith("toi_walk_test_"):
            raise PreparationError("새 격리 PostgreSQL 테스트 DB가 필요합니다.")
        schema = "date_booking_" + uuid4().hex
        with admin.begin() as connection:
            if inspect(connection).get_table_names(schema="public"):
                raise PreparationError("공용 스키마에 테이블이 없는 테스트 DB가 필요합니다.")
            connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')

        def remove_schema():
            with admin.begin() as connection:
                connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')

        self.addCleanup(remove_schema)
        engine = create_engine(TEST_URL, connect_args={"options": f"-csearch_path={schema} -clock_timeout=4000"})
        self.addCleanup(engine.dispose)
        return engine


@unittest.skipUnless(TEST_URL, "격리 PostgreSQL이 필요합니다")
class DateSlotConcurrencyTests(IsolatedPostgres, DateBookingFixture, unittest.TestCase):
    def setUp(self):
        self.setup_date_api(self.isolated_engine())

    def race_after_insert(self, first, second):
        inserted, release = Event(), Event()

        def held(*args):
            receipt = create_date_request(*args)
            inserted.set()
            if not release.wait(8):
                raise AssertionError("first booking was not released")
            return receipt

        with patch("app.routers.walk_requests.create_date_request", side_effect=held), ThreadPoolExecutor(1) as pool:
            writer = pool.submit(first)
            try:
                self.assertTrue(inserted.wait(8))
                conflict = second()
            finally:
                release.set()
            return writer.result(timeout=8), conflict

    def test_two_users_try_same_slot_and_only_first_transaction_reserves_it(self):
        date_id = self.create_slot()
        key1, key2 = uuid4(), uuid4()
        first, second = self.race_after_insert(lambda: self.submit(date_id, key1),
                                              lambda: self.submit(date_id, key2, actor=2))
        self.codes(first, "WALK_REQUEST_CREATED", 201)
        self.codes(second, "WALK_REQUEST_RETRYABLE")
        self.codes(self.submit(date_id, key2, actor=2), "WALK_SLOT_UNAVAILABLE")
        self.assertEqual(len(self.rows(WalkRequest)), 1)
        available = self.client.get(LIST_URL, params={"region_codes": "11680"}, headers=self.auth())
        self.assertEqual(available.status_code, 200, available.text)
        self.assertEqual(available.json()["data"]["items"], [])

    def test_same_key_retries_without_second_booking_after_transient_lock(self):
        date_id = self.create_slot()
        key = uuid4()
        first, second = self.race_after_insert(lambda: self.submit(date_id, key), lambda: self.submit(date_id, key))
        self.codes(first, "WALK_REQUEST_CREATED", 201)
        self.codes(second, "WALK_REQUEST_RETRYABLE")
        self.assertEqual(self.submit(date_id, key).json(), first.json())
        self.codes(self.submit(date_id, key, end=END + timedelta(minutes=15)), "IDEMPOTENCY_KEY_REUSED")
        self.assertEqual(len(self.rows(WalkRequest)), 1)

    def test_slot_edit_cannot_run_between_reservation_check_and_commit(self):
        date_id = self.create_slot()
        url = f"/dogs/1/date-slots/{date_id}"
        def edit():
            return self.client.patch(url, headers=self.auth(3), json={"end_time": "10:30"})
        first, blocked = self.race_after_insert(lambda: self.submit(date_id, uuid4()), edit)
        self.codes(first, "WALK_REQUEST_CREATED", 201)
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(edit().status_code, 409)
        with self.Session() as db:
            self.assertEqual(db.get(DogWalkDateSlot, date_id).end_time.isoformat(), "10:00:00")

    def test_cross_contract_overlap_releases_after_cancel_and_retains_legacy_shape(self):
        legacy = self.post()
        self.assertEqual(legacy.status_code, 201, legacy.text)
        date_id = self.create_slot()
        adjacent = self.create_slot(slot("10:00", "11:00"))
        result = self.client.get(LIST_URL, params={"region_codes": "11680"}, headers=self.auth())
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["data"]["items"][0]["date_slots"][0]["date_slot_id"], adjacent)
        self.codes(self.submit(date_id, uuid4(), actor=2), "WALK_SLOT_UNAVAILABLE")
        self.assertEqual(self.act(legacy.json()["id"], "CANCEL", user=1).status_code, 200)
        created = self.submit(date_id, uuid4(), actor=2)
        self.codes(created, "WALK_REQUEST_CREATED", 201)
        request_id = created.json()["data"]["id"]
        self.assertEqual(self.act(request_id, "APPROVE").status_code, 200)
        self.advance(0)
        started = self.start(request_id, user=2)
        self.assertEqual(started.status_code, 201, started.text)
        self.advance(60)
        self.assertEqual(self.end(started.json()["id"], user=2).status_code, 200)

    def test_concurrent_approval_then_cancellation_retries_in_order(self):
        date_id = self.create_slot()
        request_id = self.submit(date_id, uuid4()).json()["data"]["id"]
        from app.walk_request_service import transition_request
        ready, release = Event(), Event()

        def held(*args):
            row = transition_request(*args)
            if row.status == "APPROVED":
                ready.set()
                if not release.wait(8):
                    raise AssertionError("approval was not released")
            return row

        with patch("app.routers.walk_requests.transition_request", side_effect=held), ThreadPoolExecutor(2) as pool:
            approving = pool.submit(lambda: self.act(request_id, "APPROVE"))
            try:
                self.assertTrue(ready.wait(8))
                cancel = pool.submit(lambda: self.act(request_id, "CANCEL", user=1))
            finally:
                release.set()
            self.assertEqual(approving.result(timeout=8).status_code, 200)
            result = cancel.result(timeout=8)
        if result.status_code == 409:
            result = self.act(request_id, "CANCEL", user=1)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual((result.json()["status"], result.json()["reviewed_by"]), ("CANCELLED", 3))

    def test_date_slot_start_and_cancel_do_not_wait_for_actor_lock_upgrade(self):
        date_id = self.create_slot()
        request_id = self.submit(date_id, uuid4()).json()["data"]["id"]
        self.assertEqual(self.act(request_id, "APPROVE").status_code, 200)
        self.advance(0)
        started, release = Event(), Event()

        def held(*args):
            walk = start_walk(*args)
            started.set()
            if not release.wait(8):
                raise AssertionError("date-slot start was not released")
            return walk

        with patch("app.routers.walks.start_walk", side_effect=held), ThreadPoolExecutor(1) as pool:
            writer = pool.submit(lambda: self.start(request_id))
            try:
                self.assertTrue(started.wait(8))
                conflict = self.act(request_id, "CANCEL", user=1)
                self.assertEqual(conflict.status_code, 409, conflict.text)
                self.assertIn("다른 작업과 충돌", conflict.json()["detail"])
                retry = self.start(request_id)
                self.assertEqual(retry.status_code, 409, retry.text)
                self.assertIn("다른 작업과 충돌", retry.json()["detail"])
            finally:
                release.set()
            self.assertEqual(writer.result(timeout=8).status_code, 201)


@unittest.skipUnless(TEST_URL, "격리 PostgreSQL이 필요합니다")
class DateSlotMigrationTests(IsolatedPostgres, unittest.TestCase):
    def setup_predecessor(self):
        engine = self.isolated_engine()
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE users (id INTEGER PRIMARY KEY)")
            connection.exec_driver_sql("CREATE TABLE shelters (id INTEGER PRIMARY KEY)")
            connection.exec_driver_sql("CREATE TABLE dogs (id INTEGER PRIMARY KEY)")
        for name in ("007_walk_domain.sql", "009_add_f03_walk_register.sql", "011_f05_walk_approval.sql",
                     "012_f06_walk_tracking.sql", "013_f07_incident_report.sql", "022_regions.sql"):
            self.apply(engine, name)
        return engine

    @staticmethod
    def apply(engine, name):
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.exec_driver_sql((MIGRATIONS / name).read_text(encoding="utf-8"))

    @staticmethod
    def seed_legacy(connection):
        connection.exec_driver_sql("INSERT INTO users VALUES (1), (2), (3); INSERT INTO shelters VALUES (1);"
                                   "INSERT INTO dogs (id) VALUES (1), (2)")
        connection.exec_driver_sql("INSERT INTO dog_walk_date_slots (id, dog_id, slot_date, start_time, end_time, duration_minutes)"
                                   " VALUES (1, 1, '2030-01-07', '09:00', '10:00', 30)")
        connection.exec_driver_sql("INSERT INTO walk_requests (id, user_id, dog_id, shelter_id, requested_start_at, "
                                   "requested_end_at, status, reviewed_by, reviewed_at) VALUES"
                                   " (1, 1, 1, 1, '2030-01-07 00:00Z', '2030-01-07 01:00Z', 'APPROVED', 3, NOW())")

    def test_028_applies_to_empty_predecessor_and_sets_fk_and_partial_index(self):
        engine = self.setup_predecessor()
        self.apply(engine, "028_date_slot_booking.sql")
        with engine.begin() as connection:
            self.seed_legacy(connection)
            self.assertIsNone(connection.execute(text("SELECT date_slot_id FROM walk_requests")).scalar_one())
            self.assertTrue(connection.execute(text("SELECT is_active FROM dog_walk_date_slots")).scalar_one())
            indexes = inspect(connection).get_indexes("walk_requests")
            self.assertTrue(any(i["name"] == "uq_walk_requests_occupied_date_slot" and i["unique"] for i in indexes))
            connection.exec_driver_sql("UPDATE walk_requests SET date_slot_id = 1 WHERE id = 1")
        with self.assertRaises(IntegrityError), engine.begin() as connection:
            connection.exec_driver_sql("INSERT INTO walk_requests (id, user_id, dog_id, shelter_id, requested_start_at, "
                                       "requested_end_at, date_slot_id) VALUES"
                                       " (2, 2, 1, 1, '2030-01-07 00:00Z', '2030-01-07 01:00Z', 1)")

    def test_028_retains_manually_linked_010_history_and_prevents_cascading_deletion(self):
        engine = self.setup_predecessor()
        with engine.begin() as connection:
            self.seed_legacy(connection)
            connection.exec_driver_sql("ALTER TABLE walk_requests ADD COLUMN date_slot_id INTEGER"
                                       " REFERENCES dog_walk_date_slots(id) ON DELETE CASCADE")
            connection.exec_driver_sql("CREATE INDEX idx_walk_requests_date_slot ON walk_requests(date_slot_id)")
            connection.exec_driver_sql("UPDATE walk_requests SET date_slot_id = 1 WHERE id = 1")
            before = connection.execute(text("SELECT requested_start_at, requested_end_at, reviewed_by, reviewed_at,"
                                             "status, date_slot_id FROM walk_requests")).one()
        self.apply(engine, "028_date_slot_booking.sql")
        with self.assertRaises(IntegrityError), engine.begin() as connection:
            connection.exec_driver_sql("DELETE FROM dog_walk_date_slots WHERE id = 1")
        with engine.begin() as connection:
            after = connection.execute(text("SELECT requested_start_at, requested_end_at, reviewed_by, reviewed_at,"
                                            "status, date_slot_id FROM walk_requests")).one()
        self.assertEqual(before, after)
