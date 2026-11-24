"""PostgreSQL cancellation migration and write contention using disposable schemas."""
import unittest
from uuid import uuid4

from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from test_date_slot_booking import DateBookingFixture
from test_date_slot_booking_postgres import DateSlotMigrationFixture, IsolatedPostgres, TEST_URL
from walk_request_test_support import NOW


@unittest.skipUnless(TEST_URL, "격리 PostgreSQL이 필요합니다")
class ReservationConcurrencyTests(IsolatedPostgres, DateBookingFixture, unittest.TestCase):
    def setUp(self):
        self.setup_date_api(self.isolated_engine())

    def test_cancellation_wins_over_approval_and_frees_date_slot(self):
        slot_id = self.create_slot()
        first_key = uuid4()
        first_receipt = self.submit(slot_id, first_key).json()
        request_id = first_receipt["data"]["id"]
        cancelled, conflict = self.recording_race(
            lambda: self.act(request_id, "CANCEL", user=1, cancel_reason_code="CANNOT_VISIT"),
            lambda: self.act(request_id, "APPROVE"),
            hook="app.routers.walk_requests.transition_request",
        )
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertEqual(conflict.status_code, 409, conflict.text)
        self.assertEqual(self.act(request_id, "APPROVE").status_code, 409)
        self.assertEqual(self.submit(slot_id, first_key).json(), first_receipt)
        again = self.submit(slot_id, uuid4(), actor=2)
        self.assertEqual(again.status_code, 201, again.text)
        history = self.client.get(f"/walk-requests/{request_id}", headers=self.auth()).json()
        self.assertEqual((history["status"], history["date_slot_id"], history["cancel_reason_code"]),
                         ("CANCELLED", slot_id, "CANNOT_VISIT"))
        self.assertEqual(history["requested_start_at"], first_receipt["data"]["requested_start_at"])
        self.assertEqual(history["requested_end_at"], first_receipt["data"]["requested_end_at"])

    def test_cancellation_wins_over_walk_start_without_waiting(self):
        slot_id = self.create_slot()
        request_id = self.submit(slot_id, uuid4()).json()["data"]["id"]
        self.assertEqual(self.act(request_id, "APPROVE").status_code, 200)

        def start_during_cancel():
            self.advance(0)
            return self.start(request_id)

        cancelled, competing_start = self.recording_race(
            lambda: self.act(request_id, "CANCEL", user=5),
            start_during_cancel,
            hook="app.routers.walk_requests.transition_request",
        )
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertEqual(cancelled.json()["cancelled_actor_role"], "ADMIN")
        self.assertEqual(cancelled.json()["cancelled_at"], NOW.isoformat().replace("+00:00", "Z"))
        self.assertEqual(competing_start.status_code, 409, competing_start.text)
        self.assertEqual(self.start(request_id).status_code, 409)
        self.assertEqual(self.client.get("/walks/active", headers=self.auth()).status_code, 404)


@unittest.skipUnless(TEST_URL, "격리 PostgreSQL이 필요합니다")
class ReservationCancellationMigrationTests(DateSlotMigrationFixture, unittest.TestCase):
    def test_029_preserves_legacy_cancellation_and_enforces_new_history(self):
        engine = self.setup_predecessor()
        self.apply(engine, "028_date_slot_booking.sql")
        with engine.begin() as connection:
            self.seed_legacy(connection)
            connection.exec_driver_sql("UPDATE walk_requests SET status = 'CANCELLED', date_slot_id = 1 WHERE id = 1")
        self.apply(engine, "029_reservation_cancellation.sql")
        with engine.connect() as connection:
            columns = {column["name"]: column for column in inspect(connection).get_columns("walk_requests")}
            names = ("cancel_reason_code", "cancelled_at", "cancelled_by", "cancelled_actor_role")
            self.assertTrue(all(columns[name]["nullable"] for name in names))
            legacy = connection.exec_driver_sql(
                "SELECT status, date_slot_id, reviewed_by, cancel_reason_code, cancelled_at, "
                "cancelled_by, cancelled_actor_role FROM walk_requests WHERE id = 1"
            ).one()
            self.assertEqual(tuple(legacy), ("CANCELLED", 1, 3, None, None, None, None))
        for update in (
            "cancel_reason_code = 'CANNOT_VISIT'",
            "cancelled_at = NOW(), cancelled_by = 99, cancelled_actor_role = 'ADMIN'",
            "cancelled_at = NOW(), cancelled_by = 1, cancelled_actor_role = 'OTHER'",
            "cancelled_at = NOW(), cancelled_by = 1, cancelled_actor_role = 'USER', cancel_reason_code = 'OTHER'",
            "status = 'APPROVED', cancelled_at = NOW(), cancelled_by = 1, cancelled_actor_role = 'USER'",
        ):
            with self.subTest(update=update), self.assertRaises(IntegrityError), engine.begin() as connection:
                connection.exec_driver_sql(f"UPDATE walk_requests SET {update} WHERE id = 1")
        with engine.begin() as connection:
            valid = connection.exec_driver_sql(
                "UPDATE walk_requests SET cancelled_at = NOW(), cancelled_by = 1, cancelled_actor_role = 'USER' "
                "WHERE id = 1 RETURNING cancelled_by, reviewed_by"
            ).one()
            self.assertEqual(tuple(valid), (1, 3))
