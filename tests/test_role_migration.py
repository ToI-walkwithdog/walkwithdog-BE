"""Role migration validation never connects to the development database."""
import unittest
from unittest.mock import MagicMock, Mock, patch

from sqlalchemy import String
from sqlalchemy.dialects import postgresql

from scripts.migrate_backend import check_user_role, migrate
from scripts.prepare_demo_db import PreparationError


class RoleMigrationTests(unittest.TestCase):
    def setUp(self):
        self.connection = Mock()
        self.connection.dialect = postgresql.dialect()
        self.inspector = Mock(default_schema_name="public")
        self.inspector.get_enums.return_value = [{"name": "userrole", "labels": ["USER", "SHELTER", "ADMIN"]}]
        self.inspector.get_columns.return_value = [{"name": "role", "nullable": False,
                                                    "type": postgresql.ENUM("USER", "SHELTER", "ADMIN", name="userrole")}]

    def check(self):
        with patch("scripts.migrate_backend.inspect", return_value=self.inspector):
            return check_user_role(self.connection)

    def test_existing_compatible_role_needs_no_changes(self):
        self.assertEqual(self.check(), [])
        self.connection.execute.assert_not_called()

    def test_absent_type_and_column_are_reported(self):
        self.inspector.get_enums.return_value = []
        self.inspector.get_columns.return_value = [{"name": "id"}]
        self.assertEqual(self.check(), ["userrole", "users.role"])

    def test_different_enum_values_stop_migration(self):
        self.inspector.get_enums.return_value = [{"name": "userrole", "labels": ["user", "shelter", "admin"]}]
        with self.assertRaises(PreparationError):
            self.check()

    def test_wrong_type_or_nullable_role_stops_migration(self):
        for change in ({"type": String(20)}, {"nullable": True}):
            with self.subTest(change=change):
                self.inspector.get_columns.return_value = [{"name": "role", "nullable": False,
                                                            "type": postgresql.ENUM(name="userrole"), **change}]
                with self.assertRaises(PreparationError):
                    self.check()

    def test_role_preview_does_not_backup_or_apply(self):
        engine = MagicMock()
        engine.url.host = "localhost"
        engine.url.database = "isolated"
        with patch("scripts.migrate_backend.validate_local_engine"), \
                patch("scripts.migrate_backend.check_version", return_value=["users.role"]), \
                patch("scripts.migrate_backend.table_counts", return_value={"users": 3}), \
                patch("scripts.migrate_backend.backup_database") as backup:
            result = migrate(engine, "005")
        self.assertFalse(result["applied"])
        backup.assert_not_called()
        engine.begin.assert_not_called()

    def test_role_apply_preserves_do_block_and_checks_existing_rows(self):
        engine = MagicMock()
        connection = engine.begin.return_value.__enter__.return_value
        with patch("scripts.migrate_backend.validate_local_engine"), \
                patch("scripts.migrate_backend.check_version", side_effect=[["users.role"], ["users.role"], []]), \
                patch("scripts.migrate_backend.table_counts", side_effect=[{"users": 3}, {"users": 3}]), \
                patch("scripts.migrate_backend.backup_database", return_value="isolated-backup"):
            result = migrate(engine, "005", apply=True)
        self.assertTrue(result["applied"])
        connection.exec_driver_sql.assert_called_once()
        source = connection.exec_driver_sql.call_args.args[0]
        self.assertIn("DO $$", source)
        self.assertIn("END;\n$$;", source)
        self.assertIn("ADD COLUMN IF NOT EXISTS role", source)
