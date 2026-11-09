"""Request policies and authenticated API behavior; no configured DB is accessed."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from walk_request_test_support import RequestFixture, NOW, START, END
from app.models import Dog, DogWalkSetting, User, UserRole, VolunteerProfile, Walk, WalkRequest
from app.walk_request_service import validate_walk_start
from app.database import get_db
from fastapi import HTTPException
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.pool import StaticPool


class WalkRequestTests(RequestFixture, unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        event.listen(engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
        self.setup_api(engine)

    def test_level_policy_initial_profile_and_no_automatic_promotion(self):
        result = self.create()
        self.assertEqual(result["status"], "PENDING")
        self.assertEqual((result["user_id"], result["shelter_id"]), (1, 1))
        self.assertIsNone(result["reviewed_by"])
        self.assertIsNone(result["reviewed_at"])
        self.assertIsNone(result["rejection_reason"])
        with self.Session() as db:
            profile = db.get(VolunteerProfile, 1)
            self.assertEqual((profile.level, profile.total_walk_count, profile.total_distance_m,
                              profile.total_duration_seconds, profile.unique_dog_count), (1, 0, 0, 0, 0))
        self.assertEqual(self.act(result["id"], "APPROVE").status_code, 200)
        with self.Session() as db:
            self.assertEqual(db.get(VolunteerProfile, 1).total_walk_count, 0)

    def test_existing_advanced_profile_reused_without_changing_statistics(self):
        before = self.rows(VolunteerProfile)
        self.create(user=6, dog=2)
        self.assertEqual(self.rows(VolunteerProfile), before)

    def test_level_one_can_apply_for_experienced_dog_but_still_needs_approval(self):
        result = self.create(dog=2)
        self.assertEqual(result['status'], 'PENDING')
        with self.Session() as db:
            with self.assertRaises(HTTPException) as error:
                validate_walk_start(db, result['id'], db.get(User, 1))
            self.assertEqual(error.exception.status_code, 409)
            self.assertEqual(db.get(VolunteerProfile, 1).level, 1)
        self.assertEqual(self.act(result['id'], 'APPROVE').status_code, 200)

    def test_profile_and_history_reads_never_create_records(self):
        before = self.rows(VolunteerProfile)
        response = self.client.get("/volunteers/me", headers=self.auth())
        self.assertEqual(response.status_code, 200)
        self.assertEqual((response.json()["user_id"], response.json()["level"], response.json()["total_walk_count"]), (1, 1, 0))
        self.assertEqual(self.client.get("/walk-requests/my", headers=self.auth()).json(), [])
        self.assertEqual(self.rows(VolunteerProfile), before)
        advanced = self.client.get("/volunteers/me", headers=self.auth(6)).json()
        self.assertEqual(advanced["level"], 2)
        self.assertEqual(advanced["total_walk_count"], 3)
        self.assertEqual(self.client.patch("/volunteers/me", headers=self.auth(), json={"level": 2}).status_code, 405)

    def test_only_regular_users_can_apply_or_read_volunteer_profile(self):
        for user in (None, 3, 4, 5, 7):
            expected = 401 if user is None else 403
            self.assertEqual(self.post(user=user).status_code, expected)
            self.assertEqual(self.client.get("/volunteers/me", headers=self.auth(user)).status_code, expected)

    def test_client_cannot_supply_identity_status_or_level(self):
        for field, value in (("user_id", 2), ("shelter_id", 2), ("status", "APPROVED"),
                             ("reviewed_by", 5), ("level", 2), ("total_walk_count", 100)):
            self.assertEqual(self.post(**{field: value}).status_code, 422)
        self.assertEqual(self.rows(WalkRequest), [])
        self.assertEqual(len(self.rows(VolunteerProfile)), 1)

    def test_dog_shelter_settings_and_slots_required_with_profile_rollback(self):
        before = self.rows(VolunteerProfile)
        for dog, status in ((999, 404), (4, 409), (5, 409), (6, 409), (7, 409)):
            self.assertEqual(self.post(dog=dog).status_code, status)
        self.assertEqual(self.rows(VolunteerProfile), before)
        self.assertEqual(self.rows(WalkRequest), [])

    def test_aware_iso_dates_positive_dog_and_time_order_are_422(self):
        for start, end in (("2030-01-07T09:00:00", END.isoformat()), (START.isoformat(), "2030-01-07T10:00:00"),
                           (12345, END.isoformat()), (str(int(START.timestamp())), END.isoformat()),
                           ("bad", END.isoformat()), (END, START), (START, START)):
            self.assertEqual(self.post(start=start, end=end).status_code, 422)
        for dog in (0, -1, True, "1"):
            self.assertEqual(self.post(dog=dog).status_code, 422)

    def test_past_and_exactly_now_requests_are_409(self):
        for start in (NOW - timedelta(hours=1), NOW):
            self.assertEqual(self.post(dog=9, start=start, end=start + timedelta(hours=1)).status_code, 409)
        self.assertEqual(len(self.rows(VolunteerProfile)), 1)

    def test_seoul_date_and_weekday_are_independent_of_input_offset(self):
        # UTC Sunday -> Seoul Monday, crossing UTC midnight but not the Seoul date.
        result = self.create(dog=9, start="2030-01-06T23:30:00Z", end="2030-01-07T00:30:00Z")
        self.assertEqual(result["requested_start_at"], "2030-01-06T23:30:00Z")
        # UTC Monday late night -> Seoul Tuesday, for which no slot exists.
        self.assertEqual(self.post(user=2, dog=9, start="2030-01-07T23:30:00Z", end="2030-01-08T00:30:00Z").status_code, 409)
        # Same UTC date, different Seoul dates.
        response = self.post(user=2, dog=9, start="2030-01-07T14:30:00Z", end="2030-01-07T15:30:00Z")
        self.assertEqual(response.status_code, 409)
        self.assertIn("날짜", response.json()["detail"])

    def test_full_slot_boundaries_allowed_without_arbitrary_duration_cap(self):
        result = self.create(start="2030-01-07T09:00:00+09:00", end="2030-01-07T12:00:00+09:00")
        self.assertEqual(result["requested_end_at"], "2030-01-07T03:00:00Z")

    def test_outside_slot_and_combining_adjacent_slots_are_rejected(self):
        for dog, start, end in ((1, START - timedelta(minutes=1), END), (1, START, START + timedelta(hours=4)),
                                (8, START + timedelta(minutes=30), END + timedelta(minutes=30))):
            self.assertEqual(self.post(dog=dog, start=start, end=end).status_code, 409)

    def test_exact_and_partial_active_duplicates_block_but_adjacent_requests_work(self):
        first = self.create()
        self.assertEqual(self.post().status_code, 409)
        self.assertEqual(self.post(start=START + timedelta(minutes=30), end=END + timedelta(minutes=30)).status_code, 409)
        second = self.create(start=END, end=END + timedelta(hours=1))
        self.assertEqual(self.act(first["id"], "APPROVE").status_code, 200)
        self.assertEqual(self.act(second["id"], "APPROVE").status_code, 200)

    def test_distinct_volunteers_can_propose_same_dog_and_time(self):
        first, second = self.create(), self.create(user=2)
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(first["status"], second["status"])
        self.assertEqual(self.act(first["id"], "APPROVE").status_code, 200)
        self.assertEqual(self.act(second["id"], "APPROVE").status_code, 409)

    def test_pending_different_dogs_allowed_but_approved_volunteer_conflict_blocks(self):
        first, other = self.create(), self.create(dog=9)
        self.assertEqual(self.act(first["id"], "APPROVE").status_code, 200)
        self.assertEqual(self.act(other["id"], "APPROVE").status_code, 409)
        self.assertEqual(self.post(dog=3).status_code, 409)
        self.assertEqual(self.post(user=2).status_code, 409)  # same dog booking

    def test_rejected_or_cancelled_application_can_be_resubmitted_as_new_row(self):
        first = self.create()
        self.assertEqual(self.act(first["id"], "REJECT", rejection_reason="불가").status_code, 200)
        second = self.create()
        self.assertEqual(self.act(second["id"], "CANCEL", user=1).status_code, 200)
        third = self.create()
        self.assertEqual(len({first["id"], second["id"], third["id"]}), 3)

    def test_review_permissions_use_historical_shelter_and_admin_override(self):
        request = self.create()
        for user in (None, 1, 2, 4, 7):
            self.assertEqual(self.act(request["id"], "APPROVE", user=user).status_code, 401 if user is None else 403)
            self.assertEqual(self.act(request["id"], "REJECT", user=user, rejection_reason="거절").status_code, 401 if user is None else 403)
        response = self.act(request["id"], "APPROVE", user=5)
        self.assertEqual(response.status_code, 200)
        self.assertEqual((response.json()["status"], response.json()["reviewed_by"]), ("APPROVED", 5))
        self.assertEqual(response.json()["reviewed_at"], NOW.isoformat().replace("+00:00", "Z"))
        self.assertIsNone(response.json()["rejection_reason"])

    def test_rejection_reason_validation_and_trim(self):
        request = self.create()
        for body in ({"action": "REJECT"}, {"action": "REJECT", "rejection_reason": "  "},
                     {"action": "APPROVE", "rejection_reason": None},
                     {"action": "CANCEL", "rejection_reason": "사유"}, {"action": "UNKNOWN"},
                     {"action": "APPROVE", "status": "APPROVED"}, {"action": "APPROVE", "reviewed_by": 5}, {}):
            self.assertEqual(self.client.patch(f"/walk-requests/{request['id']}", headers=self.auth(3), json=body).status_code, 422)
        response = self.act(request["id"], "REJECT", rejection_reason="  안전 확인 필요  ")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["rejection_reason"], "안전 확인 필요")
        self.assertEqual(response.json()["reviewed_by"], 3)

    def test_final_states_and_repeated_review_actions_are_409(self):
        for state, action in (("APPROVED", "APPROVE"), ("APPROVED", "REJECT"),
                              ("REJECTED", "APPROVE"), ("REJECTED", "REJECT"), ("REJECTED", "CANCEL"),
                              ("CANCELLED", "APPROVE"), ("CANCELLED", "REJECT"), ("CANCELLED", "CANCEL")):
            # Different times avoid the database's active-identical constraint.
            i = len(self.rows(WalkRequest))
            identifier = self.seed_request(status=state, start=START + timedelta(days=i * 7), end=END + timedelta(days=i * 7))
            extra = {"rejection_reason": "재거절"} if action == "REJECT" else {}
            self.assertEqual(self.act(identifier, action, **extra).status_code, 409)

    def test_owner_staff_admin_cancellation_and_review_metadata_preserved(self):
        first = self.create()
        self.assertEqual(self.act(first["id"], "CANCEL", user=2).status_code, 403)
        self.assertEqual(self.act(first["id"], "CANCEL", user=4).status_code, 403)
        self.assertEqual(self.act(first["id"], "CANCEL", user=7).status_code, 403)
        pending_cancel = self.act(first["id"], "CANCEL", user=1).json()
        self.assertIsNone(pending_cancel["reviewed_by"])
        second = self.create()
        approved = self.act(second["id"], "APPROVE").json()
        cancelled = self.act(second["id"], "CANCEL", user=1)
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled.json()["reviewed_at"], approved["reviewed_at"])
        self.assertEqual(cancelled.json()["reviewed_by"], approved["reviewed_by"])
        third = self.create()
        self.assertEqual(self.act(third["id"], "CANCEL", user=3).status_code, 200)
        fourth = self.create()
        self.assertEqual(self.act(fourth["id"], "CANCEL", user=5).status_code, 200)

    def test_past_start_or_any_existing_walk_blocks_cancellation(self):
        for i, state in enumerate(("PENDING", "APPROVED")):
            identifier = self.seed_request(status=state, start=NOW - timedelta(hours=2 + i * 3), end=NOW - timedelta(hours=1 + i * 3))
            self.assertEqual(self.act(identifier, "CANCEL", user=1).status_code, 409)
        identifier = self.seed_request(status="APPROVED")
        with self.Session.begin() as db:
            db.add(Walk(walk_request_id=identifier, started_at=NOW))
        self.assertEqual(self.act(identifier, "CANCEL", user=1).status_code, 409)

    def test_approval_revalidates_walkability_slots_and_future(self):
        request = self.create()
        endpoint = "/dogs/1/walk-settings"
        for change in ({"walkable": False}, {"slots": []},
                       {"slots": [{"day_of_week": 1, "start_time": "09:00", "end_time": "12:00"}]}):
            self.assertEqual(self.client.patch(endpoint, headers=self.auth(3), json=change).status_code, 200)
            self.assertEqual(self.act(request["id"], "APPROVE").status_code, 409)
            self.assertEqual(self.client.patch(endpoint, headers=self.auth(3), json={
                "walkable": True, "difficulty": "BEGINNER", "slots": [{"day_of_week": 0, "start_time": "09:00", "end_time": "12:00"}]
            }).status_code, 200)
        with patch("app.walk_request_service.utc_now", return_value=START):
            self.assertEqual(self.act(request["id"], "APPROVE").status_code, 409)
        self.assertEqual(self.act(request["id"], "APPROVE").status_code, 200)

    def test_approval_revalidates_role_but_ignores_legacy_level(self):
        request = self.create(user=6, dog=2)
        with self.Session.begin() as db:
            db.get(VolunteerProfile, 6).level = 1
            db.get(User, 6).role = UserRole.SHELTER
        self.assertEqual(self.act(request['id'], 'APPROVE').status_code, 409)
        with self.Session.begin() as db:
            db.get(User, 6).role = UserRole.USER
        self.assertEqual(self.act(request['id'], 'APPROVE').status_code, 200)

    def test_legacy_request_without_profile_can_be_approved_without_creating_profile(self):
        advanced = self.seed_request(dog=2)
        self.assertEqual(self.act(advanced, 'APPROVE').status_code, 200)
        with self.Session() as db:
            self.assertIsNone(db.get(VolunteerProfile, 1))

    def test_transfer_preserves_inbox_ownership_blocks_approval_allows_old_shelter_rejection(self):
        request = self.create()
        with self.Session.begin() as db:
            db.get(Dog, 1).shelter_id = 2
        old = self.client.get("/shelters/1/walk-requests", headers=self.auth(3))
        new = self.client.get("/shelters/2/walk-requests", headers=self.auth(4))
        self.assertEqual([r["id"] for r in old.json()], [request["id"]])
        self.assertEqual(new.json(), [])
        self.assertEqual(self.act(request["id"], "APPROVE", user=4).status_code, 403)
        self.assertEqual(self.act(request["id"], "APPROVE").status_code, 409)
        rejected = self.act(request["id"], "REJECT", rejection_reason="소속 이관 확인")
        self.assertEqual(rejected.status_code, 200)
        self.assertEqual(rejected.json()["shelter_id"], 1)

    def test_my_list_filters_stable_order_pagination_and_no_other_owner(self):
        identifiers = [self.seed_request(status=state, start=START + timedelta(minutes=i * 10), end=START + timedelta(minutes=i * 10 + 5))
                       for i, state in enumerate(("PENDING", "APPROVED", "REJECTED", "CANCELLED"))]
        self.seed_request(user=2, start=END, end=END + timedelta(minutes=5))
        with self.Session.begin() as db:
            for request in db.query(WalkRequest):
                request.created_at = NOW
        response = self.client.get("/walk-requests/my?user_id=2&limit=2&offset=1", headers=self.auth())
        self.assertEqual(response.status_code, 200)
        self.assertEqual([r["id"] for r in response.json()], list(reversed(identifiers))[1:3])
        self.assertTrue(all(r["user_id"] == 1 for r in response.json()))
        pending = self.client.get("/walk-requests/my?status=PENDING", headers=self.auth()).json()
        self.assertEqual([r["id"] for r in pending], identifiers[:1])
        self.assertIn("dog", pending[0])
        self.assertIn("created_at", pending[0])

    def test_shelter_list_role_dog_status_pagination_and_missing_shelter(self):
        first = self.create()
        second = self.create(user=2, dog=9)
        self.create(user=2, dog=3)
        self.assertEqual(self.act(first["id"], "APPROVE").status_code, 200)
        for user in (None, 1, 2, 4, 7):
            self.assertEqual(self.client.get("/shelters/1/walk-requests", headers=self.auth(user)).status_code, 401 if user is None else 403)
        result = self.client.get("/shelters/1/walk-requests?status=PENDING&dog_id=9&limit=1&offset=0", headers=self.auth(3))
        self.assertEqual([r["id"] for r in result.json()], [second["id"]])
        self.assertEqual(self.client.get("/shelters/2/walk-requests", headers=self.auth(5)).status_code, 200)
        self.assertEqual(self.client.get("/shelters/3/walk-requests", headers=self.auth(5)).json(), [])
        self.assertEqual(self.client.get("/shelters/999/walk-requests", headers=self.auth(5)).status_code, 404)

    def test_history_auth_invalid_filters_and_missing_request(self):
        self.assertEqual(self.client.get("/walk-requests/my").status_code, 401)
        for query in ("status=COMPLETED", "limit=0", "limit=101", "offset=-1", "limit=abc"):
            self.assertEqual(self.client.get("/walk-requests/my?" + query, headers=self.auth()).status_code, 422)
        self.assertEqual(self.client.get("/shelters/1/walk-requests?dog_id=0", headers=self.auth(3)).status_code, 422)
        self.assertEqual(self.act(999, "APPROVE").status_code, 404)

    def test_history_uses_constant_query_count_and_never_writes(self):
        self.create()
        self.create(dog=9)
        self.create(dog=3)
        statements = []

        def record(conn, cursor, statement, params, context, executemany):
            statements.append(statement)

        event.listen(self.engine, "before_cursor_execute", record)
        try:
            for path, actor, count in (("/walk-requests/my", 1, 3), ("/shelters/1/walk-requests", 3, 4)):
                statements.clear()
                self.assertEqual(self.client.get(path, headers=self.auth(actor)).status_code, 200)
                self.assertEqual(len(statements), count, statements)
                self.assertTrue(all(s.lstrip().upper().startswith("SELECT") for s in statements))
        finally:
            event.remove(self.engine, "before_cursor_execute", record)

    def test_start_guard_requires_owner_approval_and_no_existing_walk_without_creating_one(self):
        identifier = self.seed_request(status="APPROVED")
        with self.Session() as db:
            result = validate_walk_start(db, identifier, db.get(User, 1))
            self.assertEqual(result.id, identifier)
            self.assertEqual(db.query(Walk).count(), 0)
        for target, user, code in ((identifier, 2, 403), (999, 1, 404)):
            with self.Session() as db, self.assertRaises(HTTPException) as error:
                validate_walk_start(db, target, db.get(User, user))
            self.assertEqual(error.exception.status_code, code)
        with self.Session.begin() as db:
            db.add(Walk(walk_request_id=identifier, started_at=NOW))
        with self.Session() as db, self.assertRaises(HTTPException) as error:
            validate_walk_start(db, identifier, db.get(User, 1))
        self.assertEqual(error.exception.status_code, 409)
        pending = self.seed_request(start=END, end=END + timedelta(hours=1))
        with self.Session() as db, self.assertRaises(HTTPException) as error:
            validate_walk_start(db, pending, db.get(User, 1))
        self.assertEqual(error.exception.status_code, 409)

    def test_database_failures_are_classified_by_sqlstate_and_constraint_and_rollback(self):
        class PGError(Exception):
            def __init__(self, code, name):
                super().__init__("private database details")
                self.pgcode, self.diag = code, SimpleNamespace(constraint_name=name)

        before = self.rows(VolunteerProfile)
        for code, name, expected in (
            ("23505", "uq_walk_requests_active", 409), ("23505", "volunteer_profiles_pkey", 409),
            ("23P01", "ex_walk_requests_dog_booking", 409), ("23P01", "ex_walk_requests_user_booking", 409),
            ("55P03", None, 409), ("40P01", None, 409), ("40001", None, 409),
            ("23505", "unrelated_unique", 500), ("23514", "ck_status", 500), ("08006", None, 500),
        ):
            rollback_calls = []

            def failing_db():
                with self.Session() as db:
                    with patch.object(db, "commit", side_effect=IntegrityError("secret SQL", {}, PGError(code, name))), \
                            patch.object(db, "rollback", wraps=db.rollback) as rollback:
                        try:
                            yield db
                        finally:
                            rollback_calls.append(rollback.called)

            self.app.dependency_overrides[get_db] = failing_db
            response = self.post()
            self.assertEqual(response.status_code, expected, response.text)
            self.assertNotIn("private", response.text)
            self.assertNotIn("secret", response.text)
            self.assertEqual(rollback_calls, [True])
            self.assertEqual(self.rows(VolunteerProfile), before)
            self.assertEqual(self.rows(WalkRequest), [])

    def test_failed_commit_restores_review_status_and_metadata(self):
        request = self.create()

        def failing_db():
            with self.Session() as db, patch.object(db, "commit", side_effect=SQLAlchemyError("secret")):
                yield db

        self.app.dependency_overrides[get_db] = failing_db
        self.assertEqual(self.act(request["id"], "APPROVE").status_code, 500)
        with self.Session() as db:
            stored = db.get(WalkRequest, request["id"])
            self.assertEqual(stored.status, "PENDING")
            self.assertIsNone(stored.reviewed_by)
            self.assertIsNone(stored.reviewed_at)


if __name__ == "__main__":
    unittest.main()
