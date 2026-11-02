"""Additive backend migrations: dry-run first, validated backup required for --apply."""
import argparse
import json

from sqlalchemy import inspect, text

from scripts.prepare_demo_db import (ROOT, PreparationError, backup_database,
                                     table_counts, validate_local_engine)


def check_table(connection, model):
    inspector = inspect(connection)
    name = model.__tablename__
    if name not in inspector.get_table_names():
        return [name]
    columns = {c["name"]: c for c in inspector.get_columns(name)}
    if set(columns) != set(model.__table__.columns.keys()):
        raise PreparationError(f"{name} 기존 컬럼 구조가 다릅니다. 자동 수정하지 않습니다.")
    for column in model.__table__.columns:
        actual = columns[column.name]
        expected = column.type.compile(dialect=connection.dialect)
        found = actual["type"].compile(dialect=connection.dialect)
        if expected != found or column.nullable != actual["nullable"]:
            raise PreparationError(f"{name}.{column.name} 타입/NULL 구조가 다릅니다.")
    primary = inspector.get_pk_constraint(name)["constrained_columns"]
    if primary != [c.name for c in model.__table__.primary_key]:
        raise PreparationError(f"{name} 기본키 구조가 다릅니다.")
    foreign = {(tuple(f["constrained_columns"]), f["referred_table"], tuple(f["referred_columns"]))
               for f in inspector.get_foreign_keys(name)}
    for constraint in inspector.get_foreign_keys(name):
        if (constraint.get("referred_schema") not in (None, inspector.default_schema_name)
                or constraint.get("options", {}).get("ondelete", "NO ACTION") != "NO ACTION"):
            raise PreparationError(f"{name} 외래키 옵션이 다릅니다.")
    expected_foreign = {((f.parent.name,), f.column.table.name, (f.column.name,))
                        for f in model.__table__.foreign_keys}
    if foreign != expected_foreign:
        raise PreparationError(f"{name} 외래키 구조가 다릅니다.")
    actual_indexes = {i["name"]: i for i in inspector.get_indexes(name)}
    pending = []
    for index in model.__table__.indexes:
        actual = actual_indexes.get(index.name)
        if actual is None:
            pending.append(index.name)
        elif (actual["column_names"] != [c.name for c in index.columns]
              or actual["unique"] != index.unique
              or any(actual.get("dialect_options", {}).get(key) for key in
                     ("postgresql_where", "postgresql_include", "postgresql_nulls_not_distinct"))):
            raise PreparationError(f"{name}.{index.name} 인덱스 구조가 다릅니다.")
    return pending


def check_public_columns(connection):
    from app.models import Dog, Shelter
    inspector = inspect(connection)
    missing = []
    for model, names in ((Dog, ("external_data", "synced_at", "process_state", "neuter_status")),
                         (Shelter, ("external_code",))):
        name = model.__tablename__
        columns = {c["name"]: c for c in inspector.get_columns(name)}
        for field in names:
            if field not in columns:
                missing.append(f"{name}.{field}")
                continue
            expected = model.__table__.columns[field].type.compile(dialect=connection.dialect)
            actual = columns[field]
            if actual["type"].compile(dialect=connection.dialect) != expected or not actual["nullable"]:
                raise PreparationError(f"{name}.{field} 구조가 다릅니다. 자동 수정하지 않습니다.")
    if "shelters.external_code" not in missing:
        matches = [i for i in inspector.get_indexes("shelters") + inspector.get_unique_constraints("shelters")
                   if i.get("column_names") == ["external_code"] and i.get("unique", True)]
        if not matches:
            missing.append("uq_shelters_external_code")
        for match in matches:
            if any(match.get("dialect_options", {}).get(key) for key in
                   ("postgresql_where", "postgresql_include", "postgresql_nulls_not_distinct")):
                raise PreparationError("보호소 코드 유일성 인덱스 옵션이 다릅니다.")
        for index in inspector.get_indexes("shelters"):
            if index["name"] == "uq_shelters_external_code" and (not index["unique"] or index["column_names"] != ["external_code"]):
                raise PreparationError("보호소 코드 인덱스 구조가 다릅니다.")
    return missing


def check_version(connection, version):
    from app.models import AuthSession, ChatSession, ChatMessage
    if version == "002":
        return check_table(connection, AuthSession)
    if version == "003":
        return check_public_columns(connection)
    if version == "004":
        return check_table(connection, ChatSession) + check_table(connection, ChatMessage)
    if version == "005":
        return check_user_role(connection)
    if version == "006":
        return check_user_shelter(connection)
    if version == "007":
        from scripts.walk_schema import check_walk_schema
        return check_walk_schema(connection)
    raise PreparationError("지원하지 않는 버전")


def check_user_role(connection):
    from app.models import User
    inspector = inspect(connection)
    role_type = User.__table__.columns.role.type
    enums = {item["name"]: item for item in inspector.get_enums(schema=inspector.default_schema_name)}
    missing = []
    if role_type.name not in enums:
        missing.append(role_type.name)
    elif enums[role_type.name]["labels"] != role_type.enums:
        raise PreparationError("userrole 기존 Enum 값이 다릅니다. 자동 수정하지 않습니다.")
    columns = {column["name"]: column for column in inspector.get_columns("users")}
    if "role" not in columns:
        missing.append("users.role")
    else:
        column = columns["role"]
        if (column["nullable"] or role_type.name in missing
                or column["type"].compile(dialect=connection.dialect) != role_type.compile(dialect=connection.dialect)):
            raise PreparationError("users.role 타입/NULL 구조가 다릅니다. 자동 수정하지 않습니다.")
    return missing


def check_user_shelter(connection):
    from app.models import User
    inspector = inspect(connection)
    columns = {column["name"]: column for column in inspector.get_columns("users")}
    missing = []
    if "shelter_id" not in columns:
        missing.append("users.shelter_id")
    else:
        column = columns["shelter_id"]
        expected = User.__table__.columns.shelter_id.type.compile(dialect=connection.dialect)
        if (column["type"].compile(dialect=connection.dialect) != expected
                or not column["nullable"] or column.get("default") is not None):
            raise PreparationError("users.shelter_id 타입/NULL/default 구조가 다릅니다. 자동 수정하지 않습니다.")

    foreign = [f for f in inspector.get_foreign_keys("users") if "shelter_id" in f["constrained_columns"]]
    if not foreign:
        missing.append("fk_users_shelter_id")
    elif (len(foreign) != 1 or foreign[0]["constrained_columns"] != ["shelter_id"]
          or foreign[0]["referred_table"] != "shelters" or foreign[0]["referred_columns"] != ["id"]
          or foreign[0].get("referred_schema") not in (None, inspector.default_schema_name)
          or foreign[0].get("options", {}).get("ondelete", "NO ACTION") != "NO ACTION"
          or foreign[0].get("options", {}).get("onupdate", "NO ACTION") != "NO ACTION"):
        raise PreparationError("users.shelter_id 외래키 구조가 다릅니다. 자동 수정하지 않습니다.")

    indexes = {i["name"]: i for i in inspector.get_indexes("users")}
    index = indexes.get("ix_users_shelter_id")
    if index is None:
        missing.append("ix_users_shelter_id")
    elif (index["column_names"] != ["shelter_id"] or index["unique"]
          or any(index.get("dialect_options", {}).get(key) for key in
                 ("postgresql_where", "postgresql_include", "postgresql_nulls_not_distinct"))):
        raise PreparationError("ix_users_shelter_id 인덱스 구조가 다릅니다. 자동 수정하지 않습니다.")
    return missing


def migrate(engine, version, apply=False):
    validate_local_engine(engine)
    if version not in ("002", "003", "004", "005", "006", "007"):
        raise PreparationError("지원하지 않는 마이그레이션입니다.")
    with engine.connect() as connection:
        missing = check_version(connection, version)
        before = table_counts(connection)
    result = {"target": {"host": engine.url.host, "database": engine.url.database},
              "version": version, "missing": missing, "applied": False, "backup": None}
    if version == "007":
        result.update(counts_before=before, counts_after=None)
    if not apply or not missing:
        if version == "007" and not missing:
            result["counts_after"] = before
        return result
    result["backup"] = backup_database(engine, ROOT / ".demo-backups")
    filenames = {"002": "002_auth_sessions.sql", "003": "003_public_data.sql",
                 "004": "004_chat.sql", "005": "005_user_roles.sql", "006": "006_user_shelters.sql",
                 "007": "007_walk_domain.sql"}
    source = (ROOT / "migrations" / filenames[version]).read_text()
    with engine.begin() as connection:
        if version == "007":
            connection.execute(text("SET LOCAL lock_timeout = '5s'"))
        connection.execute(text("SELECT pg_advisory_xact_lock(782451)"))
        if version == "007":
            # Freeze all existing tables while checking the backed-up row counts.
            quote = connection.dialect.identifier_preparer.quote
            names = inspect(connection).get_table_names()
            connection.execute(text("LOCK TABLE " + ", ".join(quote(n) for n in sorted(names))
                                    + " IN SHARE ROW EXCLUSIVE MODE"))
            if table_counts(connection) != before:
                raise PreparationError("백업 중 데이터가 변경됐습니다. 쓰기를 멈추고 재실행하세요.")
        check_version(connection, version)
        statements = "\n".join(l for l in source.splitlines() if not l.lstrip().startswith("--"))
        if version in ("005", "006"):
            # The PostgreSQL DO block contains semicolons; do not split its body.
            connection.exec_driver_sql(statements)
        else:
            for statement in statements.split(";"):
                if statement.strip() and statement.strip().upper() not in ("BEGIN", "COMMIT"):
                    connection.execute(text(statement))
        pending = check_version(connection, version)
        if pending:
            raise PreparationError("마이그레이션 후 구조 확인 실패")
        after = table_counts(connection)
        if any(after[name] != count for name, count in before.items()):
            raise PreparationError("기존 행 수가 달라졌습니다. 롤백합니다.")
        if version == "007":
            result["counts_after"] = after
    result["applied"] = True
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", choices=["002", "003", "004", "005", "006", "007"])
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    from app.database import engine
    try:
        print(json.dumps(migrate(engine, args.version, args.apply), ensure_ascii=False, indent=2))
    except PreparationError as exc:
        print(f"중단: {exc}")
        return 1
    except Exception:
        print("중단: 연결·SQL·권한 오류입니다. 트랜잭션은 롤백됩니다. 비밀정보 보호를 위해 원문 오류는 출력하지 않습니다.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
