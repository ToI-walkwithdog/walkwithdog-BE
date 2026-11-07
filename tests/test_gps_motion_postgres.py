"""Real additive 007 -> 008 migration, data preservation and fail-closed checks.

Only GPS_MOTION_TEST_DATABASE_URL, an EMPTY toi_walk_test_* local DB, is used.
No database/table drops, resets or production credentials are used.
"""
import os
from pathlib import Path
import unittest
from unittest.mock import patch

with patch.dict(os.environ, {'DATABASE_URL': 'sqlite://'}):
    from app.models import Base
    from app.walk_models import WALK_MODELS

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from scripts.migrate_backend import migrate
from scripts.prepare_demo_db import ROOT, PreparationError, table_counts, validate_local_engine
from scripts.walk_schema import check_motion_schema, check_walk_schema
from test_walk_postgres import TransactionEngine

TEST_URL = os.getenv('GPS_MOTION_TEST_DATABASE_URL')


@unittest.skipUnless(TEST_URL, 'GPS_MOTION_TEST_DATABASE_URL의 빈 격리 PostgreSQL이 필요합니다')
class MotionMigrationPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(TEST_URL)
        validate_local_engine(cls.engine)
        if not cls.engine.url.database.startswith('toi_walk_test_'):
            raise PreparationError('테스트 DB 이름은 toi_walk_test_로 시작해야 합니다.')
        with cls.engine.begin() as c:
            inspector = inspect(c)
            custom = [s for s in inspector.get_schema_names() if s not in ('public', 'information_schema') and not s.startswith('pg_')]
            if inspector.get_table_names() or custom:
                raise PreparationError('새로운 빈 격리 테스트 DB만 허용합니다.')
            walk_names = {m.__tablename__ for m in WALK_MODELS}
            Base.metadata.create_all(c, tables=[t for t in Base.metadata.sorted_tables if t.name not in walk_names])
            for statement in (ROOT / 'migrations/007_walk_domain.sql').read_text().split(';'):
                statement = '\n'.join(l for l in statement.splitlines() if not l.startswith('--')).strip()
                if statement and statement.upper() not in ('BEGIN', 'COMMIT'):
                    c.exec_driver_sql(statement)
            c.exec_driver_sql("INSERT INTO shelters(id,name) VALUES (1,'보호소')")
            c.exec_driver_sql("INSERT INTO users(id,email,nickname) VALUES (1,'motion@example.com','봉사자')")
            c.exec_driver_sql("INSERT INTO dogs(id,name,shelter_id) VALUES (1,'강아지',1)")
            c.exec_driver_sql("INSERT INTO walk_requests(id,user_id,dog_id,shelter_id,requested_start_at,requested_end_at) VALUES (1,1,1,1,'2030-01-07 00:00Z','2030-01-07 01:00Z')")
            c.exec_driver_sql("INSERT INTO walks(id,walk_request_id,status,started_at,ended_at,total_distance_m,duration_seconds,average_pace) VALUES (1,1,'COMPLETED','2030-01-07 00:00Z','2030-01-07 00:02Z',200,120,600)")

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        self.connection = self.engine.connect()
        self.transaction = self.connection.begin()
        self.addCleanup(self.connection.close)
        self.addCleanup(self.transaction.rollback)
        self.proxy = TransactionEngine(self.engine, self.connection)

    def apply(self):
        return migrate(self.proxy, '008', apply=True)

    def test_008_backup_apply_reapply_preserves_every_legacy_value(self):
        c = self.connection
        old = dict(c.execute(text('SELECT * FROM walks WHERE id=1')).mappings().one())
        counts = table_counts(c)
        preview = migrate(self.proxy, '008')
        self.assertEqual(len(preview['missing']), 6)
        self.assertIsNone(preview['backup'])
        result = self.apply()
        self.assertTrue(result['applied'])
        self.assertGreater(Path(result['backup']).stat().st_size, 0)
        self.assertEqual(result['counts_before'], result['counts_after'])
        self.assertEqual(table_counts(c), counts)
        new = dict(c.execute(text('SELECT * FROM walks WHERE id=1')).mappings().one())
        self.assertEqual({k: new[k] for k in old}, old)
        self.assertEqual([new[k] for k in set(new) - set(old)], [None] * 4)
        self.assertEqual((check_walk_schema(c), check_motion_schema(c)), ([], []))
        self.assertFalse(self.apply()['applied'])

    def test_008_rejects_partial_negative_inconsistent_and_invalid_pace_metrics(self):
        self.apply()
        invalid = [
            'moving_duration_seconds=0',
            'moving_duration_seconds=-1,stopped_duration_seconds=0,unknown_duration_seconds=121,moving_distance_m=1',
            'moving_duration_seconds=120,stopped_duration_seconds=0,unknown_duration_seconds=1,moving_distance_m=200',
            'moving_duration_seconds=0,stopped_duration_seconds=120,unknown_duration_seconds=0,moving_distance_m=0',
            'moving_duration_seconds=120,stopped_duration_seconds=0,unknown_duration_seconds=0,moving_distance_m=201',
            'moving_duration_seconds=2147483647,stopped_duration_seconds=2147483647,unknown_duration_seconds=2147483647,moving_distance_m=200',
        ]
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(IntegrityError):
                with self.connection.begin_nested():
                    self.connection.exec_driver_sql('UPDATE walks SET ' + changes + ' WHERE id=1')
        self.connection.exec_driver_sql('UPDATE walks SET moving_duration_seconds=100, stopped_duration_seconds=20,unknown_duration_seconds=0,moving_distance_m=200,average_pace=500 WHERE id=1')
        self.assertEqual(check_motion_schema(self.connection), [])

    def test_008_unknown_only_valid_and_legacy_pace_constraint_preserved(self):
        self.apply()
        self.connection.exec_driver_sql('UPDATE walks SET moving_duration_seconds=0,stopped_duration_seconds=0,unknown_duration_seconds=120,moving_distance_m=0,average_pace=NULL WHERE id=1')
        with self.assertRaises(IntegrityError):
            with self.connection.begin_nested():
                self.connection.exec_driver_sql('UPDATE walks SET average_pace=1 WHERE id=1')

    def test_008_incompatible_existing_column_stops_before_backup(self):
        self.connection.exec_driver_sql('ALTER TABLE walks ADD COLUMN moving_duration_seconds BIGINT')
        with patch('scripts.migrate_backend.backup_database') as backup:
            with self.assertRaises(PreparationError):
                self.apply()
            backup.assert_not_called()

    def test_008_wrong_default_or_unvalidated_constraint_stops(self):
        self.apply()
        for sql in (
            'ALTER TABLE walks ALTER COLUMN moving_duration_seconds SET DEFAULT 0',
            'ALTER TABLE walks DROP CONSTRAINT ck_walk_motion_pace; ALTER TABLE walks ADD CONSTRAINT ck_walk_motion_pace CHECK (moving_duration_seconds IS NULL OR average_pace IS NULL OR (moving_duration_seconds > 0 AND moving_distance_m > 0)) NOT VALID',
            'ALTER TABLE walks ADD CONSTRAINT unexpected_motion_check CHECK (moving_duration_seconds >= 0)',
        ):
            with self.connection.begin_nested() as savepoint:
                self.connection.exec_driver_sql(sql)
                with self.subTest(sql=sql), self.assertRaises(PreparationError):
                    check_motion_schema(self.connection)
                savepoint.rollback()

    def test_008_sql_failure_rolls_back_all_additions(self):
        original = Path.read_text
        def read(path, *args, **kwargs):
            value = original(path, *args, **kwargs)
            return value + '\nSELECT 1/0;' if path.name == '008_gps_motion_analysis.sql' else value
        with patch.object(Path, 'read_text', read), self.assertRaises(DBAPIError):
            self.apply()
        self.assertEqual(len(check_motion_schema(self.connection)), 6)
        self.assertEqual(self.connection.execute(text('SELECT average_pace FROM walks WHERE id=1')).scalar_one(), 600)

    def test_008_sql_can_complete_compatible_partial_addition(self):
        self.connection.exec_driver_sql('ALTER TABLE walks ADD COLUMN moving_duration_seconds INTEGER')
        self.assertEqual(len(check_motion_schema(self.connection)), 5)
        # Fixture-only uncommitted ALTER holds an exclusive lock: a separate
        # pg_dump would wait on this very transaction. Exercise SQL completion
        # here; real runner/backup behavior is covered on committed 007 above.
        self.connection.exec_driver_sql((ROOT / 'migrations/008_gps_motion_analysis.sql').read_text())
        self.assertEqual(check_motion_schema(self.connection), [])
