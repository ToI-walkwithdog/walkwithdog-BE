"""Synthetic recording fixtures reuse Issue #3 users, shelters, settings and auth."""
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from importlib import import_module
from threading import Event
from unittest.mock import patch

from walk_request_test_support import RequestFixture, START
from app.routers import walks


class WalkFixture(RequestFixture):
    def setup_walk_api(self, engine):
        self.setup_api(engine)
        self.app.include_router(walks.router)
        self.record_clock = self.enterContext(patch("app.walk_service.utc_now", return_value=START))
        self.request_clock = self.enterContext(patch("app.walk_request_service.utc_now", return_value=START))

    def advance(self, seconds):
        self.record_clock.return_value = START + timedelta(seconds=seconds)
        self.request_clock.return_value = START + timedelta(seconds=seconds)

    def start(self, request_id, user=1, **extra):
        return self.client.post("/walks", headers=self.auth(user), json={"walk_request_id": request_id, **extra})

    def begin(self, user=1, dog=1):
        request_id = self.seed_request(user=user, dog=dog, status="APPROVED")
        response = self.start(request_id, user)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    @staticmethod
    def point(sequence=0, seconds=0, latitude=0, longitude=0, accuracy_m=5):
        return {"sequence_no": sequence, "latitude": latitude, "longitude": longitude,
                "accuracy_m": accuracy_m, "recorded_at": (START + timedelta(seconds=seconds)).isoformat()}

    def upload(self, walk_id, points, user=1):
        return self.client.post(f"/walks/{walk_id}/points", headers=self.auth(user), json={"points": points})

    def marker(self, identifier, user=1, seconds=0, **extra):
        return self.client.post(f"/walks/{identifier}/markers", headers=self.auth(user), json={
            "marker_type": "SNIFF", "latitude": 0, "longitude": 0,
            "recorded_at": (START + timedelta(seconds=seconds)).isoformat(), **extra})

    def incident(self, walk_id, user=1, seconds=0, **extra):
        return self.client.post(f"/walks/{walk_id}/incidents", headers=self.auth(user), json={
            "incident_type": "INJURY", "description": "안전 확인 필요",
            "occurred_at": (START + timedelta(seconds=seconds)).isoformat(), **extra})

    def end(self, walk_id, user=1, **payload):
        return self.client.patch(f"/walks/{walk_id}/end", headers=self.auth(user), json=payload)

    def detail(self, walk_id, user=1, query=""):
        return self.client.get(f"/walks/{walk_id}{query}", headers=self.auth(user))

    def recording_race(self, first, second, hook="app.walk_service.locked_walk"):
        module_name, attribute = hook.rsplit(".", 1)
        original = getattr(import_module(module_name), attribute)
        entered, release = Event(), Event()

        def gated(*args, **kwargs):
            result = original(*args, **kwargs)
            if not entered.is_set():
                entered.set()
                if not release.wait(8):
                    raise AssertionError("recording race barrier timed out")
            return result

        with patch(hook, side_effect=gated), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(first)
            try:
                self.assertTrue(entered.wait(8), "first transaction did not acquire real DB locks")
                loser = second()
            finally:
                release.set()
            return future.result(timeout=10), loser
