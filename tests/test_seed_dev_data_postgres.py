"""Isolated PostgreSQL verification for Issue #116 (regions + dev seed + non-mutation)."""
from datetime import date
import os
from pathlib import Path
import unittest
from uuid import uuid4
from unittest.mock import patch

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import (
    Dog,
    DogWalkDateSlot,
    DogWalkSetting,
    Shelter,
    User,
    UserConsent,
    UserPledgeAcknowledgement,
)
from app.region_master import read_snapshot, sync_regions
from scripts.prepare_demo_db import PreparationError, validate_local_engine
from scripts.seed_dev_data import (
    SEED_ACCOUNTS,
    VIRTUAL_DOGS,
    VIRTUAL_SHELTERS,
    match_shelter_regions,
    seed_dev_data,
)

TEST_URL = os.getenv("SIGNUP_TEST_DATABASE_URL")
ROOT = Path(__file__).resolve().parents[1]
REGION_ZIP = ROOT / "data" / "regions" / "legal_dong_code_full_20261123.zip"


@unittest.skipUnless(TEST_URL, "격리 PostgreSQL(SIGNUP_TEST_DATABASE_URL)이 필요합니다.")
class SeedDevDataPostgresTests(unittest.TestCase):
    def setUp(self):
        self.policy_environment = patch.dict(os.environ)
        self.policy_environment.start()
        self.addCleanup(self.policy_environment.stop)
        for key in (
            "PLEDGE_CURRENT_VERSION",
            "TERMS_CURRENT_VERSION",
            "PRIVACY_CURRENT_VERSION",
            "TERMS_DOCUMENT_VERSION",
            "PRIVACY_DOCUMENT_VERSION",
            "PRIVACY_REQUIRED_CONSENTS",
        ):
            os.environ.pop(key, None)

        admin = create_engine(TEST_URL)
        self.addCleanup(admin.dispose)
        validate_local_engine(admin)
        if not admin.url.database.startswith("toi_walk_test_"):
            raise PreparationError("빈 격리 PostgreSQL 테스트 DB가 필요합니다.")
        schema = "seed_dev_" + uuid4().hex
        with admin.begin() as conn:
            if inspect(conn).get_table_names(schema="public"):
                raise PreparationError("공용 스키마에 테이블이 없는 테스트 DB가 필요합니다.")
            conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')

        def drop_schema():
            with admin.begin() as conn:
                conn.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')

        self.addCleanup(drop_schema)
        self.engine = create_engine(
            TEST_URL,
            connect_args={"options": f"-csearch_path={schema} -clock_timeout=4000"},
        )
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)

        self.Session = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)

    def test_regions_seed_idempotency_fk_integrity_and_real_data_preserved(self):
        base_day = date(2026, 12, 15)
        with self.Session.begin() as db:
            region_report = sync_regions(db, read_snapshot(REGION_ZIP), apply=True)
            self.assertTrue(region_report["applied"])

            real_shelter = Shelter(
                name="공공실제보호소PG",
                external_code="311000020269999",
                phone="02-555-0001",
                address="서울특별시 강남구 논현로 500",
            )
            ambiguous_shelter = Shelter(
                name="모호주소보호소PG",
                address="중구 중앙로 1",
            )
            unmatched_shelter = Shelter(
                name="미매칭보호소PG",
                address="가상도시 없는구 12",
            )
            db.add_all([real_shelter, ambiguous_shelter, unmatched_shelter])
            db.flush()

            real_dog = Dog(
                shelter_id=real_shelter.id,
                desertion_no="411311202699999",
                name="공공실제강아지PG",
                breed="진도견",
                age=3,
                gender="M",
                trait_source="unknown",
            )
            db.add(real_dog)
            db.flush()
            real_shelter_id = real_shelter.id
            real_dog_id = real_dog.id

        with self.Session() as db:
            preview = seed_dev_data(db, apply=False, base_date_kst=base_day)
            db.rollback()
        self.assertEqual(preview["planned_creates"]["shelters"], len(VIRTUAL_SHELTERS))
        self.assertEqual(preview["planned_creates"]["dogs"], len(VIRTUAL_DOGS))
        self.assertEqual(preview["planned_creates"]["users"], len(SEED_ACCOUNTS))

        with self.Session.begin() as db:
            applied = seed_dev_data(db, apply=True, base_date_kst=base_day)
        self.assertTrue(applied["applied"])

        with self.Session() as db:
            accounts = {entry["login_id"]: entry["user_id"] for entry in applied["accounts"]}
            consents = db.query(UserConsent).filter_by(user_id=accounts["test_user_completed"]).all()
            self.assertEqual(
                {(row.consent_type, row.document_version) for row in consents},
                {("TERMS_OF_SERVICE", "terms-demo-v1"), ("PRIVACY_COLLECTION_USE", "privacy-demo-v1")},
            )
            onboarding_user = db.get(User, accounts["test_user_onboarding"])
            pledge = db.query(UserPledgeAcknowledgement).filter_by(user_id=onboarding_user.id).one()
            self.assertEqual(pledge.pledge_version, "pledge-demo-v1")

        with self.Session.begin() as db:
            rerun = seed_dev_data(db, apply=True, base_date_kst=base_day)
        self.assertTrue(all(v == 0 for v in rerun["planned_creates"].values()))

        with self.Session() as db:
            s_real = db.get(Shelter, real_shelter_id)
            d_real = db.get(Dog, real_dog_id)
            self.assertEqual(s_real.name, "공공실제보호소PG")
            self.assertIsNone(s_real.region_code)
            self.assertEqual(d_real.trait_source, "unknown")
            self.assertEqual(db.query(DogWalkSetting).filter(DogWalkSetting.dog_id == real_dog_id).count(), 0)
            self.assertEqual(db.query(DogWalkDateSlot).filter(DogWalkDateSlot.dog_id == real_dog_id).count(), 0)

            matching = match_shelter_regions(db)
            self.assertEqual(matching["evaluated_count"], 3)
            self.assertEqual(matching["matched_candidates"][0]["shelter_id"], real_shelter_id)
            self.assertEqual(matching["matched_candidates"][0]["candidate_region_code"], "11680")
            self.assertEqual(len(matching["ambiguous"]), 1)
            self.assertEqual(len(matching["unmatched"]), 1)


if __name__ == "__main__":
    unittest.main()
