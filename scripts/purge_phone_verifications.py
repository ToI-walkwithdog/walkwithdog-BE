"""Remove phone verification requests that can no longer be used (Issue #40).

Dry run by default (counts only). --apply deletes. Running it at least once a day keeps
expired or consumed requests under 24 hours old; how it is scheduled (cron, worker,
on request) is still to be decided. Prints counts only, never numbers or codes.
"""
import argparse
from datetime import datetime, timezone
import json


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="delete; without it only counts are printed")
    args = parser.parse_args(argv)
    from app.database import SessionLocal
    from app.phone_verification_service import purge_phone_verifications
    with SessionLocal() as db:
        counts = purge_phone_verifications(db, datetime.now(timezone.utc), apply=args.apply)
    print(json.dumps({"applied": args.apply, "rows": counts}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
