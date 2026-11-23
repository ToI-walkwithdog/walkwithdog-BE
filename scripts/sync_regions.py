"""Load or update the region master from the official 법정동코드 file (Issue #43).

Preview by default: the plan is computed against the DB and rolled back. --apply writes
it in one transaction (all or nothing). The report (source hash, counts, every change)
is printed as JSON and, with --report, also written to a file.

  python -m scripts.sync_regions --input data/regions/legal_dong_code_full_20261123.zip
  python -m scripts.sync_regions --input <zip> --apply --report region_sync_report.json
  python -m scripts.sync_regions --download <dir>      # fetch the current official zip only
  python -m scripts.sync_regions --input <zip> --offline   # convert without a DB

DATABASE_URL names the target DB. Migration 022_regions.sql must be applied first.
A run stops without writing when the file is incomplete (a master code is missing from
it), when more than 10% of active rows would be deactivated (--allow-large-change after
review), or when a code changes level or parent.
"""
import argparse
import json
from pathlib import Path
import sys

from sqlalchemy.exc import SQLAlchemyError

from app.region_master import (SnapshotError, SyncRefused, build_master, download_snapshot, read_snapshot,
                               sync_regions)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path, help="공식 법정동코드 전체자료 zip 또는 txt")
    source.add_argument("--download", type=Path, metavar="DIR", help="공식 zip을 DIR에 내려받고 종료")
    parser.add_argument("--apply", action="store_true", help="검증 후 한 트랜잭션으로 적용")
    parser.add_argument("--offline", action="store_true", help="DB 없이 변환 결과만 확인")
    parser.add_argument("--allow-large-change", action="store_true", help="대량 비활성화를 검토 후 허용")
    parser.add_argument("--report", type=Path, help="JSON 보고서 저장 경로")
    args = parser.parse_args(argv)
    try:
        if args.download:
            path = download_snapshot(args.download)
            print(json.dumps({"downloaded": str(path), **read_snapshot(path).metadata}, ensure_ascii=False, indent=2))
            return 0
        snapshot = read_snapshot(args.input)
        if args.offline:
            if args.apply:
                parser.error("--offline과 --apply는 함께 쓸 수 없습니다.")
            report = {"source": snapshot.metadata, "master": build_master(snapshot.rows).summary(), "applied": False}
        else:
            from app.database import SessionLocal  # needs DATABASE_URL only for DB runs

            with SessionLocal() as db:
                try:
                    report = sync_regions(db, snapshot, apply=args.apply,
                                          allow_large_change=args.allow_large_change)
                    if args.apply:
                        db.commit()
                    else:
                        db.rollback()
                except Exception:
                    db.rollback()
                    raise
        text = json.dumps(report, ensure_ascii=False, indent=2)
        if args.report:
            args.report.write_text(text, encoding="utf-8")
        print(text)
        return 0
    except (SnapshotError, SyncRefused) as exc:
        print(f"중단: {exc}", file=sys.stderr)
    except SQLAlchemyError as exc:
        print(f"중단: DB 오류({type(exc).__name__}). 022_regions.sql 적용 여부와 연결을 확인하세요.", file=sys.stderr)
    except OSError as exc:
        print(f"중단: 파일 또는 네트워크 오류({type(exc).__name__}).", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
