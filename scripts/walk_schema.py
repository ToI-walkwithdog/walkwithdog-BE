"""Read-only, fail-closed PostgreSQL catalog validation for migration 007.

The reviewed catalog contract preserves expression grouping and index predicates;
it never guesses equivalence by removing parentheses or rewriting expressions.
"""
import json

from sqlalchemy import inspect, text

from scripts.prepare_demo_db import ROOT, PreparationError, validate_base_tables

CONTRACT = ROOT / "migrations/007_walk_domain.schema.json"


def catalog_table(connection, name):
    """Return a JSON-serializable public-schema contract (no data or writes)."""
    params = {"name": name}
    relation = connection.execute(text("""
        SELECT c.relkind, c.relpersistence, c.relrowsecurity, c.relforcerowsecurity
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relname = :name
    """), params).mappings().one_or_none()
    if relation is None:
        return None
    columns = connection.execute(text("""
        SELECT a.attname AS name, format_type(a.atttypid, a.atttypmod) AS type,
               a.attnotnull AS not_null, a.attidentity AS identity,
               a.attgenerated AS generated, pg_get_expr(d.adbin, d.adrelid) AS default,
               pg_get_serial_sequence(format('public.%I', c.relname), a.attname) AS sequence
        FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        LEFT JOIN pg_attrdef d ON d.adrelid = c.oid AND d.adnum = a.attnum
        WHERE n.nspname = 'public' AND c.relname = :name
          AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum
    """), params).mappings()
    constraints = connection.execute(text("""
        SELECT con.conname AS name, pg_get_constraintdef(con.oid) AS definition,
               con.convalidated AS validated
        FROM pg_constraint con JOIN pg_class c ON c.oid = con.conrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relname = :name
          AND con.contype <> 'n' ORDER BY con.conname
    """), params).mappings()
    indexes = connection.execute(text("""
        SELECT x.relname AS name, pg_get_indexdef(i.indexrelid) AS definition,
               i.indisvalid AS valid, i.indisready AS ready, i.indislive AS live
        FROM pg_index i JOIN pg_class c ON c.oid = i.indrelid
        JOIN pg_class x ON x.oid = i.indexrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relname = :name ORDER BY x.relname
    """), params).mappings()
    return {"relation": dict(relation), "columns": [dict(row) for row in columns],
            "constraints": {row["name"]: {k: v for k, v in row.items() if k != "name"} for row in constraints},
            "indexes": {row["name"]: {k: v for k, v in row.items() if k != "name"} for row in indexes}}


def compare_table(name, actual, expected, standalone_indexes):
    if actual is None:
        return [name]
    for key in ("relation", "columns", "constraints"):
        if actual[key] != expected[key]:
            raise PreparationError(f"{name} 기존 {key} 구조가 다릅니다. 자동 수정하지 않습니다.")
    pending = []
    for index, definition in expected["indexes"].items():
        if index not in actual["indexes"] and index in standalone_indexes:
            pending.append(index)
        elif actual["indexes"].get(index) != definition:
            raise PreparationError(f"{name}.{index} 인덱스 구조/유효성이 다릅니다.")
    if set(actual["indexes"]) - set(expected["indexes"]):
        raise PreparationError(f"{name}에 예상하지 않은 인덱스가 있습니다.")
    return pending


def check_walk_schema(connection):
    from app.walk_models import WALK_MODELS

    # The checked-in SQL and contract target public, never another search-path schema.
    schemas = connection.execute(text("SELECT current_schemas(false)")).scalar_one()
    if list(schemas) != ["public"]:
        raise PreparationError("007은 search_path가 public인 DB만 지원합니다.")
    validate_base_tables(connection)
    inspector = inspect(connection)
    for parent in ("users", "dogs", "shelters"):
        columns = {c["name"]: c for c in inspector.get_columns(parent)}
        if (str(columns["id"]["type"]) != "INTEGER" or columns["id"]["nullable"]
                or inspector.get_pk_constraint(parent)["constrained_columns"] != ["id"]):
            raise PreparationError(f"{parent}.id 기존 참조키 구조가 다릅니다.")

    contract = json.loads(CONTRACT.read_text())
    if set(contract) != {m.__tablename__ for m in WALK_MODELS}:
        raise PreparationError("007 스키마 계약과 ORM 테이블 목록이 다릅니다.")
    pending = []
    for model in WALK_MODELS:
        name = model.__tablename__
        expected = contract[name]
        standalone = {i.name for i in model.__table__.indexes}
        actual = catalog_table(connection, name)
        pending.extend(compare_table(name, actual, expected, standalone))
        # IF NOT EXISTS must not hide an index/sequence owned by another object.
        reserved = {name, *expected["indexes"]}
        reserved.update(c["sequence"].split(".")[-1] for c in expected["columns"] if c["sequence"])
        owned = connection.execute(text("""
            SELECT c.relname, c.relkind, t.relname AS indexed_table
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            LEFT JOIN pg_index i ON i.indexrelid = c.oid
            LEFT JOIN pg_class t ON t.oid = i.indrelid
            WHERE n.nspname = 'public' AND c.relname = ANY(:names)
        """), {"names": sorted(reserved)}).mappings()
        for row in owned:
            obj = row["relname"]
            if obj == name:
                continue  # relation flags checked by compare_table
            if (actual is None or (obj in expected["indexes"] and row["indexed_table"] != name)
                    or (obj not in expected["indexes"] and row["relkind"] != "S")):
                raise PreparationError(f"{obj} 기존 객체 이름/소유 관계가 충돌합니다.")
    return pending
