"""Issue #89 on disposable PostgreSQL schemas: migration 031, ORM parity, committed settlement,
concurrent marker and incident retries, end vs marker, and the incident SAVEPOINT."""
import unittest
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from test_date_slot_booking_postgres import DateSlotMigrationFixture, IsolatedPostgres, TEST_URL
from test_walk_recording_completion import CompletionFixture, utc
from walk_request_test_support import START
from app import walk_service
from app.database import Base

MARKER = ("INSERT INTO walk_markers (walk_id, marker_type, latitude, longitude, recorded_at{cols}) "
          "VALUES (1, 'SNIFF', 0, 0, '2030-01-07 00:01Z'{vals})")


def marker_sql(key=None, fingerprint=None):
    if key is None and fingerprint is None:
        return MARKER.format(cols="", vals="")
    cols, vals = [], []
    if key is not None:
        cols.append("client_marker_id"), vals.append(f"'{key}'")
    if fingerprint is not None:
        cols.append("request_fingerprint"), vals.append(f"'{fingerprint}'")
    return MARKER.format(cols=", " + ", ".join(cols), vals=", " + ", ".join(vals))


@unittest.skipUnless(TEST_URL, "격리 PostgreSQL이 필요합니다")
class MarkerMigrationTests(DateSlotMigrationFixture, unittest.TestCase):
    def migrated(self):
        engine = self.setup_predecessor()
        for name in ("028_date_slot_booking.sql", "029_reservation_cancellation.sql",
                     "030_shelter_code_verification.sql"):
            self.apply(engine, name)
        with engine.begin() as connection:
            self.seed_legacy(connection)
            connection.exec_driver_sql("INSERT INTO walks (id, walk_request_id, started_at) VALUES (1, 1, NOW())")
            connection.exec_driver_sql(marker_sql())  # a legacy marker before 031
        self.apply(engine, "031_walk_marker_idempotency.sql")
        return engine

    def test_031_is_additive_rerunnable_and_enforces_the_key_rules(self):
        engine = self.migrated()
        self.apply(engine, "031_walk_marker_idempotency.sql")  # a second run changes nothing
        key = uuid4()
        with engine.begin() as connection:
            legacy = connection.exec_driver_sql(
                "SELECT client_marker_id, request_fingerprint, position_recorded_at FROM walk_markers").one()
            self.assertEqual(tuple(legacy), (None, None, None))
            connection.exec_driver_sql(marker_sql())  # keyless markers may repeat
            connection.exec_driver_sql(marker_sql(key, "a" * 64))
        for statement in (marker_sql(key, "b" * 64), marker_sql(uuid4()), marker_sql(fingerprint="c" * 64)):
            with self.subTest(statement=statement), self.assertRaises(IntegrityError), engine.begin() as connection:
                connection.exec_driver_sql(statement)

    def test_checked_in_sql_matches_the_orm_marker_columns(self):
        migrated = self.migrated()
        orm = self.isolated_engine()
        Base.metadata.create_all(orm)

        def describe(engine):
            with engine.connect() as connection:
                inspector = inspect(connection)
                columns = {c["name"]: (str(c["type"]), c["nullable"]) for c in inspector.get_columns("walk_markers")}
                indexes = {i["name"]: (i["unique"], tuple(i["column_names"]),
                                       str(i.get("dialect_options", {}).get("postgresql_where")))
                           for i in inspector.get_indexes("walk_markers")}
                checks = {c["name"] for c in inspector.get_check_constraints("walk_markers")}
            return ({name: columns[name] for name in ("client_marker_id", "request_fingerprint", "position_recorded_at")},
                    indexes["uq_walk_markers_client_marker_id"], "ck_marker_client_key" in checks)

        self.assertEqual(describe(migrated), describe(orm))


@unittest.skipUnless(TEST_URL, "격리 PostgreSQL이 필요합니다")
class WalkRecordingCompletionPostgresTests(IsolatedPostgres, CompletionFixture, unittest.TestCase):
    def setUp(self):
        self.setup_completion_api(self.isolated_engine())
        self.walk_id = self.begin()["id"]

    def test_active_404_commits_the_auto_timeout_on_postgres(self):
        self.advance(3601)
        self.assertEqual(self.active().status_code, 404)
        self.assertEqual(self.walk_row(self.walk_id), ("COMPLETED", "AUTO_TIMEOUT", utc(START)))

    def test_concurrent_marker_retries_store_one_marker(self):
        self.advance(30)
        body = {"client_marker_id": str(uuid4()), "latitude": 0, "longitude": 0, "accuracy_m": 5}
        first, second = self.recording_race(lambda: self.tap(self.walk_id, seconds=30, **body),
                                            lambda: self.tap(self.walk_id, seconds=30, **body))
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(second.status_code, 409, second.text)  # the walk row is locked: retry later
        retry = self.tap(self.walk_id, seconds=30, **body)
        self.assertEqual((retry.status_code, retry.json()["id"]), (200, first.json()["id"]))
        self.assertEqual(len(self.markers()), 1)

    def test_concurrent_incident_retries_store_one_incident(self):
        self.advance(30)
        body = {"client_report_id": str(uuid4())}
        first, second = self.recording_race(lambda: self.report(self.walk_id, seconds=20, **body),
                                            lambda: self.report(self.walk_id, seconds=20, **body),
                                            hook="app.walk_service.locked_walk_for_incident")
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(second.status_code, 409, second.text)
        retry = self.report(self.walk_id, seconds=20, **body)
        self.assertEqual((retry.status_code, retry.json()["id"]), (200, first.json()["id"]))
        self.assertEqual(len(self.incidents()), 1)

    def test_end_first_refuses_a_concurrent_new_marker(self):
        self.advance(120)
        self.upload(self.walk_id, [self.point(0, 100, accuracy_m=5)])
        ended, marker = self.recording_race(lambda: self.end(self.walk_id),
                                            lambda: self.tap(self.walk_id, seconds=110, client_marker_id=str(uuid4())))
        self.assertEqual(ended.status_code, 200, ended.text)
        self.assertEqual(marker.status_code, 409, marker.text)
        self.assertEqual(self.tap(self.walk_id, seconds=110, client_marker_id=str(uuid4())).status_code, 409)
        self.assertEqual(self.markers(), [])

    def test_incident_key_race_rolls_back_only_the_savepoint(self):
        """A unique race on client_report_id must leave the transaction usable for the re-read."""
        self.advance(30)
        body = {"client_report_id": str(uuid4())}
        stored = self.report(self.walk_id, seconds=20, **body)
        self.assertEqual(stored.status_code, 201, stored.text)
        real = walk_service.stored_report
        calls = []

        def miss_twice(db, actor, payload):  # both pre-insert lookups miss, as in a real race
            calls.append(1)
            return None if len(calls) <= 2 else real(db, actor, payload)

        with patch("app.walk_service.stored_report", side_effect=miss_twice):
            # confirm_duplicate skips the 180 s check, as an uncommitted racing row would be invisible to it
            replay = self.report(self.walk_id, seconds=20, confirm_duplicate=True, **body)
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(replay.json()["id"], stored.json()["id"])
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(self.incidents()), 1)


if __name__ == "__main__":
    unittest.main()
