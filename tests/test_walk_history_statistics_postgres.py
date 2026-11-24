"""Run the same owner history contract against a disposable PostgreSQL schema."""
import unittest

from test_date_slot_booking_postgres import IsolatedPostgres, TEST_URL
from test_walk_history_statistics import HistoryStatisticsCases


@unittest.skipUnless(TEST_URL, "격리 PostgreSQL이 필요합니다")
class WalkHistoryStatisticsPostgresTests(IsolatedPostgres, HistoryStatisticsCases, unittest.TestCase):
    def setUp(self):
        self.setup_history_api(self.isolated_engine())
