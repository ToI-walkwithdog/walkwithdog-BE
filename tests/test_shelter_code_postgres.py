"""Issue #87 on disposable PostgreSQL schemas: migration 030, ORM parity, committed counters, races."""
import unittest

from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from test_date_slot_booking_postgres import DateSlotMigrationFixture, IsolatedPostgres, TEST_URL
from test_shelter_code import ShelterCodeFixture
from app.database import Base
from app.models import Walk

TABLES = ("shelter_code_issuances", "shelter_code_attempts")
DIGEST = "a" * 64
ISSUE = ("INSERT INTO shelter_code_issuances (walk_request_id, volunteer_user_id, issued_by, code_digest, "
         "issued_at, expires_at) VALUES ({request}, 1, 3, '{digest}', '2030-01-07 00:00Z', '{expires}')")


def issue_sql(request=1, digest=DIGEST, expires="2030-01-07 00:10Z"):
    return ISSUE.format(request=request, digest=digest, expires=expires)


def describe(engine, table):
    with engine.connect() as connection:
        inspector = inspect(connection)
        return {
            "columns": [(c["name"], str(c["type"]), c["nullable"]) for c in inspector.get_columns(table)],
            "primary_key": inspector.get_pk_constraint(table)["constrained_columns"],
            "foreign_keys": sorted((fk["name"], tuple(fk["constrained_columns"]), fk["referred_table"],
                                    tuple(fk["referred_columns"])) for fk in inspector.get_foreign_keys(table)),
            "checks": sorted(check["name"] for check in inspector.get_check_constraints(table)),
            "indexes": sorted((i["name"], i["unique"], tuple(i["column_names"]),
                               str(i.get("dialect_options", {}).get("postgresql_where")))
                              for i in inspector.get_indexes(table)),
        }


@unittest.skipUnless(TEST_URL, "격리 PostgreSQL이 필요합니다")
class ShelterCodeMigrationTests(DateSlotMigrationFixture, unittest.TestCase):
    def migrated(self):
        engine = self.setup_predecessor()
        for name in ("028_date_slot_booking.sql", "029_reservation_cancellation.sql"):
            self.apply(engine, name)
        with engine.begin() as connection:
            self.seed_legacy(connection)
        self.apply(engine, "030_shelter_code_verification.sql")
        return engine

    def test_030_is_additive_rerunnable_and_enforces_its_constraints(self):
        engine = self.migrated()
        with engine.connect() as connection:
            before = connection.exec_driver_sql("SELECT * FROM walk_requests ORDER BY id").all()
        self.apply(engine, "030_shelter_code_verification.sql")  # a second run changes nothing
        with engine.begin() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT * FROM walk_requests ORDER BY id").all(), before)
            connection.exec_driver_sql(issue_sql())
        for statement in (
            issue_sql(),  # a second non-revoked issuance for the same reservation
            issue_sql(digest="short"),
            issue_sql(expires="2030-01-07 00:00Z"),
            issue_sql(request=99),
            "UPDATE shelter_code_issuances SET used_at = NOW()",  # used before verified
            "UPDATE shelter_code_issuances SET verified_at = NOW(), used_at = NOW(), revoked_at = NOW()",
            "INSERT INTO shelter_code_attempts VALUES (1, 1, 6, NULL, NOW())",
            "INSERT INTO shelter_code_attempts VALUES (1, 1, -1, NULL, NOW())",
            "INSERT INTO shelter_code_attempts VALUES (1, 99, 0, NULL, NOW())",
        ):
            with self.subTest(statement=statement), self.assertRaises(IntegrityError), engine.begin() as connection:
                connection.exec_driver_sql(statement)
        with engine.begin() as connection:
            connection.exec_driver_sql("UPDATE shelter_code_issuances SET revoked_at = NOW()")
            connection.exec_driver_sql(issue_sql())  # re-issuing after a revocation is allowed
            connection.exec_driver_sql("INSERT INTO shelter_code_attempts VALUES (1, 1, 5, NOW(), NOW())")
            self.assertEqual(connection.exec_driver_sql("SELECT count(*) FROM shelter_code_issuances").scalar(), 2)
        with self.assertRaises(IntegrityError), engine.begin() as connection:
            connection.exec_driver_sql("INSERT INTO shelter_code_attempts VALUES (1, 1, 0, NULL, NOW())")

    def test_checked_in_sql_matches_the_orm_tables(self):
        migrated = self.migrated()
        orm = self.isolated_engine()
        Base.metadata.create_all(orm)
        for table in TABLES:
            with self.subTest(table=table):
                self.assertEqual(describe(migrated, table), describe(orm, table))


@unittest.skipUnless(TEST_URL, "격리 PostgreSQL이 필요합니다")
class ShelterCodeConcurrencyTests(IsolatedPostgres, ShelterCodeFixture, unittest.TestCase):
    def setUp(self):
        self.setup_code_api(self.isolated_engine())
        self.request_id = self.seed_request(status="APPROVED")

    def test_failures_are_committed_before_422_and_429(self):
        code = self.issued_code(self.request_id)
        for count in range(1, 5):
            self.contract(self.verify(self.request_id, self.wrong(code)), 422, "SHELTER_CODE_INVALID")
            self.assertEqual(self.failures(self.request_id), (count, False))
        self.contract(self.verify(self.request_id, self.wrong(code)), 429, "SHELTER_CODE_RATE_LIMITED")
        self.assertEqual(self.failures(self.request_id), (5, True))

    def test_start_first_blocks_a_concurrent_reissue(self):
        self.verify(self.request_id, self.issued_code(self.request_id))
        started, reissued = self.recording_race(lambda: self.start(self.request_id, code=False),
                                                lambda: self.issue(self.request_id),
                                                hook="app.walk_service.validate_walk_start")
        self.assertEqual(started.status_code, 201, started.text)
        self.contract(reissued, 409, "WALK_REQUEST_RETRYABLE")
        self.contract(self.issue(self.request_id), 409, "WALK_REQUEST_NOT_ELIGIBLE")
        self.assertEqual([row.used_at is not None for row in self.issuances(self.request_id)], [True])

    def test_reissue_first_revokes_the_verification_of_a_late_start(self):
        self.verify(self.request_id, self.issued_code(self.request_id))
        reissued, started = self.recording_race(lambda: self.issue(self.request_id),
                                                lambda: self.start(self.request_id, code=False),
                                                hook="app.routers.shelter_codes.issue_code")
        self.contract(reissued, 201, "SHELTER_CODE_ISSUED")
        self.start_code(started, "WALK_START_RETRYABLE")
        self.start_code(self.start(self.request_id, code=False), "WALK_START_CODE_REQUIRED")
        self.assertEqual(self.rows(Walk), [])
        self.assertEqual([row.revoked_at is not None for row in self.issuances(self.request_id)], [True, False])

    def test_concurrent_starts_create_one_walk_and_use_the_code_once(self):
        self.verify(self.request_id, self.issued_code(self.request_id))
        winner, loser = self.recording_race(lambda: self.start(self.request_id, code=False),
                                            lambda: self.start(self.request_id, code=False),
                                            hook="app.walk_service.validate_walk_start")
        self.assertEqual(winner.status_code, 201, winner.text)
        self.start_code(loser, "WALK_START_RETRYABLE")
        self.start_code(self.start(self.request_id, code=False), "WALK_START_ALREADY_STARTED")
        self.assertEqual(len(self.rows(Walk)), 1)
        self.assertEqual([row.used_at is not None for row in self.issuances(self.request_id)], [True])


if __name__ == "__main__":
    unittest.main()
