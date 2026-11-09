"""Portable ORM constraints and the read-only catalog contract; no configured DB access."""
import copy
import contextlib
import json
import os
import unittest
from unittest.mock import MagicMock, patch

with patch.dict(os.environ, {"DATABASE_URL": "sqlite://"}):
    from app.models import Base, Dog, Shelter, User
    from app.walk_models import (
        WALK_MODELS, DogWalkSetting, DogWalkSlot, DogTraitSummary, VolunteerProfile,
        Walk, WalkIncident, WalkMarker, WalkPoint, WalkRequest, WalkReview,
    )

from sqlalchemy import create_engine, event
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import configure_mappers, sessionmaker
from sqlalchemy.schema import CreateIndex, CreateTable

from scripts.migrate_backend import check_version, migrate
from scripts.prepare_demo_db import PreparationError, ROOT
from scripts.walk_schema import CONTRACT, MOTION_CONTRACT, compare_table


class WalkMetadataTests(unittest.TestCase):
    def test_all_ten_models_register_with_existing_base_and_relationships(self):
        configure_mappers()
        self.assertEqual(len(WALK_MODELS), 10)
        self.assertTrue(all(model.metadata is Base.metadata for model in WALK_MODELS))
        self.assertEqual(set(json.loads(CONTRACT.read_text())), {m.__tablename__ for m in WALK_MODELS})
        for model in WALK_MODELS:
            for relationship in model.__mapper__.relationships:
                self.assertIsNotNone(relationship.back_populates)
            for foreign in model.__table__.foreign_keys:
                self.assertEqual(foreign.ondelete, "NO ACTION")

    def test_checked_in_sql_exactly_matches_orm_postgresql_ddl(self):
        names = {m.__tablename__ for m in WALK_MODELS}
        source = (ROOT / "migrations/007_walk_domain.sql").read_text()
        statements = []
        motion = json.loads(MOTION_CONTRACT.read_text())
        additions = {c["name"] for c in motion["columns"]} | {"CONSTRAINT " + n for n in motion["constraints"]}
        for table in Base.metadata.sorted_tables:
            if table.name in names:
                ddl = str(CreateTable(table, if_not_exists=True).compile(dialect=postgresql.dialect())).strip()
                if table.name == "walks":
                    # Compare the immutable 007 projection, excluding reviewed 008 additions.
                    ddl = "\n".join(l for l in ddl.splitlines() if not any(l.strip().startswith(n + " ") for n in additions))
                statements.append(ddl + ";")
                statements.extend(str(CreateIndex(i, if_not_exists=True).compile(dialect=postgresql.dialect())) + ";"
                                  for i in sorted(table.indexes, key=lambda i: i.name))
        actual = "\n".join(line for line in source.splitlines() if not line.startswith("--"))
        expected = "BEGIN;\n\n" + "\n\n".join(statements) + "\n\nCOMMIT;"
        self.assertEqual([l.rstrip() for l in actual.strip().splitlines()],
                         [l.rstrip() for l in expected.splitlines()])
        self.assertNotRegex(source.upper(), r"(?m)^\s*(DROP|TRUNCATE|DELETE|UPDATE|INSERT)\b")

    def test_types_nullability_defaults_and_all_constraint_contracts(self):
        contract = json.loads(CONTRACT.read_text())
        for model in WALK_MODELS:
            table = model.__table__
            snapshot = contract[table.name]
            if table.name == "walks":
                motion = json.loads(MOTION_CONTRACT.read_text())
                snapshot["columns"].extend(motion["columns"])
                snapshot["constraints"].update(motion["constraints"])
            expected_columns = {c["name"]: c for c in snapshot["columns"]}
            self.assertEqual(set(expected_columns), set(table.columns.keys()))
            for column in table.columns:
                expected = expected_columns[column.name]
                self.assertEqual(column.nullable, not expected["not_null"])
                if column.type.__class__.__name__ == "Numeric":
                    self.assertEqual(expected["type"], f"numeric({column.type.precision},{column.type.scale})")
                if column.type.__class__.__name__ == "DateTime":
                    self.assertTrue(column.type.timezone)
                if column.server_default is not None:
                    self.assertIsNotNone(expected["default"])
            for constraint in table.constraints:
                if constraint.name:
                    self.assertIn(constraint.name, snapshot["constraints"])
            for index in table.indexes:
                self.assertIn(index.name, snapshot["indexes"])


class WalkConstraintTests(unittest.TestCase):
    def setUp(self):
        from datetime import datetime, timedelta, timezone
        self.start = datetime(2026, 11, 2, 1, tzinfo=timezone.utc)
        self.end = self.start + timedelta(hours=1)
        self.engine = create_engine("sqlite://")
        event.listen(self.engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
        Base.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        self.db = sessionmaker(bind=self.engine)()
        self.addCleanup(self.db.close)
        self.db.add_all([User(id=1, email="walk@example.com", nickname="봉사자"), Shelter(id=1, name="보호소")])
        self.db.flush()
        self.db.add(Dog(id=1, name="강아지", shelter_id=1))
        self.db.flush()
        self.request = WalkRequest(user_id=1, dog_id=1, shelter_id=1,
                                   requested_start_at=self.start, requested_end_at=self.end)
        self.db.add(self.request)
        self.db.flush()
        self.walk = Walk(walk_request_id=self.request.id, started_at=self.start)
        self.db.add(self.walk)
        self.db.flush()

    def invalid(self, instance):
        with self.assertRaises(IntegrityError):
            with self.db.begin_nested():
                self.db.add(instance)
                self.db.flush()

    def test_new_public_dog_not_automatically_walkable(self):
        self.assertIsNone(self.db.get(Dog, 1).walk_setting)
        self.invalid(DogWalkSetting(dog_id=1, difficulty="UNKNOWN"))
        self.invalid(DogWalkSetting(dog_id=1, recommended_duration_minutes=0))
        setting = DogWalkSetting(dog_id=1)
        self.db.add(setting)
        self.db.flush()
        self.assertFalse(setting.walkable)
        self.assertEqual(setting.difficulty, "BEGINNER")
        self.invalid(DogWalkSetting(dog_id=1))

    def test_slot_order_weekday_and_duplicate(self):
        from datetime import time
        setting = DogWalkSetting(dog_id=1)
        self.db.add(setting)
        self.db.flush()
        slot = dict(walk_setting_id=setting.id, day_of_week=0, start_time=time(9), end_time=time(10))
        self.db.add(DogWalkSlot(**slot))
        self.db.flush()
        for changes in ({}, {"day_of_week": 7}, {"end_time": time(8)}):
            self.invalid(DogWalkSlot(**{**slot, **changes}))

    def test_request_times_duplicate_and_review_metadata(self):
        data = dict(user_id=1, dog_id=1, shelter_id=1, requested_start_at=self.start, requested_end_at=self.end)
        for changes in ({}, {"requested_end_at": self.start}, {"status": "COMPLETED"},
                        {"status": "APPROVED"}, {"status": "REJECTED"},
                        {"status": "CANCELLED", "reviewed_by": 1}):
            if changes:
                from datetime import timedelta
                changes = {"requested_start_at": self.start + timedelta(minutes=15), **changes}
            self.invalid(WalkRequest(**{**data, **changes}))
        # Cancelling an application releases its exact-duplicate key.
        self.request.status = "CANCELLED"
        self.db.flush()
        self.db.add(WalkRequest(**data))
        self.db.flush()

    def test_one_walk_per_request(self):
        self.invalid(Walk(walk_request_id=self.request.id, started_at=self.start))

    def test_walk_end_status_and_metrics(self):
        for changes in ({"status": "COMPLETED"}, {"ended_at": self.end},
                        {"total_distance_m": -1}, {"duration_seconds": -1}, {"average_pace": 120}):
            with self.assertRaises(IntegrityError):
                with self.db.begin_nested():
                    for name, value in changes.items():
                        setattr(self.walk, name, value)
                    self.db.flush()
        self.walk.status, self.walk.ended_at = "COMPLETED", self.end
        self.walk.total_distance_m, self.walk.duration_seconds = 0, 0
        self.db.flush()  # A zero-distance walk has NULL pace, never a made-up value.

    def test_point_ranges_order_and_retry_key(self):
        data = dict(walk_id=self.walk.id, latitude=37.123456, longitude=127.123456,
                    recorded_at=self.start, sequence_no=0)
        self.db.add(WalkPoint(**data))
        self.db.flush()
        for changes in ({}, {"latitude": 91}, {"longitude": -181}, {"sequence_no": -1}, {"accuracy_m": -1}):
            if changes:
                changes = {"sequence_no": 1, **changes}
            self.invalid(WalkPoint(**{**data, **changes}))
        self.invalid(WalkPoint(**{**data, "walk_id": 999}))

    def test_marker_type_and_incident_pair(self):
        self.invalid(WalkMarker(walk_id=self.walk.id, marker_type="UNKNOWN", latitude=0, longitude=0, recorded_at=self.start))
        incident = dict(walk_id=self.walk.id, reported_by=1, incident_type="INJURY", description="안전 문제", occurred_at=self.start)
        for changes in ({"latitude": 37}, {"incident_type": "UNKNOWN"}, {"status": "UNKNOWN"}, {"description": ""}):
            self.invalid(WalkIncident(**{**incident, **changes}))
        self.db.add_all([WalkIncident(**incident), WalkIncident(**incident)])
        self.db.flush()

    def test_review_observations_and_uniqueness(self):
        self.invalid(WalkReview(walk_id=self.walk.id))
        for value in (0, 6):
            self.invalid(WalkReview(walk_id=self.walk.id, leash_pulling=value))
        self.db.add(WalkReview(walk_id=self.walk.id, sociability_with_people=4))
        self.db.flush()
        self.invalid(WalkReview(walk_id=self.walk.id, barking_level=3))

    def test_unknown_traits_and_initial_profile(self):
        summary = DogTraitSummary(dog_id=1, calculation_version="walk-v1")
        profile = VolunteerProfile(user_id=1)
        self.db.add_all([summary, profile])
        self.db.flush()
        self.assertIsNone(summary.activity_level)
        self.assertIsNone(summary.evidence)
        self.assertEqual((summary.walk_count, summary.review_count, profile.level, profile.total_walk_count), (0, 0, 1, 0))
        with self.assertRaises(IntegrityError):
            with self.db.begin_nested():
                summary.activity_level = 3
                self.db.flush()

    def test_summary_evidence_counts_score_and_profile_totals(self):
        summary = DogTraitSummary(dog_id=1, calculation_version="walk-v1", walk_count=1)
        profile = VolunteerProfile(user_id=1)
        self.db.add_all([summary, profile])
        self.db.flush()
        for target, changes in (
            (summary, {"review_count": 2}), (summary, {"walk_count": -1}),
            (summary, {"calculation_version": ""}),
            (summary, {"activity_level": 3}),
            (summary, {"activity_level": 6, "evidence": {"sample_count": 1}}),
            (profile, {"level": 0}), (profile, {"total_distance_m": -1}),
            (profile, {"total_duration_seconds": -1}), (profile, {"unique_dog_count": 1}),
        ):
            with self.subTest(changes=changes), self.assertRaises(IntegrityError):
                with self.db.begin_nested():
                    for key, value in changes.items():
                        setattr(target, key, value)
                    self.db.flush()
        summary.activity_level, summary.evidence = 3.25, {"activity_level": {"sample_count": 1}}
        self.db.flush()

    def test_orm_delete_keeps_history_including_nullable_reviewer(self):
        self.request.status = "APPROVED"
        self.request.reviewed_by, self.request.reviewed_at = 1, self.start
        self.db.flush()
        self.assertEqual(len(self.db.get(User, 1).reviewed_walk_requests), 1)
        with self.assertRaises(IntegrityError):
            with self.db.begin_nested():
                self.db.delete(self.db.get(User, 1))
                self.db.flush()
        self.assertEqual(self.request.reviewed_by, 1)


class WalkCatalogContractTests(unittest.TestCase):
    def setUp(self):
        self.expected = json.loads(CONTRACT.read_text())["walk_requests"]
        self.standalone = {i.name for i in WalkRequest.__table__.indexes}

    def test_complete_missing_table_and_missing_index(self):
        self.assertEqual(compare_table("walk_requests", self.expected, self.expected, self.standalone), [])
        self.assertEqual(compare_table("walk_requests", None, self.expected, self.standalone), ["walk_requests"])
        actual = copy.deepcopy(self.expected)
        del actual["indexes"]["ix_walk_requests_reviewer"]
        self.assertEqual(compare_table("walk_requests", actual, self.expected, self.standalone), ["ix_walk_requests_reviewer"])

    def test_every_column_property_mismatch_blocks(self):
        for field, value in (("type", "bigint"), ("not_null", False), ("default", "42"),
                             ("identity", "a"), ("generated", "s"), ("sequence", "public.foreign_seq")):
            actual = copy.deepcopy(self.expected)
            actual["columns"][0][field] = value
            with self.subTest(field=field), self.assertRaises(PreparationError):
                compare_table("walk_requests", actual, self.expected, self.standalone)

    def test_all_constraint_and_index_changes_block(self):
        for category in ("constraints", "indexes"):
            for name, definition in self.expected[category].items():
                for field, value in definition.items():
                    actual = copy.deepcopy(self.expected)
                    actual[category][name][field] = not value if isinstance(value, bool) else value + " invalid"
                    with self.subTest(category=category, name=name, field=field), self.assertRaises(PreparationError):
                        compare_table("walk_requests", actual, self.expected, self.standalone)
        actual = copy.deepcopy(self.expected)
        del actual["constraints"]["ck_status"]
        with self.assertRaises(PreparationError):
            compare_table("walk_requests", actual, self.expected, self.standalone)


class WalkMigrationRunnerTests(unittest.TestCase):
    def setUp(self):
        self.engine = MagicMock()
        self.connection = self.engine.begin.return_value.__enter__.return_value
        self.connection.dialect.identifier_preparer.quote.side_effect = lambda name: '"' + name + '"'
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch("scripts.migrate_backend.validate_local_engine"))
        self.check = self.stack.enter_context(patch("scripts.migrate_backend.check_version", side_effect=[["walks"], ["walks"], []]))
        self.counts = self.stack.enter_context(patch("scripts.migrate_backend.table_counts", return_value={"users": 2}))
        self.backup = self.stack.enter_context(patch("scripts.migrate_backend.backup_database", return_value="validated.dump"))
        inspector = self.stack.enter_context(patch("scripts.migrate_backend.inspect")).return_value
        inspector.get_table_names.return_value = ["users"]

    def test_preview_and_reapply_do_not_write_or_backup(self):
        self.assertFalse(migrate(self.engine, "007")["applied"])
        self.check.side_effect = None
        self.check.return_value = []
        self.assertFalse(migrate(self.engine, "007", apply=True)["applied"])
        self.backup.assert_not_called()
        self.engine.begin.assert_not_called()

    def test_apply_locks_validates_and_preserves_rows(self):
        self.assertTrue(migrate(self.engine, "007", apply=True)["applied"])
        self.assertEqual(self.counts.call_count, 3)
        sql = [str(c.args[0]) for c in self.connection.execute.call_args_list]
        self.assertTrue(any("LOCK TABLE" in s for s in sql))
        self.assertFalse(any(s.strip().upper() in ("BEGIN", "COMMIT") for s in sql))

    def test_conflict_and_backup_failure_prevent_transaction(self):
        self.check.side_effect = PreparationError("schema conflict")
        with self.assertRaises(PreparationError):
            migrate(self.engine, "007", apply=True)
        self.backup.assert_not_called()
        self.engine.begin.assert_not_called()
        self.check.side_effect = [["walks"]]
        self.backup.side_effect = PreparationError("backup failed")
        with self.assertRaises(PreparationError):
            migrate(self.engine, "007", apply=True)
        self.engine.begin.assert_not_called()

    def test_sql_failure_postcheck_and_row_changes_rollback(self):
        for failure in ("sql", "schema", "rows", "backup_race"):
            self.check.side_effect = [["walks"], ["walks"], ["walks"] if failure == "schema" else []]
            self.counts.side_effect = [{"users": 2}, {"users": 3 if failure == "backup_race" else 2},
                                       {"users": 1 if failure == "rows" else 2}]
            self.connection.execute.side_effect = PreparationError("SQL failed") if failure == "sql" else None
            with self.subTest(failure=failure), self.assertRaises(PreparationError):
                migrate(self.engine, "007", apply=True)
            self.assertEqual(self.engine.begin.return_value.__exit__.call_args.args[0], PreparationError)

    def test_version_dispatch(self):
        with patch("scripts.walk_schema.check_walk_schema", return_value=["walks"]) as checker:
            self.assertEqual(check_version(self.connection, "007"), ["walks"])
            checker.assert_called_once_with(self.connection)


if __name__ == "__main__":
    unittest.main()
