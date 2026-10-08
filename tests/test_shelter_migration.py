"""Migration planning and validation never connect to the configured database."""
import os
import unittest
from unittest.mock import MagicMock, Mock, patch

from sqlalchemy import Integer, String
from sqlalchemy.dialects import postgresql

with patch.dict(os.environ, {"DATABASE_URL": "sqlite://"}):
    from app import models

from scripts.migrate_backend import check_user_shelter, check_version, migrate
from scripts.prepare_demo_db import PreparationError


class ShelterMigrationTests(unittest.TestCase):
    def setUp(self):
        self.connection = Mock()
        self.connection.dialect = postgresql.dialect()
        self.inspector = Mock(default_schema_name="public")
        self.column = {"name": "shelter_id", "type": Integer(), "nullable": True, "default": None}
        self.foreign = {"constrained_columns": ["shelter_id"], "referred_table": "shelters",
                        "referred_columns": ["id"], "referred_schema": None, "options": {}}
        self.index = {"name": "ix_users_shelter_id", "column_names": ["shelter_id"], "unique": False}
        self.inspector.get_columns.return_value = [self.column]
        self.inspector.get_foreign_keys.return_value = [self.foreign]
        self.inspector.get_indexes.return_value = [self.index]

    def check(self):
        with patch("scripts.migrate_backend.inspect", return_value=self.inspector):
            return check_user_shelter(self.connection)

    def test_compatible_schema_requires_no_changes(self):
        self.assertEqual(self.check(), [])
        self.connection.execute.assert_not_called()

    def test_missing_column_fk_and_index_are_reported(self):
        self.inspector.get_columns.return_value = [{"name": "id"}]
        self.inspector.get_foreign_keys.return_value = []
        self.inspector.get_indexes.return_value = []
        self.assertEqual(self.check(), ["users.shelter_id", "fk_users_shelter_id", "ix_users_shelter_id"])

    def test_partial_migration_reports_only_missing_constraints(self):
        self.inspector.get_foreign_keys.return_value = []
        self.inspector.get_indexes.return_value = []
        self.assertEqual(self.check(), ["fk_users_shelter_id", "ix_users_shelter_id"])

    def test_wrong_column_type_nullability_or_default_stops_migration(self):
        for change in ({"type": String()}, {"nullable": False}, {"default": "1"}):
            self.inspector.get_columns.return_value = [{**self.column, **change}]
            with self.subTest(change=change), self.assertRaises(PreparationError):
                self.check()

    def test_incompatible_foreign_keys_stop_migration(self):
        for change in ({"referred_table": "dogs"}, {"referred_columns": ["external_code"]},
                       {"referred_schema": "other"}, {"options": {"ondelete": "CASCADE"}},
                       {"options": {"onupdate": "CASCADE"}}, {"constrained_columns": ["id", "shelter_id"]}):
            self.inspector.get_foreign_keys.return_value = [{**self.foreign, **change}]
            with self.subTest(change=change), self.assertRaises(PreparationError):
                self.check()

    def test_wrong_index_stops_migration(self):
        for change in ({"unique": True}, {"column_names": ["id"]},
                       {"dialect_options": {"postgresql_where": "shelter_id > 1"}}):
            self.inspector.get_indexes.return_value = [{**self.index, **change}]
            with self.subTest(change=change), self.assertRaises(PreparationError):
                self.check()

    def test_version_dispatch(self):
        with patch("scripts.migrate_backend.check_user_shelter", return_value=["users.shelter_id"]) as check:
            self.assertEqual(check_version(self.connection, "006"), ["users.shelter_id"])
            check.assert_called_once_with(self.connection)

    def test_preview_never_backs_up_or_applies(self):
        engine = MagicMock()
        with patch("scripts.migrate_backend.validate_local_engine"), \
                patch("scripts.migrate_backend.check_version", return_value=["users.shelter_id"]), \
                patch("scripts.migrate_backend.table_counts", return_value={"users": 3}), \
                patch("scripts.migrate_backend.backup_database") as backup:
            result = migrate(engine, "006")
        self.assertFalse(result["applied"])
        backup.assert_not_called()
        engine.begin.assert_not_called()

    def test_apply_preserves_do_block_and_has_no_data_rewrites(self):
        engine = MagicMock()
        connection = engine.begin.return_value.__enter__.return_value
        with patch("scripts.migrate_backend.validate_local_engine"), \
                patch("scripts.migrate_backend.check_version", side_effect=[["users.shelter_id"], ["users.shelter_id"], []]), \
                patch("scripts.migrate_backend.table_counts", side_effect=[{"users": 3}, {"users": 3}]), \
                patch("scripts.migrate_backend.backup_database", return_value="isolated-backup"):
            self.assertTrue(migrate(engine, "006", apply=True)["applied"])
        connection.exec_driver_sql.assert_called_once()
        source = connection.exec_driver_sql.call_args.args[0]
        self.assertIn("DO $$", source)
        self.assertIn("END;\n$$;", source)
        self.assertIn("REFERENCES shelters(id)", source)
        self.assertIn("ADD COLUMN IF NOT EXISTS shelter_id INTEGER;", source)
        self.assertNotRegex(source.upper(), r"\b(UPDATE|DELETE|DROP|TRUNCATE)\b")

    def test_existing_complete_schema_does_not_reapply(self):
        engine = MagicMock()
        with patch("scripts.migrate_backend.validate_local_engine"), \
                patch("scripts.migrate_backend.check_version", return_value=[]), \
                patch("scripts.migrate_backend.table_counts", return_value={"users": 3}), \
                patch("scripts.migrate_backend.backup_database") as backup:
            self.assertFalse(migrate(engine, "006", apply=True)["applied"])
        backup.assert_not_called()
        engine.begin.assert_not_called()

    def test_backup_failure_prevents_apply(self):
        engine = MagicMock()
        with patch("scripts.migrate_backend.validate_local_engine"), \
                patch("scripts.migrate_backend.check_version", return_value=["users.shelter_id"]), \
                patch("scripts.migrate_backend.table_counts", return_value={"users": 3}), \
                patch("scripts.migrate_backend.backup_database", side_effect=PreparationError("backup failed")):
            with self.assertRaises(PreparationError):
                migrate(engine, "006", apply=True)
        engine.begin.assert_not_called()

    def test_row_count_change_fails_transaction_validation(self):
        engine = MagicMock()
        with patch("scripts.migrate_backend.validate_local_engine"), \
                patch("scripts.migrate_backend.check_version", side_effect=[["users.shelter_id"], ["users.shelter_id"], []]), \
                patch("scripts.migrate_backend.table_counts", side_effect=[{"users": 3}, {"users": 2}]), \
                patch("scripts.migrate_backend.backup_database", return_value="isolated-backup"):
            with self.assertRaisesRegex(PreparationError, "행 수"):
                migrate(engine, "006", apply=True)
        self.assertEqual(engine.begin.return_value.__exit__.call_args.args[0], PreparationError)


if __name__ == "__main__":
    unittest.main()
