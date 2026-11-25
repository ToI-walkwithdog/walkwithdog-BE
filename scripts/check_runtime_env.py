"""Read-only checker for env templates and real deployment settings (Issue #128).

Two modes, both reading only the named files (never os.environ, a .env loader or the shell):

  template FILE [--local-compose]
      An example file in the repository: every known API key is listed (commented or not),
      the required ones uncommented, secret values empty, no SOLAPI keys and no value for
      ACCOUNT_EXIT_RECORD_POLICY. --local-compose also allows and requires the local Compose
      keys (deploy/.env.example).
  deploy --api FILE [--compose FILE]
      The real EC2 files: the API runtime file (.env.api) must hold every required value with
      a usable Mock SMS combination, distinct HMAC keys of at least 32 characters, a complete
      or empty level policy and values the code uses as given; the optional DB preparation
      file (.env) holds only the PostgreSQL bootstrap keys. Neither file may hold the other's
      keys.

Both modes refuse duplicate keys, malformed lines, unknown keys and values with surrounding
spaces or quotes. Output names keys and rules only: values, secrets and line contents are
never printed. Exit status: 0 when every check passes, 1 when any fails, 2 for usage errors
or unreadable files.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

KEY_LINE = re.compile(r"([A-Z][A-Z0-9_]*)=(.*)")
COMMENTED_KEY = re.compile(r"#\s*([A-Z][A-Z0-9_]*)=(.*)")
INLINE_COMMENT = re.compile(r"\s#")

# Mirrors of the application's rules; tests compare them with the app constants.
MOCK_ENVIRONMENTS = {"local", "development", "test"}
APP_ENVIRONMENTS = MOCK_ENVIRONMENTS | {"production"}
HMAC_KEY_MIN_LENGTH = 32
FIXED_CODE = re.compile(r"[0-9]{6}")
PLEDGE_VERSION_MAX_LENGTH = 32
DOCUMENT_VERSION_MAX_LENGTH = 32
LEVEL_VERSION_MAX_LENGTH = 64
CONSENT_TYPES = {"PRIVACY_COLLECTION_USE", "AI_OVERSEAS_TRANSFER"}
GEMINI_MODEL = re.compile(r"[a-zA-Z0-9._-]{1,100}")
COMPOSE_UPLOAD_DIR = "/srv/uploads/dogs"

REQUIRED = ("DATABASE_URL", "APP_ENV", "SMS_PROVIDER", "SMS_MOCK_FIXED_CODE", "PHONE_VERIFICATION_HMAC_KEY",
            "SHELTER_CODE_HMAC_KEY", "PLEDGE_CURRENT_VERSION", "PRIVACY_DOCUMENT_VERSION", "TERMS_DOCUMENT_VERSION",
            "LOCATION_GEOCODING_PROVIDER")
PRESENT = ("PRIVACY_REQUIRED_CONSENTS",)  # the key must exist; an empty value means "none"
OPTIONAL = ("PRIVACY_OPTIONAL_CONSENTS", "POLICY_DOCUMENTS_DIR", "VOLUNTEER_LEVEL_POLICY_VERSION",
            "VOLUNTEER_LEVEL_POLICY_START_AT", "LOCATION_EXTERNAL_TRANSFER_ENABLED", "KAKAO_REST_API_KEY",
            "PUBLIC_API_KEY", "PUBLIC_IMPORT_KEY", "MATCHING_INCLUDE_DEMO", "GEMINI_API_KEY", "GEMINI_MODEL",
            "ACCOUNT_EXIT_RECORD_POLICY", "AUTO_CREATE_SCHEMA", "UPLOAD_DIR")
# Tuning values and the range the code uses as given (anything else is replaced or fails).
POSITIVE_OR_DEFAULT = ("SIGNUP_RATE_LIMIT_REQUESTS", "SIGNUP_RATE_LIMIT_WINDOW_SECONDS",
                       "AVAILABILITY_RATE_LIMIT_REQUESTS", "AVAILABILITY_RATE_LIMIT_WINDOW_SECONDS",
                       "LOGIN_RATE_LIMIT_REQUESTS", "LOGIN_RATE_LIMIT_WINDOW_SECONDS",
                       "ACCOUNT_EXIT_PASSWORD_FAILURE_LIMIT", "ACCOUNT_EXIT_PASSWORD_FAILURE_WINDOW_SECONDS")
POSITIVE = ("WALK_MARKER_MAX_PER_WALK",)
INTEGER = ("AUTO_END_AFTER_SECONDS", "AUTO_END_CHECK_INTERVAL_SECONDS", "INCIDENT_DUPLICATE_WINDOW_SECONDS",
           "INCIDENT_LATE_REPORT_HOURS", "LIVE_WALK_STALE_SECONDS", "WALK_SUMMARY_MAX_POINTS")
NON_NEGATIVE = ("WALK_OVERDUE_GRACE_MINUTES",)
NON_NEGATIVE_DECIMAL = ("WALK_MARKER_MAX_ACCURACY_M",)
TUNING = POSITIVE_OR_DEFAULT + POSITIVE + INTEGER + NON_NEGATIVE + NON_NEGATIVE_DECIMAL
API_KEYS = frozenset(REQUIRED + PRESENT + OPTIONAL + TUNING)
SECRETS = frozenset({"DATABASE_URL", "SMS_MOCK_FIXED_CODE", "PHONE_VERIFICATION_HMAC_KEY", "SHELTER_CODE_HMAC_KEY",
                     "KAKAO_REST_API_KEY", "PUBLIC_API_KEY", "PUBLIC_IMPORT_KEY", "GEMINI_API_KEY",
                     "POSTGRES_PASSWORD"})
SOLAPI_KEYS = frozenset({"SOLAPI_API_KEY", "SOLAPI_API_SECRET", "SOLAPI_SENDER_NUMBER"})
DB_PREPARATION_KEYS = frozenset({"POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"})
LOCAL_COMPOSE_KEYS = DB_PREPARATION_KEYS | {"API_PORT", "API_IMAGE", "API_ENV_FILE"}
DB_NAME = re.compile(r"[A-Za-z0-9_]+")


class Report:
    def __init__(self, label):
        self.label = label
        self.failures = []

    def fail(self, subject, rule):
        self.failures.append((subject, rule))

    def lines(self):
        if not self.failures:
            return [f"{self.label}: OK"]
        return [f"{self.label}: FAIL {subject}: {rule}" for subject, rule in self.failures]


class EnvFile:
    """KEY=VALUE lines of one file. Comments and blank lines are skipped; `# KEY=` comments are
    remembered as documented keys (templates)."""

    def __init__(self, path: Path, report: Report):
        self.values, self.documented, self.commented_values = {}, set(), {}
        text = path.read_text(encoding="utf-8")
        if text.startswith("\ufeff"):
            report.fail("file", "starts with a UTF-8 BOM; save it as UTF-8 without BOM")
            text = text[1:]
        for number, raw in enumerate(text.splitlines(), start=1):
            line = raw.rstrip("\r")
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("#"):
                commented = COMMENTED_KEY.match(stripped)
                if commented:
                    self.documented.add(commented.group(1))
                    self.commented_values.setdefault(commented.group(1), commented.group(2).strip())
                continue
            match = KEY_LINE.fullmatch(line)
            if match is None:
                report.fail(f"line {number}", "not a KEY=VALUE line")
                continue
            key, value = match.groups()
            if key in self.values:
                report.fail(key, "duplicate key")
                continue
            if value != value.strip():
                report.fail(key, "value has surrounding spaces")
            elif value[:1] in ("'", '"') or INLINE_COMMENT.search(value):
                report.fail(key, "quotes or an inline comment are read differently by Compose and dotenv")
            self.values[key] = value

    def listed(self):
        return set(self.values) | self.documented


def check_known_keys(env, report, allowed):
    for key in sorted(env.listed()):
        if key in SOLAPI_KEYS:
            report.fail(key, "SOLAPI is not integrated; remove the key")
        elif key not in allowed:
            report.fail(key, "unknown key")


def check_commented_values(env, report):
    """A commented line must not keep a secret or an account-exit policy value."""
    for key in sorted((SECRETS | {"ACCOUNT_EXIT_RECORD_POLICY"}) & set(env.commented_values)):
        if env.commented_values[key]:
            report.fail(key, "a commented line must not keep a value for this key")


def check_formats(values, report):
    """Per-key rules for non-empty values, shared by both modes."""
    def value(key):
        return values.get(key, "")

    if value("APP_ENV") and value("APP_ENV").lower() not in APP_ENVIRONMENTS:
        report.fail("APP_ENV", "must be local, development, test or production")
    if value("SMS_PROVIDER") and value("SMS_PROVIDER").lower() != "mock":
        report.fail("SMS_PROVIDER", "must be mock (SOLAPI is not integrated)")
    if value("SMS_MOCK_FIXED_CODE") and not FIXED_CODE.fullmatch(value("SMS_MOCK_FIXED_CODE")):
        report.fail("SMS_MOCK_FIXED_CODE", "must be exactly 6 ASCII digits")
    for key in ("PHONE_VERIFICATION_HMAC_KEY", "SHELTER_CODE_HMAC_KEY"):
        if value(key) and len(value(key)) < HMAC_KEY_MIN_LENGTH:
            report.fail(key, f"must be at least {HMAC_KEY_MIN_LENGTH} characters")
    if value("PLEDGE_CURRENT_VERSION") and len(value("PLEDGE_CURRENT_VERSION")) > PLEDGE_VERSION_MAX_LENGTH:
        report.fail("PLEDGE_CURRENT_VERSION", f"must be at most {PLEDGE_VERSION_MAX_LENGTH} characters")
    for key in ("PRIVACY_DOCUMENT_VERSION", "TERMS_DOCUMENT_VERSION"):
        if value(key) and len(value(key)) > DOCUMENT_VERSION_MAX_LENGTH:
            report.fail(key, f"must be at most {DOCUMENT_VERSION_MAX_LENGTH} characters")
    consents = {}
    for key in ("PRIVACY_REQUIRED_CONSENTS", "PRIVACY_OPTIONAL_CONSENTS"):
        items = {item.strip() for item in value(key).split(",") if item.strip()}
        if items - CONSENT_TYPES:
            report.fail(key, "unknown consent type (allowed: " + ", ".join(sorted(CONSENT_TYPES)) + ")")
        consents[key] = items
    if consents["PRIVACY_REQUIRED_CONSENTS"] & consents["PRIVACY_OPTIONAL_CONSENTS"]:
        report.fail("PRIVACY_OPTIONAL_CONSENTS", "a consent type cannot be both required and optional")
    if value("VOLUNTEER_LEVEL_POLICY_VERSION") and len(value("VOLUNTEER_LEVEL_POLICY_VERSION")) > LEVEL_VERSION_MAX_LENGTH:
        report.fail("VOLUNTEER_LEVEL_POLICY_VERSION", f"must be at most {LEVEL_VERSION_MAX_LENGTH} characters")
    if value("VOLUNTEER_LEVEL_POLICY_START_AT"):
        try:
            starts_at = datetime.fromisoformat(value("VOLUNTEER_LEVEL_POLICY_START_AT"))
        except ValueError:
            starts_at = None
        if starts_at is None or starts_at.utcoffset() != timedelta(0):
            report.fail("VOLUNTEER_LEVEL_POLICY_START_AT", "must be an ISO 8601 UTC timestamp")
    if value("LOCATION_GEOCODING_PROVIDER") and value("LOCATION_GEOCODING_PROVIDER").lower() not in ("mock", "kakao"):
        report.fail("LOCATION_GEOCODING_PROVIDER", "must be mock or kakao")
    for key in ("LOCATION_EXTERNAL_TRANSFER_ENABLED", "MATCHING_INCLUDE_DEMO"):
        if value(key) and value(key).lower() not in ("true", "false"):
            report.fail(key, "must be true or false")
    if value("GEMINI_MODEL") and not GEMINI_MODEL.fullmatch(value("GEMINI_MODEL")):
        report.fail("GEMINI_MODEL", "must be 1 to 100 letters, digits, dots, underscores or hyphens")
    if value("ACCOUNT_EXIT_RECORD_POLICY"):
        report.fail("ACCOUNT_EXIT_RECORD_POLICY", "must stay unset (retention policy undecided)")
    if value("AUTO_CREATE_SCHEMA") and value("AUTO_CREATE_SCHEMA").lower() != "false":
        report.fail("AUTO_CREATE_SCHEMA", "must be false")
    for key in TUNING:
        if key not in values or (key in POSITIVE_OR_DEFAULT and values[key] == ""):
            continue  # unset: the code default; an empty limit is the default too
        if not tuning_value_ok(key, values[key]):
            report.fail(key, tuning_rule(key))


def tuning_value_ok(key, raw):
    if key in NON_NEGATIVE_DECIMAL:
        try:
            number = Decimal(raw)
        except InvalidOperation:
            return False
        return number.is_finite() and number >= 0
    if not re.fullmatch(r"[+-]?[0-9]{1,18}", raw):  # also keeps int() far from its digit limit
        return False
    number = int(raw)
    if key in POSITIVE_OR_DEFAULT or key in POSITIVE:
        return number >= 1
    if key in NON_NEGATIVE:
        return number >= 0
    return True


def tuning_rule(key):
    if key in NON_NEGATIVE_DECIMAL:
        return "must be a finite decimal of 0 or more"
    if key in POSITIVE_OR_DEFAULT or key in POSITIVE:
        return "must be a positive integer"
    if key in NON_NEGATIVE:
        return "must be an integer of 0 or more"
    return "must be an integer"


def check_template(path: Path, local_compose: bool) -> Report:
    report = Report(f"template {path.as_posix()}")
    env = EnvFile(path, report)
    allowed = API_KEYS | (LOCAL_COMPOSE_KEYS if local_compose else set())
    check_known_keys(env, report, allowed)
    for key in sorted(API_KEYS - env.listed()):
        report.fail(key, "missing from the template (list it, commented if optional)")
    for key in REQUIRED + PRESENT + (tuple(sorted(DB_PREPARATION_KEYS)) if local_compose else ()):
        if key not in env.values:
            report.fail(key, "required key must be an uncommented KEY= line")
    for key in sorted(SECRETS & set(env.values)):
        if env.values[key]:
            report.fail(key, "secret value must be empty in a template")
    check_commented_values(env, report)
    check_formats(env.values, report)
    return report


def check_deploy(api_path: Path, compose_path: Path | None) -> list[Report]:
    report = Report(f"deploy {api_path.as_posix()}")
    env = EnvFile(api_path, report)
    for key in sorted(env.documented & DB_PREPARATION_KEYS):
        report.fail(key, "remove the commented key from the runtime file")
    check_known_keys(env, report, API_KEYS | LOCAL_COMPOSE_KEYS)
    for key in sorted(set(env.values) & LOCAL_COMPOSE_KEYS):
        report.fail(key, "belongs to the DB preparation file, not the API runtime file" if key in DB_PREPARATION_KEYS
                    else "is set by the deploy script; remove it from the runtime file")
    check_commented_values(env, report)
    values = env.values

    for key in REQUIRED:
        if not values.get(key):
            report.fail(key, "required value is missing")
    for key in PRESENT:
        if key not in values:
            report.fail(key, "must be present (an empty value means no required consents)")
    check_formats(values, report)

    if values.get("SMS_PROVIDER", "").lower() == "mock" and values.get("APP_ENV", "").lower() not in MOCK_ENVIRONMENTS:
        report.fail("APP_ENV", "the mock SMS provider needs local, development or test")
    phone, shelter = values.get("PHONE_VERIFICATION_HMAC_KEY", ""), values.get("SHELTER_CODE_HMAC_KEY", "")
    if phone and phone == shelter:
        report.fail("SHELTER_CODE_HMAC_KEY", "must differ from PHONE_VERIFICATION_HMAC_KEY")
    if bool(values.get("VOLUNTEER_LEVEL_POLICY_VERSION")) != bool(values.get("VOLUNTEER_LEVEL_POLICY_START_AT")):
        report.fail("VOLUNTEER_LEVEL_POLICY_START_AT", "set both level policy values or neither")
    provider = values.get("LOCATION_GEOCODING_PROVIDER", "").lower()
    if provider == "mock" and values.get("APP_ENV", "").lower() not in MOCK_ENVIRONMENTS:
        report.fail("LOCATION_GEOCODING_PROVIDER", "mock needs APP_ENV local, development or test")
    if provider == "kakao" and (values.get("LOCATION_EXTERNAL_TRANSFER_ENABLED", "").lower() != "true"
                                or not values.get("KAKAO_REST_API_KEY")):
        report.fail("LOCATION_GEOCODING_PROVIDER", "kakao needs LOCATION_EXTERNAL_TRANSFER_ENABLED=true and a key")
    if bool(values.get("GEMINI_API_KEY")) != bool(values.get("GEMINI_MODEL")):
        report.fail("GEMINI_MODEL", "set GEMINI_API_KEY and GEMINI_MODEL together or neither")
    if values.get("UPLOAD_DIR") and values["UPLOAD_DIR"] != COMPOSE_UPLOAD_DIR:
        report.fail("UPLOAD_DIR", f"Compose pins {COMPOSE_UPLOAD_DIR}")
    dsn = None
    if values.get("DATABASE_URL"):
        try:
            dsn = urlsplit(values["DATABASE_URL"])
            port = dsn.port
        except ValueError:
            dsn, port = None, None
        if (dsn is None or not dsn.scheme.startswith("postgresql") or dsn.hostname != "db" or port != 5432
                or not dsn.username or not DB_NAME.fullmatch(dsn.path.lstrip("/"))):
            report.fail("DATABASE_URL", "must be a postgresql URL with a user, host db, port 5432 and a DB name")
            dsn = None
    reports = [report]

    if compose_path is not None:
        db = Report(f"deploy {compose_path.as_posix()}")
        prep = EnvFile(compose_path, db)
        for key in sorted(prep.listed()):
            if key in API_KEYS or key in SOLAPI_KEYS:
                db.fail(key, "belongs to the API runtime file, not the DB preparation file")
            elif key not in DB_PREPARATION_KEYS:
                db.fail(key, "unknown key (only POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_DB)")
        for key in sorted(DB_PREPARATION_KEYS):
            if not prep.values.get(key):
                db.fail(key, "required value is missing")
        if prep.values.get("POSTGRES_DB") and not DB_NAME.fullmatch(prep.values["POSTGRES_DB"]):
            db.fail("POSTGRES_DB", "letters, digits and underscores only")
        if dsn is not None and prep.values.get("POSTGRES_DB") and dsn.path.lstrip("/") != prep.values["POSTGRES_DB"]:
            report.fail("DATABASE_URL", "DB name must match POSTGRES_DB of the DB preparation file")
        if dsn is not None and prep.values.get("POSTGRES_USER") and dsn.username == prep.values["POSTGRES_USER"]:
            report.fail("DATABASE_URL", "use the API-only account, not POSTGRES_USER")
        if (prep.values.get("POSTGRES_PASSWORD") and values.get("DATABASE_URL")
                and prep.values["POSTGRES_PASSWORD"] in values["DATABASE_URL"]):
            report.fail("DATABASE_URL", "must not reuse POSTGRES_PASSWORD")
        reports.append(db)
    return reports


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Check env templates or real deployment settings without printing values.")
    modes = parser.add_subparsers(dest="mode", required=True)
    template = modes.add_parser("template", help="check a repository example file")
    template.add_argument("file", type=Path)
    template.add_argument("--local-compose", action="store_true",
                          help="the combined local Compose example (deploy/.env.example)")
    deploy = modes.add_parser("deploy", help="check the real EC2 files")
    deploy.add_argument("--api", type=Path, required=True, help="API runtime file (.env.api)")
    deploy.add_argument("--compose", type=Path, help="DB preparation file (.env)")
    args = parser.parse_args(argv)
    try:
        if args.mode == "template":
            reports = [check_template(args.file, args.local_compose)]
        else:
            reports = check_deploy(args.api, args.compose)
    except (OSError, UnicodeDecodeError) as exc:
        print(f"cannot read the file ({type(exc).__name__})", file=sys.stderr)
        return 2
    for report in reports:
        for line in report.lines():
            print(line)
    return 1 if any(report.failures for report in reports) else 0


if __name__ == "__main__":
    sys.exit(main())
