"""Inspect or safely migrate the configured PostgreSQL database."""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from sqlalchemy import Integer, JSON, inspect, text

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations/001_matching_snapshot.sql"


class PreparationError(RuntimeError):
    pass


def schema_state(connection):
    inspector = inspect(connection)
    if "matching_results" not in inspector.get_table_names():
        raise PreparationError("matching_results 테이블이 없습니다. 개발 DB 대상을 확인하세요.")
    columns = {c["name"]: c for c in inspector.get_columns("matching_results")}
    missing = []
    for name, kind in (("survey_id", Integer), ("analysis_snapshot", JSON)):
        if name not in columns:
            missing.append(name)
        elif not isinstance(columns[name]["type"], kind) or not columns[name]["nullable"]:
            raise PreparationError(f"{name}의 타입/NULL 허용 구조가 다릅니다. 자동 수정하지 않습니다.")

    if "survey_id" in columns:
        foreign_keys = [f for f in inspector.get_foreign_keys("matching_results")
                        if "survey_id" in f["constrained_columns"]]
        valid = (len(foreign_keys) == 1
                 and foreign_keys[0]["constrained_columns"] == ["survey_id"]
                 and foreign_keys[0]["referred_table"] == "surveys"
                 and foreign_keys[0]["referred_columns"] == ["id"]
                 and foreign_keys[0].get("referred_schema") in (None, inspector.default_schema_name)
                 and foreign_keys[0].get("options", {}).get("ondelete", "NO ACTION") == "NO ACTION")
        if not valid:
            raise PreparationError("survey_id 외래키 구조가 다릅니다. 자동 수정하지 않습니다.")

    indexes = inspector.get_indexes("matching_results")
    constraints = inspector.get_unique_constraints("matching_results")
    named = [i for i in indexes + constraints if i["name"] == "uq_matching_survey_dog"]
    if named:
        for index in named:
            options = index.get("dialect_options", {})
            if (index.get("column_names") != ["survey_id", "dog_id"]
                    or not index.get("unique", True)
                    or options.get("postgresql_where")
                    or options.get("postgresql_include")
                    or options.get("postgresql_nulls_not_distinct")):
                raise PreparationError("uq_matching_survey_dog 인덱스 구조가 다릅니다.")
        if connection.dialect.name == "postgresql":
            flags = connection.execute(text(
                "SELECT i.indisvalid, i.indisready FROM pg_index i "
                "JOIN pg_class t ON t.oid = i.indrelid "
                "JOIN pg_namespace n ON n.oid = t.relnamespace "
                "JOIN pg_class x ON x.oid = i.indexrelid "
                "WHERE n.nspname = current_schema() AND t.relname = 'matching_results' "
                "AND x.relname = 'uq_matching_survey_dog'"
            )).one_or_none()
            if flags is None or not all(flags):
                raise PreparationError("중복 방지 인덱스가 유효하지 않습니다. 자동 수정하지 않습니다.")
    else:
        missing.append("uq_matching_survey_dog")
    return {"ready": not missing, "missing": missing}


def validate_base_tables(connection):
    from app.models import User, Shelter, Dog, Survey, MatchingResult
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())
    for model in (User, Shelter, Dog, Survey, MatchingResult):
        name = model.__tablename__
        if name not in tables:
            raise PreparationError(f"필수 테이블 {name}이 없습니다.")
        expected = set(model.__table__.columns.keys())
        if model is MatchingResult:
            expected -= {"survey_id", "analysis_snapshot"}
        if model is Dog:
            expected -= {"external_data", "synced_at", "process_state", "neuter_status"}
        if model is Shelter:
            expected -= {"external_code"}
        if model is User:
            expected -= {"role"}  # Added separately by migration 005.
        missing = expected - {c["name"] for c in inspector.get_columns(name)}
        if missing:
            raise PreparationError(f"{name}에 필수 컬럼이 없습니다: {sorted(missing)}")


def table_counts(connection):
    quote = connection.dialect.identifier_preparer.quote
    return {name: connection.execute(text(f"SELECT count(*) FROM {quote(name)}")).scalar_one()
            for name in inspect(connection).get_table_names()}


def backup_database(engine, directory):
    if not shutil.which("pg_dump") or not shutil.which("pg_restore"):
        raise PreparationError("pg_dump와 pg_restore가 필요합니다. 마이그레이션하지 않습니다.")
    directory = Path(directory).resolve()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, filename = tempfile.mkstemp(prefix="toi-before-demo-", suffix=".dump", dir=directory)
    os.close(descriptor)
    archive = Path(filename)
    archive.chmod(0o600)
    url = engine.url
    process_env = os.environ.copy()
    # Avoid credentials in command arguments and output; honor connection SSL options.
    for key in list(process_env):
        if key.startswith("PG"):
            process_env.pop(key)
    for key, value in (("PGHOST", url.host), ("PGPORT", url.port), ("PGDATABASE", url.database),
                       ("PGUSER", url.username), ("PGPASSWORD", url.password)):
        if value is not None:
            process_env[key] = str(value)
    for option in ("sslmode", "sslcert", "sslkey", "sslrootcert", "sslcrl"):
        if option in url.query:
            process_env["PG" + option.upper()] = str(url.query[option])
    process_env["PGCONNECT_TIMEOUT"] = "10"
    try:
        dumped = subprocess.run(["pg_dump", "--format=custom", "--no-password", "--file", str(archive)],
                                env=process_env, capture_output=True, timeout=120, check=False)
        if dumped.returncode or archive.stat().st_size == 0:
            raise PreparationError("pg_dump 백업 실패. 마이그레이션하지 않습니다.")
        checked = subprocess.run(["pg_restore", "--list", str(archive)], env=process_env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30, check=False)
        if checked.returncode:
            raise PreparationError("백업 아카이브 검증 실패. 마이그레이션하지 않습니다.")
    except subprocess.TimeoutExpired as exc:
        raise PreparationError("백업 시간 초과. 마이그레이션하지 않습니다.") from exc
    return str(archive)


def apply_migration(connection):
    # Execute only the checked-in static migration inside the caller's transaction.
    source = "\n".join(line for line in MIGRATION.read_text().splitlines()
                       if not line.lstrip().startswith("--"))
    for statement in source.split(";"):
        statement = statement.strip()
        if statement and statement.upper() not in ("BEGIN", "COMMIT"):
            connection.execute(text(statement))


def validate_local_engine(engine):
    if engine.dialect.name != "postgresql":
        raise PreparationError("이 명령은 PostgreSQL 개발 DB 전용입니다.")
    if engine.url.host not in ("localhost", "127.0.0.1", "::1"):
        raise PreparationError("현재 시연 도구는 로컬 PostgreSQL만 허용합니다.")


def prepare(engine, apply=False, backup_dir=ROOT / ".demo-backups"):
    validate_local_engine(engine)
    with engine.connect() as connection:
        validate_base_tables(connection)
        state = schema_state(connection)
        before = table_counts(connection)
    result = {"target": {"host": engine.url.host, "port": engine.url.port,
                         "database": engine.url.database}, "schema": state, "counts": before,
              "applied": False, "backup": None}
    if not apply or state["ready"]:
        return result
    result["backup"] = backup_database(engine, backup_dir)
    with engine.begin() as connection:
        connection.execute(text("SET LOCAL lock_timeout = '5s'"))
        # Fail rather than wait indefinitely; prevent row changes during count validation.
        names = inspect(connection).get_table_names()
        quote = connection.dialect.identifier_preparer.quote
        connection.execute(text("LOCK TABLE " + ", ".join(quote(n) for n in names)
                                + " IN SHARE ROW EXCLUSIVE MODE"))
        validate_base_tables(connection)
        schema_state(connection)
        locked_before = table_counts(connection)
        if before != locked_before:
            raise PreparationError("백업 중 데이터가 변경됐습니다. 쓰기 작업을 멈춘 뒤 재실행하세요.")
        apply_migration(connection)
        after = schema_state(connection)
        if not after["ready"] or table_counts(connection) != locked_before:
            raise PreparationError("마이그레이션 검증 실패. 트랜잭션을 롤백합니다.")
        result["schema"] = after
        result["applied"] = True
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="백업 후 미적용 마이그레이션 적용")
    parser.add_argument("--backup-dir", default=str(ROOT / ".demo-backups"))
    args = parser.parse_args(argv)
    try:
        from app.database import engine
        print(json.dumps(prepare(engine, args.apply, args.backup_dir), ensure_ascii=False, indent=2))
        return 0
    except PreparationError as exc:
        print(f"DB 준비 실패: {exc}")
    except Exception:
        print("DB 준비 실패: 연결·권한·잠금 상태를 확인하세요. 비밀정보 보호를 위해 원문 오류는 출력하지 않습니다.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
