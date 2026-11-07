"""Issue #6: owner map, public dog map privacy, history, statistics and level refresh."""
from decimal import Decimal
import unittest

from walk_map_test_support import PRIVATE_KEYS, WalkMapFixture, all_keys
from walk_record_test_support import START
from walk_request_test_support import END
from app.models import VolunteerProfile
from app.volunteer_stats import (
    LEVEL_2_MIN_COMPLETED_WALKS, LEVEL_2_MIN_UNIQUE_DOGS, level_for, refresh_volunteer_profile,
)
from app.walk_map_service import OWNER_MAX_ROUTE_POINTS
from datetime import timedelta


class OwnerMapTests(WalkMapFixture, unittest.TestCase):
    def setUp(self):
        self.setup_maps()

    def test_only_owner_volunteer_can_read_user_endpoints(self):
        self.seed_walk(points=3)
        for suffix in ("map", "profile", "walks"):
            path = f"/users/1/{suffix}"
            self.assertEqual(self.get(path, None).status_code, 401)
            for other in (2, 3, 4, 5):  # another volunteer, shelters, admin
                self.assertEqual(self.get(path, other).status_code, 403, (suffix, other))
            self.assertEqual(self.get(path, 1).status_code, 200, suffix)
        self.assertEqual(self.get("/users/999/map", 1).status_code, 403)  # no existence leak

    def test_map_returns_only_own_completed_walks_with_dog_distance_and_time(self):
        mine = self.seed_walk(dog=1, points=3, day=1, distance="250.50", duration=900)
        self.seed_walk(dog=2, status="INTERRUPTED", points=3, day=2)
        self.seed_walk(dog=9, status="IN_PROGRESS", points=3, day=3)
        self.seed_walk(user=2, dog=1, points=3, day=4)
        body = self.get("/users/1/map").json()
        self.assertEqual((body["total"], len(body["walks"])), (1, 1))
        walk = body["walks"][0]
        self.assertEqual((walk["walk_id"], walk["dog"]["id"], walk["dog"]["name"]), (mine, 1, "강아지1"))
        self.assertEqual((walk["total_distance_m"], walk["duration_seconds"], walk["point_count"]), ("250.50", 900, 3))
        self.assertEqual(len(walk["route"]), 3)

    def test_map_route_is_separate_from_raw_gps_data(self):
        self.seed_walk(points=3)
        route = self.get("/users/1/map").json()["walks"][0]["route"]
        for point in route:
            self.assertEqual(set(point), {"latitude", "longitude"})
        raw = self.detail(self.seed_walk(points=3, day=2)).json()["points"][0]
        self.assertTrue({"accuracy_m", "recorded_at", "sequence_no"} <= set(raw))

    def test_large_route_is_simplified_but_keeps_first_and_last_points(self):
        count = 450
        self.seed_walk(points=count)
        walk = self.get("/users/1/map").json()["walks"][0]
        self.assertEqual(walk["point_count"], count)
        self.assertLessEqual(len(walk["route"]), OWNER_MAX_ROUTE_POINTS + 1)
        self.assertGreater(len(walk["route"]), 10)
        self.assertEqual(Decimal(walk["route"][0]["latitude"]), Decimal("37.5"))
        self.assertEqual(Decimal(walk["route"][-1]["latitude"]), Decimal("37.5") + Decimal(count - 1) * Decimal("0.0001"))

    def test_walk_without_points_has_empty_route(self):
        self.seed_walk(points=0)
        walk = self.get("/users/1/map").json()["walks"][0]
        self.assertEqual((walk["point_count"], walk["route"]), (0, []))

    def test_pagination_newest_first_and_bounds(self):
        for day in (1, 2, 3):
            self.seed_walk(day=day, points=2)
        first = self.get("/users/1/map?limit=2").json()
        self.assertEqual((first["total"], len(first["walks"])), (3, 2))
        stamps = [w["started_at"] for w in first["walks"]]
        self.assertEqual(stamps, sorted(stamps, reverse=True))
        last = self.get("/users/1/map?limit=2&offset=2").json()
        self.assertEqual(len(last["walks"]), 1)
        self.assertLess(last["walks"][0]["started_at"], stamps[-1])
        for query in ("limit=0", "limit=31", "offset=-1"):
            self.assertEqual(self.get(f"/users/1/map?{query}").status_code, 422, query)


class DogMapTests(WalkMapFixture, unittest.TestCase):
    def setUp(self):
        self.setup_maps()

    def test_requires_login_and_existing_dog(self):
        self.assertEqual(self.get("/dogs/1/map", None).status_code, 401)
        self.assertEqual(self.get("/dogs/999/map", 1).status_code, 404)
        for viewer in (1, 3, 5):
            self.assertEqual(self.get("/dogs/1/map", viewer).status_code, 200)

    def test_stats_count_only_completed_walks_and_distinct_volunteers(self):
        self.seed_walk(user=1, dog=1, points=100, day=1, distance="100.00", duration=600)
        self.seed_walk(user=1, dog=1, points=100, day=2, distance="50.25", duration=300)
        self.seed_walk(user=2, dog=1, points=100, day=3, distance="10.00", duration=60)
        self.seed_walk(user=6, dog=1, status="INTERRUPTED", points=100, day=4)
        self.seed_walk(user=6, dog=1, status="IN_PROGRESS", points=100, day=5)
        self.seed_walk(user=1, dog=2, points=100, day=6)
        body = self.get("/dogs/1/map").json()
        self.assertEqual(body["stats"], {"walk_count": 3, "total_distance_m": "160.25",
                                         "total_duration_seconds": 960, "volunteer_count": 2})
        self.assertEqual((body["total"], len(body["walks"])), (3, 3))

    def test_public_map_hides_volunteer_identity_ids_and_times(self):
        self.seed_walk(user=1, points=100)
        body = self.get("/dogs/1/map", 2).json()
        self.assertFalse(PRIVATE_KEYS & {k for k in all_keys(body) if k != "dog_id"}, set(all_keys(body)))
        self.assertEqual(set(body["walks"][0]), {"total_distance_m", "duration_seconds", "route"})

    def test_public_route_is_trimmed_rounded_and_simplified(self):
        self.seed_walk(points=100)  # ~1.1 km straight line: raw start 37.5000, raw end 37.5099
        route = self.get("/dogs/1/map").json()["walks"][0]["route"]
        self.assertGreaterEqual(len(route), 2)
        latitudes = [Decimal(p["latitude"]) for p in route]
        for point in route:
            for value in (point["latitude"], point["longitude"]):
                self.assertGreaterEqual(Decimal(value).as_tuple().exponent, -3, value)
        self.assertGreaterEqual(min(latitudes), Decimal("37.501"))  # raw start (37.500) trimmed away
        self.assertLessEqual(max(latitudes), Decimal("37.509"))     # raw end (37.510) trimmed away
        self.assertEqual(len(route), len({(p["latitude"], p["longitude"]) for p in route}))

    def test_short_routes_are_withheld_but_still_counted(self):
        self.seed_walk(points=20)  # ~211 m: nothing is left after trimming both ends
        body = self.get("/dogs/1/map").json()
        self.assertEqual(body["walks"][0]["route"], [])
        self.assertEqual(body["stats"]["walk_count"], 1)

    def test_owner_map_is_not_masked_but_public_map_is(self):
        self.seed_walk(points=100)
        owner = self.get("/users/1/map").json()["walks"][0]["route"]
        public = self.get("/dogs/1/map").json()["walks"][0]["route"]
        self.assertEqual(Decimal(owner[0]["latitude"]), Decimal("37.5"))
        self.assertNotIn(owner[0], public)
        self.assertLess(len(public), len(owner))

    def test_pagination_bounds(self):
        for day in (1, 2, 3):
            self.seed_walk(day=day, points=2)
        body = self.get("/dogs/1/map?limit=2").json()
        self.assertEqual((body["total"], len(body["walks"]), body["stats"]["walk_count"]), (3, 2, 3))
        for query in ("limit=0", "limit=31", "offset=-1"):
            self.assertEqual(self.get(f"/dogs/1/map?{query}").status_code, 422, query)


class HistoryAndProfileTests(WalkMapFixture, unittest.TestCase):
    def setUp(self):
        self.setup_maps()

    def test_history_lists_all_statuses_but_flags_only_completed_as_records(self):
        self.seed_walk(day=1, status="COMPLETED", points=2)
        self.seed_walk(day=2, status="INTERRUPTED", points=2)
        self.seed_walk(day=3, status="IN_PROGRESS", points=2)
        self.seed_walk(user=2, day=4, points=2)
        body = self.get("/users/1/walks").json()
        self.assertEqual(body["total"], 3)
        self.assertEqual([(w["status"], w["counted_as_volunteer_record"]) for w in body["walks"]],
                         [("COMPLETED", True), ("INTERRUPTED", False), ("IN_PROGRESS", False)])
        self.assertEqual(body["walks"][0]["dog"]["name"], "강아지1")
        only = self.get("/users/1/walks?status=INTERRUPTED").json()
        self.assertEqual((only["total"], only["walks"][0]["status"]), (1, "INTERRUPTED"))
        self.assertEqual(self.get("/users/1/walks?status=NOPE").status_code, 422)
        self.assertEqual(len(self.get("/users/1/walks?limit=1&offset=1").json()["walks"]), 1)
        self.assertEqual(self.get("/users/1/walks?limit=101").status_code, 422)

    def test_profile_defaults_match_volunteer_me(self):
        profile = self.get("/users/2/profile", 2).json()
        me = self.get("/volunteers/me", 2).json()
        self.assertEqual(profile.pop("nickname"), "사용자2")
        self.assertEqual(profile, me)
        self.assertEqual((profile["level"], profile["total_walk_count"]), (1, 0))

    def test_profile_statistics_stay_consistent_with_volunteer_me_after_walks(self):
        self.complete_via_api()
        profile = self.get("/users/1/profile").json()
        me = self.get("/volunteers/me").json()
        self.assertEqual(profile.pop("nickname"), "사용자1")
        self.assertEqual(profile, me)
        self.assertEqual((me["total_walk_count"], me["total_distance_m"], me["total_duration_seconds"],
                          me["unique_dog_count"]), (1, "22.24", 120, 1))


class StatisticsAndLevelTests(WalkMapFixture, unittest.TestCase):
    def setUp(self):
        self.setup_maps()

    def test_completing_a_walk_updates_totals_once(self):
        walk, ended = self.complete_via_api()
        self.assertEqual(self.profile_row(1), (1, 1, Decimal("22.24"), 120, 1))
        self.assertEqual(self.end(walk["id"]).status_code, 409)  # duplicate close
        self.assertEqual(self.profile_row(1), (1, 1, Decimal("22.24"), 120, 1))

    def test_interrupted_walks_are_not_volunteer_records(self):
        walk = self.begin()
        self.advance(60)
        self.assertEqual(self.end(walk["id"], status="INTERRUPTED").status_code, 200)
        self.assertIsNone(self.profile_row(1))

    def test_unique_dog_count_only_grows_for_new_dogs(self):
        self.complete_via_api(dog=1)
        again = self.seed_request(user=1, dog=1, status="APPROVED", start=END, end=END + timedelta(hours=1))
        self.advance(3600)
        started = self.start(again)
        self.assertEqual(started.status_code, 201, started.text)
        self.assertEqual(self.end(started.json()["id"]).status_code, 200)
        level, count, _, _, dogs = self.profile_row(1)
        self.assertEqual((count, dogs), (2, 1))
        other = self.seed_request(user=1, dog=9, status="APPROVED", start=END + timedelta(hours=2), end=END + timedelta(hours=3))
        self.advance(3 * 3600)  # inside the START+3h window of the third request
        started = self.start(other)
        self.assertEqual(started.status_code, 201, started.text)
        self.assertEqual(self.end(started.json()["id"]).status_code, 200)
        self.assertEqual(self.profile_row(1)[1::3], (3, 2))

    def test_refresh_is_idempotent_and_repairs_stale_totals(self):
        for day, dog in enumerate((1, 1, 9), start=1):
            self.seed_walk(dog=dog, day=day, distance="100.00", duration=600)
        self.seed_walk(day=4, status="INTERRUPTED")
        self.seed_walk(day=5, status="IN_PROGRESS")
        results = []
        for _ in range(2):
            with self.Session.begin() as db:
                profile = refresh_volunteer_profile(db, 1)
                results.append((profile.level, profile.total_walk_count, profile.total_distance_m,
                                profile.total_duration_seconds, profile.unique_dog_count))
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0], (1, 3, Decimal("300.00"), 1800, 2))
        with self.Session.begin() as db:
            db.get(VolunteerProfile, 1).total_walk_count = 99
        with self.Session.begin() as db:
            self.assertEqual(refresh_volunteer_profile(db, 1).total_walk_count, 3)

    def test_level_rule(self):
        self.assertEqual(level_for(LEVEL_2_MIN_COMPLETED_WALKS - 1, LEVEL_2_MIN_UNIQUE_DOGS), 1)
        self.assertEqual(level_for(LEVEL_2_MIN_COMPLETED_WALKS, LEVEL_2_MIN_UNIQUE_DOGS - 1), 1)
        self.assertEqual(level_for(LEVEL_2_MIN_COMPLETED_WALKS, LEVEL_2_MIN_UNIQUE_DOGS), 2)

    def test_promotion_happens_on_completion_and_unlocks_experienced_dogs(self):
        for day, dog in enumerate((1, 1, 9, 8), start=1):
            self.seed_walk(dog=dog, day=day)
        later = dict(start=START + timedelta(days=7), end=START + timedelta(days=7, hours=1))
        self.assertEqual(self.post(user=2, dog=2, **later).status_code, 409)  # level 1 volunteer
        self.complete_via_api(dog=1)
        self.assertEqual(self.profile_row(1)[:2], (2, 5))
        self.assertEqual(self.get("/volunteers/me").json()["level"], 2)
        created = self.post(user=1, dog=2, **later)  # EXPERIENCED dog now allowed
        self.assertEqual(created.status_code, 201, created.text)

    def test_level_is_never_lowered_automatically(self):
        self.complete_via_api(user=6, dog=1)  # seeded level-2 profile with no walk rows
        level, count, *_ = self.profile_row(6)
        self.assertEqual((level, count), (2, 1))


if __name__ == "__main__":
    unittest.main()
