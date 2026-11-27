"""Onboarding rate limits through real FastAPI requests (isolated SQLite, injected clock)."""
from concurrent.futures import ThreadPoolExecutor
import math
import os
from threading import Barrier
import unittest
from unittest.mock import patch

with patch.dict(os.environ, {"DATABASE_URL": "sqlite://"}):
    from app.database import Base, get_db
    from app.models import User
    from app.rate_limit import MemoryWindowStore, RateLimiter, positive_int_env
    from app.routers import auth, users

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from privacy_test_support import privacy, use_test_privacy_policy

PASSWORD = "WalkTogether2026!"
TOO_MANY = "요청이 너무 많습니다. 잠시 후 다시 시도해 주세요."
LOGIN_TOO_MANY = "로그인 요청이 너무 많습니다. 잠시 후 다시 시도해 주세요."
ID, NICK = "/users/id-availability", "/users/nickname-availability"


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class RateLimitApiTests(unittest.TestCase):
    """Each test gets fresh limiters with the production policy and a controllable clock."""

    def setUp(self):
        use_test_privacy_policy(self)
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.clock = FakeClock()
        self.signup_limiter = RateLimiter("signup", 5, 600, MemoryWindowStore(self.clock))
        self.availability_limiter = RateLimiter("availability", 30, 60, MemoryWindowStore(self.clock))
        self.login_limiter = RateLimiter("login", 10, 60, MemoryWindowStore(self.clock), message=LOGIN_TOO_MANY)
        self.app = FastAPI()
        self.app.include_router(auth.router)
        self.app.include_router(users.router)

        def test_db():
            with self.Session() as db:
                yield db

        self.app.dependency_overrides[get_db] = test_db
        self.app.dependency_overrides[users.SIGNUP_RATE_LIMITER] = self.signup_limiter
        self.app.dependency_overrides[users.AVAILABILITY_RATE_LIMITER] = self.availability_limiter
        self.app.dependency_overrides[auth.LOGIN_RATE_LIMITER] = self.login_limiter
        self.client = self.client_for("198.51.100.7")
        self.sequence = 0

    def tearDown(self):
        self.engine.dispose()

    def client_for(self, host):
        client = TestClient(self.app, client=(host, 50000))
        self.addCleanup(client.close)
        return client

    def signup(self, client=None, **changes):
        self.sequence += 1
        body = {"login_id": f"walker{self.sequence}", "nickname": f"nick_{self.sequence}",
                "password": PASSWORD, "privacy": privacy(), **changes}
        return (client or self.client).post("/users/", json=body)

    def check(self, path, client=None, value="walker04"):
        params = {"login_id": value} if path == ID else {"nickname": value}
        return (client or self.client).get(path, params=params)

    def user_count(self):
        with self.Session() as db:
            return db.query(User).count()

    def assert_limited(self, response, retry_after):
        self.assertEqual(response.status_code, 429, response.text)
        self.assertEqual(response.json(), {"code": "TOO_MANY_REQUESTS",
                                           "data": {"retry_after_seconds": retry_after}, "message": TOO_MANY})
        self.assertEqual(response.headers["Retry-After"], str(retry_after))

    # Limits and the 429 contract

    def test_signup_allows_the_limit_then_answers_429_and_stores_nothing_more(self):
        for _ in range(5):
            self.assertEqual(self.signup().status_code, 200)
        self.clock.now += 0.5
        response = self.signup()
        self.assert_limited(response, 600)  # ceil(600 - 0.5): the oldest request leaves at t+600
        self.assertEqual(self.user_count(), 5)

    def test_retry_after_matches_the_real_wait_and_the_limit_lifts_exactly_then(self):
        for _ in range(5):
            self.signup()
        self.clock.now += 599.2
        self.assert_limited(self.signup(), 1)
        self.clock.now += 0.8  # exactly 600 s after the first request
        self.assertEqual(self.signup().status_code, 200)
        self.assertEqual(self.user_count(), 6)

    def test_window_slides_instead_of_resetting_at_a_boundary(self):
        for _ in range(5):
            self.assertEqual(self.signup().status_code, 200)
            self.clock.now += 100  # requests at t=0,100,200,300,400
        self.assert_limited(self.signup(), 100)    # t=500: t=0 expires at 600
        self.clock.now += 100
        self.assertEqual(self.signup().status_code, 200)  # t=600
        self.clock.now += 1
        self.assert_limited(self.signup(), 99)     # t=601: t=100 expires at 700, never 6 in 600 s

    def test_rejected_requests_do_not_extend_the_wait(self):
        for _ in range(5):
            self.signup()
        for elapsed in (10, 200, 450):
            self.clock.now = 1000.0 + elapsed
            self.assert_limited(self.signup(), 600 - elapsed)
        self.clock.now = 1600.0
        self.assertEqual(self.signup().status_code, 200)

    def test_invalid_requests_count_and_the_limit_is_checked_before_validation(self):
        for _ in range(5):
            self.assertEqual(self.signup(password="short").status_code, 422)
        self.assert_limited(self.signup(password="short"), 600)
        self.assert_limited(self.signup(), 600)
        self.assertEqual(self.user_count(), 0)

    def test_success_and_error_responses_are_unchanged_below_the_limit(self):
        self.assertEqual(self.signup(login_id="walker04", nickname="산책왕").json()["code"], "OK")
        duplicate = self.signup(login_id="walker04")
        self.assertEqual((duplicate.status_code, duplicate.json()["code"]), (409, "LOGIN_ID_ALREADY_EXISTS"))
        self.assertNotIn("Retry-After", duplicate.headers)
        taken = self.check(ID)
        self.assertEqual((taken.status_code, taken.json()["data"]), (200, {"available": False}))
        self.assertEqual(self.check(NICK, value="ab").json()["code"], "INVALID_NICKNAME_LENGTH")

    # Policy scope

    def test_id_and_nickname_checks_share_one_budget(self):
        for index in range(30):
            self.assertEqual(self.check(ID if index % 2 else NICK, value=f"value{index}").status_code, 200)
        self.clock.now += 15
        self.assert_limited(self.check(ID), 45)
        self.assert_limited(self.check(NICK), 45)
        self.clock.now += 45
        self.assertEqual(self.check(NICK).status_code, 200)

    def test_signup_and_availability_budgets_are_separate(self):
        for _ in range(30):
            self.check(ID)
        self.assert_limited(self.check(NICK), 60)
        self.assertEqual(self.signup().status_code, 200)
        for _ in range(4):
            self.signup()
        self.assert_limited(self.signup(), 600)
        self.clock.now += 60
        self.assertEqual(self.check(ID).status_code, 200)  # availability recovered, sign-up still limited
        self.assert_limited(self.signup(), 540)

    def test_clients_are_limited_separately_and_forwarded_headers_are_ignored(self):
        other = self.client_for("203.0.113.9")
        for _ in range(5):
            self.signup()
        self.assert_limited(self.signup(), 600)
        # A spoofed header from a direct client neither changes its bucket nor frees it.
        for header in ("203.0.113.50", "10.0.0.1, 203.0.113.51"):
            response = self.client.post("/users/", headers={"X-Forwarded-For": header, "X-Real-IP": header},
                                        json={"login_id": "spoofid", "nickname": "spoof_nick",
                                              "password": PASSWORD, "privacy": privacy()})
            self.assert_limited(response, 600)
        self.assertEqual(self.signup(client=other).status_code, 200)

    def test_other_user_routes_are_not_limited(self):
        for _ in range(40):
            self.check(ID)
        self.assertEqual(self.client.get("/users/me").status_code, 401)
        self.assertEqual(self.client.post("/auth/login", json={"identifier": "nobody", "password": "x"}).status_code, 401)

    # Login

    def login(self, client=None, **body):
        return (client or self.client).post("/auth/login", json=body)

    def test_login_allows_the_limit_then_answers_429_before_checking_credentials(self):
        self.assertEqual(self.signup(login_id="walker04").status_code, 200)
        statuses = [self.login(identifier="walker04", password="wrong-password").status_code for _ in range(9)]
        statuses.append(self.login().status_code)  # 422 attempts count too
        self.assertEqual(statuses, [401] * 9 + [422])
        # Even the correct password is refused once the limit is reached: no session is made.
        response = self.login(identifier="walker04", password=PASSWORD)
        self.assertEqual((response.status_code, response.json()),
                         (429, {"code": "TOO_MANY_REQUESTS", "data": {"retry_after_seconds": 60}, "message": LOGIN_TOO_MANY}))
        self.assertEqual(response.headers["Retry-After"], "60")
        self.clock.now += 59
        self.assertEqual(self.login(identifier="walker04", password=PASSWORD).json()["data"]["retry_after_seconds"], 1)
        self.clock.now += 1
        self.assertEqual(self.login(identifier="walker04", password=PASSWORD).status_code, 200)

    def test_login_limit_is_per_client_and_separate_from_signup(self):
        for _ in range(10):
            self.login(identifier="nobody", password="x")
        self.assertEqual(self.login(identifier="nobody", password="x").status_code, 429)
        self.assertEqual(self.login(client=self.client_for("203.0.113.9"), identifier="nobody", password="x").status_code, 401)
        self.assertEqual(self.signup().status_code, 200)
        self.assertEqual(self.check(ID).status_code, 200)

    # Concurrency and failures

    def test_concurrent_requests_never_exceed_the_limit(self):
        workers = 45
        barrier = Barrier(workers)

        def request(index):
            barrier.wait(10)
            return self.check(ID if index % 2 else NICK, value=f"value{index}").status_code

        # The fixture's single shared SQLite connection is not safe for 45 threads, so
        # stub the lookup: this test is about the limiter, not the database.
        with patch("app.routers.users.is_taken", return_value=False), \
                ThreadPoolExecutor(max_workers=workers) as pool:
            statuses = list(pool.map(request, range(workers)))
        self.assertEqual((statuses.count(200), statuses.count(429)), (30, 15))

    def test_store_failure_fails_open_without_logging_client_details(self):
        class BrokenStore:
            def hit(self, key, limit, window):
                raise ConnectionError(f"store down for {key}")

        self.app.dependency_overrides[users.SIGNUP_RATE_LIMITER] = RateLimiter("signup", 5, 600, BrokenStore())
        with self.assertLogs("app.rate_limit", "ERROR") as logs:
            responses = [self.signup() for _ in range(7)]
        self.assertEqual([r.status_code for r in responses], [200] * 7)
        output = "".join(logs.output)
        self.assertIn("ConnectionError", output)
        self.assertNotIn("198.51.100.7", output)
        self.assertNotIn(PASSWORD, output)


class ProductionWiringTests(unittest.TestCase):
    """The shipped routes use the shipped limiter instances and defaults, with no override."""

    def setUp(self):
        use_test_privacy_policy(self)
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        Session = sessionmaker(bind=self.engine)
        app = FastAPI()
        app.include_router(auth.router)
        app.include_router(users.router)

        def test_db():
            with Session() as db:
                yield db

        app.dependency_overrides[get_db] = test_db
        self.app = app
        self.client = TestClient(app)
        for limiter in (users.SIGNUP_RATE_LIMITER, users.AVAILABILITY_RATE_LIMITER, auth.LOGIN_RATE_LIMITER):
            limiter.reset()
            self.addCleanup(limiter.reset)

    def tearDown(self):
        self.client.close()
        self.engine.dispose()

    def test_default_policy(self):
        self.assertEqual((users.SIGNUP_RATE_LIMITER.limit, users.SIGNUP_RATE_LIMITER.window_seconds), (5, 600))
        self.assertEqual((users.AVAILABILITY_RATE_LIMITER.limit, users.AVAILABILITY_RATE_LIMITER.window_seconds), (30, 60))
        self.assertEqual((auth.LOGIN_RATE_LIMITER.limit, auth.LOGIN_RATE_LIMITER.window_seconds), (10, 60))
        self.assertEqual(auth.LOGIN_RATE_LIMITER.message, LOGIN_TOO_MANY)

    def test_routes_use_the_shipped_limiters(self):
        expected = {("POST", "/users/"): users.SIGNUP_RATE_LIMITER,
                    ("GET", ID): users.AVAILABILITY_RATE_LIMITER, ("GET", NICK): users.AVAILABILITY_RATE_LIMITER,
                    ("POST", "/auth/login"): auth.LOGIN_RATE_LIMITER}
        found = {}
        for route in self.app.routes:
            for method in getattr(route, "methods", ()):
                if (method, route.path) in expected:
                    found[(method, route.path)] = [d.call for d in route.dependant.dependencies]
        for key, limiter in expected.items():
            with self.subTest(route=key):
                self.assertIs(found[key][0], limiter)  # first, so it runs before validation and the DB
        unlimited = [route for route in self.app.routes
                     if route.path in ("/users/me", "/users/{user_id}", "/auth/logout")]
        self.assertEqual(len(unlimited), 3)
        for route in unlimited:
            self.assertFalse({users.SIGNUP_RATE_LIMITER, users.AVAILABILITY_RATE_LIMITER, auth.LOGIN_RATE_LIMITER}
                             & {d.call for d in route.dependant.dependencies})

    def test_real_signup_limiter_returns_429_with_real_clock(self):
        for index in range(5):
            response = self.client.post("/users/", json={"login_id": f"real{index}", "nickname": f"real_{index}",
                                                         "password": PASSWORD, "privacy": privacy()})
            self.assertEqual(response.status_code, 200, response.text)
        response = self.client.post("/users/", json={"login_id": "real6", "nickname": "real_6",
                                                     "password": PASSWORD, "privacy": privacy()})
        self.assertEqual(response.status_code, 429)
        retry_after = response.json()["data"]["retry_after_seconds"]
        self.assertTrue(590 <= retry_after <= 600, retry_after)
        self.assertEqual(response.headers["Retry-After"], str(retry_after))

    def test_real_login_limiter_returns_429_with_real_clock(self):
        for _ in range(10):  # 422 without a body is cheap and still counts
            self.assertEqual(self.client.post("/auth/login", json={}).status_code, 422)
        response = self.client.post("/auth/login", json={})
        self.assertEqual((response.status_code, response.json()["message"]), (429, LOGIN_TOO_MANY))
        retry_after = response.json()["data"]["retry_after_seconds"]
        self.assertTrue(50 <= retry_after <= 60, retry_after)

    def test_real_availability_limiter_is_shared_with_real_clock(self):
        for index in range(30):
            path, params = (ID, {"login_id": "walker04"}) if index % 2 else (NICK, {"nickname": "산책왕"})
            self.assertEqual(self.client.get(path, params=params).status_code, 200)
        for path, params in ((ID, {"login_id": "walker04"}), (NICK, {"nickname": "산책왕"})):
            self.assertEqual(self.client.get(path, params=params).status_code, 429)


class MemoryWindowStoreTests(unittest.TestCase):
    def test_exact_count_under_heavy_concurrency(self):
        store, workers = MemoryWindowStore(FakeClock()), 200
        barrier = Barrier(workers)

        def hit(_):
            barrier.wait(10)
            return store.hit("client", 50, 60)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(hit, range(workers)))
        self.assertEqual((results.count(0), len(results) - results.count(0)), (50, 150))
        self.assertEqual(set(results) - {0}, {60})

    def test_expired_clients_are_swept(self):
        clock = FakeClock()
        store = MemoryWindowStore(clock)
        for index in range(100):
            store.hit(f"client_{index}", 5, 60)
        self.assertEqual(len(store), 100)
        clock.now += 60
        store.hit("new_client", 5, 60)
        self.assertEqual(len(store), 1)

    def test_retry_after_rounds_up_and_is_at_least_one_second(self):
        clock = FakeClock()
        store = MemoryWindowStore(clock)
        store.hit("client", 1, 60)
        for elapsed, expected in ((0, 60), (0.001, 60), (30.5, 30), (59.9999, 1)):
            with self.subTest(elapsed=elapsed):
                clock.now = 1000.0 + elapsed
                self.assertEqual(store.hit("client", 1, 60), expected)
                self.assertEqual(expected, max(1, math.ceil(60 - elapsed)))


class ConfigurationTests(unittest.TestCase):
    def test_limits_come_from_environment(self):
        values = {"SIGNUP_RATE_LIMIT_REQUESTS": "3", "SIGNUP_RATE_LIMIT_WINDOW_SECONDS": "120",
                  "LOGIN_RATE_LIMIT_REQUESTS": "4", "LOGIN_RATE_LIMIT_WINDOW_SECONDS": "30"}
        with patch.dict(os.environ, values):
            limiter = RateLimiter.from_env("signup", "SIGNUP", 5, 600)
            login = RateLimiter.from_env("login", "LOGIN", 10, 60, message=LOGIN_TOO_MANY)
        self.assertEqual((limiter.limit, limiter.window_seconds), (3, 120))
        self.assertEqual((login.limit, login.window_seconds, login.message), (4, 30, LOGIN_TOO_MANY))
        with patch.dict(os.environ, {"SIGNUP_RATE_LIMIT_REQUESTS": ""}):
            self.assertEqual(positive_int_env("SIGNUP_RATE_LIMIT_REQUESTS", 5), 5)

    def test_invalid_values_fail_at_startup(self):
        for raw in ("0", "-1", "ten", "1.5"):
            with self.subTest(raw=raw), patch.dict(os.environ, {"AVAILABILITY_RATE_LIMIT_REQUESTS": raw}), \
                    self.assertRaisesRegex(ValueError, "AVAILABILITY_RATE_LIMIT_REQUESTS"):
                RateLimiter.from_env("availability", "AVAILABILITY", 30, 60)


if __name__ == "__main__":
    unittest.main()
