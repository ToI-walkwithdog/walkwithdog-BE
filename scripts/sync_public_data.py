"""Bounded official data sync: preview by default, backup before --apply."""
import argparse
import json

from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError

from app.database import engine, SessionLocal
from app.public_data import fetch_pages, synchronize
from scripts.prepare_demo_db import ROOT, backup_database, validate_local_engine, PreparationError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--max-pages", type=int, default=2, choices=range(1, 11))
    parser.add_argument("--rows", type=int, default=20, choices=range(1, 101))
    args = parser.parse_args()
    try:
        validate_local_engine(engine)
        items, metadata = fetch_pages(args.max_pages, args.rows)
        with SessionLocal() as db:
            preview = synchronize(db, items, True)
            db.rollback()
        backup = backup_database(engine, ROOT / ".demo-backups") if args.apply else None
        if args.apply:
            with SessionLocal.begin() as db:
                preview = synchronize(db, items, False)
        print(json.dumps({**preview, **metadata, "backup": backup}, ensure_ascii=False, indent=2))
        return 0
    except HTTPException as exc:
        print(f"중단 HTTP {exc.status_code}: {exc.detail}")
    except SQLAlchemyError:
        print("중단: DB 연결·마이그레이션·고유번호 충돌을 확인하세요.")
    except PreparationError as exc:
        print(f"중단: {exc}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
