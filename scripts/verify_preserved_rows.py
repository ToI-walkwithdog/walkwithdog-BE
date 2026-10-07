"""Compare old rows to a pre-change pg_dump; never print row contents or credentials."""
import argparse
import io
import re
import subprocess

from app.database import engine
from scripts.prepare_demo_db import validate_local_engine


def verify(archive):
    validate_local_engine(engine)
    allowed = {"users", "shelters", "dogs", "surveys", "matching_results"}
    dumped = subprocess.run(["pg_restore", "--data-only", "--file=-", str(archive)], capture_output=True, timeout=30, check=True).stdout.decode()
    copies = re.findall(r"COPY public\.([a-z_]+) \(([^)]+)\) FROM stdin;\n(.*?)^\\\.\n", dumped, re.S | re.M)
    checked = set()
    raw = engine.raw_connection()
    try:
        cursor = raw.cursor()
        for table, columns, rows in copies:
            if table not in allowed:
                continue
            names = [c.strip() for c in columns.split(",")]
            assert all(re.fullmatch(r"[a-z_]+", c) for c in names) and names[0] == "id"
            old = {line.split("\t", 1)[0]: line for line in rows.splitlines() if line}
            buffer = io.StringIO()
            quoted = ",".join('"' + c + '"' for c in names)
            cursor.copy_expert(f'COPY (SELECT {quoted} FROM "{table}") TO STDOUT', buffer)
            current = {line.split("\t", 1)[0]: line for line in buffer.getvalue().splitlines()}
            assert all(current.get(key) == value for key, value in old.items()), f"{table}: old rows changed"
            checked.add(table)
            print(f"{table}: {len(old)} original rows preserved")
        assert checked == allowed, "backup missing expected tables"
    finally:
        raw.rollback()
        raw.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive")
    verify(parser.parse_args().archive)
