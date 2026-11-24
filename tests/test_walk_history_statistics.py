"""Owner walk history and cumulative statistics HTTP contract."""
from datetime import timedelta
from decimal import Decimal
import unittest

from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool

from app.models import Dog, Shelter, VolunteerProfile, Walk
from app.routers import shelter_live_walks, walk_maps, walk_reviews
from app.walk_models import VolunteerLevelEvaluation, VolunteerLevelEvent
from walk_record_test_support import START, WalkFixture


class HistoryStatisticsCases(WalkFixture):
    def setup_history_api(self, engine):
        self.setup_walk_api(engine)
        for router in (walk_maps.router, walk_reviews.router, shelter_live_walks.router):
            self.app.include_router(router)

    def seed_history_walk(self, day, *, dog=1, user=1, status="COMPLETED", reason="USER",
                          distance=None, required=False, started_at=None):
        reserved_at = START + timedelta(days=day)
        started_at = reserved_at if started_at is None else started_at
        request_id = self.seed_request(user=user, dog=dog, status="APPROVED", start=reserved_at,
                                       end=reserved_at + timedelta(hours=1))
        with self.Session.begin() as db:
            walk = Walk(
                walk_request_id=request_id, status=status, end_reason=reason,
                started_at=started_at, handover_required=required, total_distance_m=distance,
                ended_at=None if status == "IN_PROGRESS" else started_at + timedelta(minutes=10),
            )
            db.add(walk)
            db.flush()
            return walk.id

    def get_json(self, path, user=1, **query):
        response = self.client.get(path, headers=self.auth(user), params=query)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def assert_history_totals(self, profile, *, count, distance, dogs, missing, complete):
        self.assertEqual(profile["history_walk_count"], count)
        self.assertEqual(profile["history_distance_m"], distance)
        self.assertEqual(profile["history_unique_dog_count"], dogs)
        self.assertEqual(profile["history_distance_missing_count"], missing)
        self.assertIs(profile["history_distance_is_complete"], complete)

    def test_finished_walk_totals_share_history_scope_without_changing_legacy_records(self):
        normal = self.seed_history_walk(0, distance=Decimal("123.45"))
        auto = self.seed_history_walk(1, reason="AUTO_TIMEOUT", distance=0)
        stopped = self.seed_history_walk(2, dog=2, status="INTERRUPTED", reason="STAFF")
        self.seed_history_walk(3, status="IN_PROGRESS", reason=None)
        self.seed_history_walk(4, status="CANCELLED", distance=900)
        self.seed_history_walk(5, dog=3, user=2, distance=800)
        with self.Session.begin() as db:
            db.add(VolunteerProfile(user_id=1, level=5, total_walk_count=20, total_distance_m=2300,
                                    total_duration_seconds=1600, unique_dog_count=10))
            db.add(VolunteerLevelEvaluation(walk_id=normal, user_id=1, status="COMPLETED",
                                            policy_version="test-v1", evaluated_at=START))

        models = (VolunteerProfile, VolunteerLevelEvaluation, VolunteerLevelEvent, Walk)
        before = [self.rows(model) for model in models]
        history = self.get_json("/walks/history")
        self.assertEqual([walk["walk_id"] for walk in history["walks"]], [stopped, auto, normal])
        self.assertEqual([walk["end_reason"] for walk in history["walks"]], ["STAFF", "AUTO_TIMEOUT", "USER"])
        self.assertEqual([walk["counted_as_volunteer_record"] for walk in history["walks"]], [False, True, True])
        profile = self.get_json("/volunteers/me")
        self.assertEqual(history["total"], 3)
        self.assert_history_totals(profile, count=history["total"], distance="123.45", dogs=2,
                                   missing=1, complete=False)
        legacy_profile = self.get_json("/users/1/profile")
        for field, value in {"level": 5, "total_walk_count": 20, "total_distance_m": "2300.00",
                             "total_duration_seconds": 1600, "unique_dog_count": 10}.items():
            self.assertEqual(profile[field], value)
            self.assertEqual(legacy_profile[field], value)
        self.assertFalse(any(field.startswith("history_") for field in legacy_profile))
        self.assertEqual([self.rows(model) for model in models], before)

    def test_empty_zero_and_missing_distance_without_creating_a_profile(self):
        self.assertEqual(self.get_json("/walks/history")["total"], 0)
        self.assert_history_totals(self.get_json("/volunteers/me"), count=0, distance="0.00", dogs=0,
                                   missing=0, complete=True)
        self.seed_history_walk(0, distance=0)
        zero = self.get_json("/volunteers/me")
        self.assert_history_totals(zero, count=1, distance="0.00", dogs=1, missing=0, complete=True)
        self.seed_history_walk(1, status="INTERRUPTED", reason="STAFF")
        self.seed_history_walk(2, user=2)
        partial = self.get_json("/volunteers/me")
        self.assert_history_totals(partial, count=2, distance="0.00", dogs=1, missing=1, complete=False)
        self.assertEqual(partial["total_walk_count"], 0)
        self.assertEqual(partial["level"], 1)
        other = self.get_json("/volunteers/me", user=2)
        self.assert_history_totals(other, count=1, distance="0.00", dogs=1, missing=1, complete=False)
        self.assertEqual(self.get_json("/walks/history", user=2)["total"], 1)
        self.assertEqual([row[0] for row in self.rows(VolunteerProfile)], [6])

    def test_shelter_names_follow_reservation_and_keep_filtered_pagination(self):
        first = self.seed_history_walk(0, required=True)
        second = self.seed_history_walk(1, dog=3, required=True, started_at=START, reason="AUTO_TIMEOUT")
        self.seed_history_walk(2, user=2, required=True)
        with self.Session.begin() as db:
            db.get(Dog, 1).shelter_id = 2
            db.get(Shelter, 1).name = "변경된 보호소명"

        statements = []

        def record_query(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        event.listen(self.engine, "before_cursor_execute", record_query)
        try:
            for filters in ({}, {"handover_status": "NOT_CONFIRMED"}):
                with self.subTest(filters=filters):
                    statements.clear()
                    page = self.get_json("/walks/history", limit=1, **filters)
                    one_count = len(statements)
                    statements.clear()
                    all_walks = self.get_json("/walks/history", limit=100, **filters)
                    self.assertEqual(len(statements), one_count)
                    self.assertEqual(page["total"], 2)
                    self.assertEqual(page["walks"], all_walks["walks"][:1])
                    self.assertEqual([(walk["walk_id"], walk["shelter_name"], walk["handover_status"])
                                      for walk in all_walks["walks"]],
                                     [(second, "다른 보호소", "NOT_CONFIRMED"),
                                      (first, "변경된 보호소명", "NOT_CONFIRMED")])
                    self.assertEqual(self.get_json("/walks/history", limit=1, offset=1, **filters)["walks"],
                                     all_walks["walks"][1:])
                    empty = self.get_json("/walks/history", limit=1, offset=2, **filters)
                    self.assertEqual((empty["total"], empty["limit"], empty["offset"], empty["walks"]),
                                     (2, 1, 2, []))
        finally:
            event.remove(self.engine, "before_cursor_execute", record_query)

        auto = self.get_json("/walks/history", status="COMPLETED", end_reason="AUTO_TIMEOUT",
                             handover_status="NOT_CONFIRMED")
        self.assertEqual(auto["total"], 1)
        self.assertEqual(auto["walks"][0]["walk_id"], second)
        self.assertEqual(self.get_json("/walks/history", status="INTERRUPTED")["total"], 0)
        self.assertNotIn("shelter_name", self.get_json("/users/1/walks")["walks"][0])

    def test_detail_maps_summary_and_access_remain_compatible(self):
        walk_id = self.begin()["id"]
        self.advance(120)
        self.assertEqual(self.marker(walk_id).status_code, 201)
        self.assertEqual(self.upload(walk_id, [self.point(), self.point(1, 60, longitude=0.001)]).status_code, 200)
        self.assertEqual(self.end(walk_id).status_code, 200)
        handover = self.client.post(
            f"/walks/{walk_id}/handover", headers=self.auth(),
            json={"handover_result": "COMPLETE", "dog_returned": True,
                  "equipment_returned": True, "condition_shared": True},
        )
        self.assertEqual(handover.status_code, 201, handover.text)
        memo = "history_statistics_test_private_memo"
        receipt = self.client.put(f"/walks/{walk_id}/private-memo", headers=self.auth(),
                                  json={"memo": memo, "expected_version": 0})
        self.assertEqual(receipt.status_code, 201, receipt.text)
        detail = self.get_json(f"/walks/{walk_id}", limit=1, offset=1)
        summary = self.get_json(f"/walks/{walk_id}/summary")
        self.assertEqual(detail["walk"]["status"], "COMPLETED")
        self.assertEqual(detail["walk"]["end_reason"], "USER")
        self.assertEqual(detail["handover_status"], "SELF_REPORTED")
        self.assertEqual(detail["handover_status"], summary["handover_status"])
        for field in ("duration_seconds", "total_distance_m"):
            self.assertEqual(detail["walk"][field], summary["summary"][field])
        self.assertEqual(detail["points"][0]["sequence_no"], 1)
        self.assertEqual(len(summary["points"]), 2)
        self.assertEqual(len(summary["markers"]), 1)
        self.assertEqual(summary["private_memo"]["memo"], memo)

        for path, user in (
            ("/walks/history", 1), ("/volunteers/me", 1), ("/users/1/map", 1), ("/dogs/1/map", 2),
            (f"/walks/{walk_id}", 1), (f"/walks/{walk_id}", 3), (f"/walks/{walk_id}", 5),
            ("/shelters/me/walks", 3), (f"/shelters/me/walks/{walk_id}", 3),
        ):
            with self.subTest(path=path, user=user):
                response = self.client.get(path, headers=self.auth(user))
                self.assertEqual(response.status_code, 200, response.text)
                self.assertNotIn("private_memo", response.text)
                self.assertNotIn(memo, response.text)
        for path, user in (
            ("/walks/history", 3), ("/walks/history", 5), ("/volunteers/me", 3), ("/volunteers/me", 5),
            (f"/walks/{walk_id}", 2), (f"/walks/{walk_id}/summary", 2),
            (f"/walks/{walk_id}/summary", 3), (f"/walks/{walk_id}/summary", 5),
        ):
            with self.subTest(path=path, user=user):
                self.assertEqual(self.client.get(path, headers=self.auth(user)).status_code, 403)
        self.assertEqual(self.client.get("/walks/history").status_code, 401)
        self.assertEqual(self.client.get("/volunteers/me").status_code, 401)


class WalkHistoryStatisticsTests(HistoryStatisticsCases, unittest.TestCase):
    def setUp(self):
        self.setup_history_api(create_engine("sqlite://", connect_args={"check_same_thread": False},
                                             poolclass=StaticPool))
