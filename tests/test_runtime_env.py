"""Issue #128: scripts/check_runtime_env.py, the env templates and their agreement with the code.

Synthetic files and values only. The checker reads only the files it is given, so these tests
also prove that the process environment and a local .env cannot change its result, and that no
value ever reaches its output."""
import io
import os
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

with patch.dict(os.environ, {"DATABASE_URL": "sqlite://"}):
    import app.location_geocoder as location_geocoder
    import app.phone_verification_service as phone_service
    import app.pledge_service as pledge_service
    import app.privacy_policy as privacy_policy
    import app.shelter_code_service as shelter_code_service
    import app.sms_provider as sms_provider
from scripts import check_runtime_env as checker

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_runtime_env.py"
PHONE_KEY = "synthetic-phone-hmac-key-0123456789abcdef"
SHELTER_KEY = "synthetic-shelter-hmac-key-0123456789abcdef"
FIXED = "975310"
API_PASSWORD = "synthetic-api-password-11"
DB_PASSWORD = "synthetic-db-password-22"
KAKAO = "synthetic-kakao-key-33"
SECRET_VALUES = (PHONE_KEY, SHELTER_KEY, FIXED, API_PASSWORD, DB_PASSWORD, KAKAO, "synthetic-gemini-key-44")
VALID_API = {
    "DATABASE_URL": f"postgresql://toi_api:{API_PASSWORD}@db:5432/toi_db",
    "APP_ENV": "development",
    "SMS_PROVIDER": "mock",
    "SMS_MOCK_FIXED_CODE": FIXED,
    "PHONE_VERIFICATION_HMAC_KEY": PHONE_KEY,
    "SHELTER_CODE_HMAC_KEY": SHELTER_KEY,
    "PLEDGE_CURRENT_VERSION": "pledge-v1",
    "PRIVACY_DOCUMENT_VERSION": "privacy-v1",
    "TERMS_DOCUMENT_VERSION": "terms-v1",
    "PRIVACY_REQUIRED_CONSENTS": "PRIVACY_COLLECTION_USE",
    "PRIVACY_OPTIONAL_CONSENTS": "AI_OVERSEAS_TRANSFER",
    "VOLUNTEER_LEVEL_POLICY_VERSION": "",
    "VOLUNTEER_LEVEL_POLICY_START_AT": "",
    "LOCATION_GEOCODING_PROVIDER": "mock",
    "LOCATION_EXTERNAL_TRANSFER_ENABLED": "false",
    "MATCHING_INCLUDE_DEMO": "false",
    "AUTO_CREATE_SCHEMA": "false",
    "UPLOAD_DIR": "/srv/uploads/dogs",
}
VALID_DB = {"POSTGRES_USER": "toi", "POSTGRES_PASSWORD": DB_PASSWORD, "POSTGRES_DB": "toi_db"}
DROP = object()


def render(values, extra_lines=()):
    lines = [f"{key}={value}" for key, value in values.items()]
    return "\n".join(["# synthetic test file", *lines, *extra_lines]) + "\n"


class CheckerTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)

    def write(self, name, text):
        path = self.dir / name
        path.write_text(text, encoding="utf-8", newline="")
        return path

    def run_checker(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = checker.main([str(arg) for arg in args])
            except SystemExit as exc:  # argparse usage errors
                code = exc.code
        output = out.getvalue() + err.getvalue()
        for secret in SECRET_VALUES:
            self.assertNotIn(secret, output)  # never a value, whatever the result
        return code, output

    def deploy(self, changes=None, extra_lines=(), db=VALID_DB, db_extra=()):
        values = dict(VALID_API)
        for key, value in (changes or {}).items():
            if value is DROP:
                values.pop(key, None)
            else:
                values[key] = value
        api = self.write(".env.api", render(values, extra_lines))
        args = ["deploy", "--api", api]
        if db is not None:
            args += ["--compose", self.write(".env", render(db, db_extra))]
        result = self.run_checker(*args)
        written = {**values, **(db or {})}
        for line in (*extra_lines, *db_extra):
            key, _, value = line.partition("=")
            written.setdefault(key.strip("# "), value)
        for key in checker.SECRETS:  # every secret written, whatever its validity, stays out of the output
            if written.get(key):
                self.assertNotIn(written[key], result[1], key)
        return result

    def assert_fails(self, result, key, rule_fragment):
        code, output = result
        self.assertEqual(code, 1, output)
        self.assertRegex(output, rf"FAIL {re.escape(key)}[^:\n]*: [^\n]*{re.escape(rule_fragment)}")


class DeployModeTests(CheckerTestCase):
    def test_a_valid_api_and_db_file_pass(self):
        code, output = self.deploy()
        self.assertEqual(code, 0, output)
        self.assertEqual(output.count(": OK"), 2)

    def test_required_values(self):
        for key in checker.REQUIRED:
            for value in (DROP, ""):
                with self.subTest(key=key, value="unset" if value is DROP else "empty"):
                    self.assert_fails(self.deploy({key: value}), key, "required value is missing")

    def test_privacy_consent_lists_follow_the_existing_policy(self):
        self.assert_fails(self.deploy({"PRIVACY_REQUIRED_CONSENTS": DROP}), "PRIVACY_REQUIRED_CONSENTS", "must be present")
        for changes in ({"PRIVACY_REQUIRED_CONSENTS": ""},  # explicitly no required consents
                        {"PRIVACY_OPTIONAL_CONSENTS": DROP}, {"PRIVACY_OPTIONAL_CONSENTS": ""},
                        {"PRIVACY_REQUIRED_CONSENTS": "PRIVACY_COLLECTION_USE,,AI_OVERSEAS_TRANSFER",
                         "PRIVACY_OPTIONAL_CONSENTS": ""}):
            with self.subTest(changes=changes):
                self.assertEqual(self.deploy(changes)[0], 0)
        self.assert_fails(self.deploy({"PRIVACY_REQUIRED_CONSENTS": "MARKETING"}), "PRIVACY_REQUIRED_CONSENTS",
                          "unknown consent type")
        self.assert_fails(self.deploy({"PRIVACY_OPTIONAL_CONSENTS": "PRIVACY_COLLECTION_USE"}),
                          "PRIVACY_OPTIONAL_CONSENTS", "both required and optional")

    def test_mock_sms_combination_and_fixed_code_format(self):
        self.assert_fails(self.deploy({"SMS_PROVIDER": "solapi"}), "SMS_PROVIDER", "must be mock")
        for app_env in ("production", "staging"):
            with self.subTest(app_env=app_env):
                self.assert_fails(self.deploy({"APP_ENV": app_env}), "APP_ENV", "")
        self.assert_fails(self.deploy({"APP_ENV": "production"}), "APP_ENV", "mock SMS provider needs")
        for code in ("97531", "9753100", "97531a", "９７５３１０", "٩٧٥٣١٠"):
            with self.subTest(code=code):
                self.assert_fails(self.deploy({"SMS_MOCK_FIXED_CODE": code}), "SMS_MOCK_FIXED_CODE", "6 ASCII digits")

    def test_hmac_keys_length_and_separation(self):
        for key in ("PHONE_VERIFICATION_HMAC_KEY", "SHELTER_CODE_HMAC_KEY"):
            with self.subTest(key=key):
                self.assert_fails(self.deploy({key: "x" * 31}), key, "at least 32 characters")
        self.assertEqual(self.deploy({"SHELTER_CODE_HMAC_KEY": "y" * 32})[0], 0)
        self.assert_fails(self.deploy({"SHELTER_CODE_HMAC_KEY": PHONE_KEY}), "SHELTER_CODE_HMAC_KEY",
                          "must differ from PHONE_VERIFICATION_HMAC_KEY")

    def test_level_policy_values(self):
        complete = {"VOLUNTEER_LEVEL_POLICY_VERSION": "level-v1", "VOLUNTEER_LEVEL_POLICY_START_AT": "2026-12-01T00:00:00+00:00"}
        self.assertEqual(self.deploy(complete)[0], 0)
        self.assertEqual(self.deploy({**complete, "VOLUNTEER_LEVEL_POLICY_START_AT": "2026-12-01T00:00:00Z"})[0], 0)
        self.assertEqual(self.deploy({"VOLUNTEER_LEVEL_POLICY_VERSION": DROP, "VOLUNTEER_LEVEL_POLICY_START_AT": DROP})[0], 0)
        for half in ({"VOLUNTEER_LEVEL_POLICY_VERSION": "level-v1"},
                     {"VOLUNTEER_LEVEL_POLICY_START_AT": "2026-12-01T00:00:00+00:00"}):
            with self.subTest(half=half):
                self.assert_fails(self.deploy(half), "VOLUNTEER_LEVEL_POLICY_START_AT", "both level policy values")
        self.assert_fails(self.deploy({**complete, "VOLUNTEER_LEVEL_POLICY_VERSION": "v" * 65}),
                          "VOLUNTEER_LEVEL_POLICY_VERSION", "at most 64")
        for start in ("2026-12-01T09:00:00+09:00", "2026-12-01T00:00:00", "next week"):
            with self.subTest(start=start):
                self.assert_fails(self.deploy({**complete, "VOLUNTEER_LEVEL_POLICY_START_AT": start}),
                                  "VOLUNTEER_LEVEL_POLICY_START_AT", "ISO 8601 UTC")

    def test_versions_location_gemini_and_pinned_settings(self):
        self.assert_fails(self.deploy({"PLEDGE_CURRENT_VERSION": "p" * 33}), "PLEDGE_CURRENT_VERSION", "at most 32")
        self.assert_fails(self.deploy({"TERMS_DOCUMENT_VERSION": "t" * 33}), "TERMS_DOCUMENT_VERSION", "at most 32")
        self.assert_fails(self.deploy({"LOCATION_GEOCODING_PROVIDER": "naver"}), "LOCATION_GEOCODING_PROVIDER", "mock or kakao")
        self.assert_fails(self.deploy({"LOCATION_GEOCODING_PROVIDER": "kakao", "KAKAO_REST_API_KEY": KAKAO}),
                          "LOCATION_GEOCODING_PROVIDER", "kakao needs")
        self.assertEqual(self.deploy({"LOCATION_GEOCODING_PROVIDER": "kakao", "KAKAO_REST_API_KEY": KAKAO,
                                      "LOCATION_EXTERNAL_TRANSFER_ENABLED": "true"})[0], 0)
        self.assert_fails(self.deploy({"LOCATION_EXTERNAL_TRANSFER_ENABLED": "yes"}),
                          "LOCATION_EXTERNAL_TRANSFER_ENABLED", "true or false")
        self.assert_fails(self.deploy({"MATCHING_INCLUDE_DEMO": "1"}), "MATCHING_INCLUDE_DEMO", "true or false")
        self.assert_fails(self.deploy({"GEMINI_API_KEY": "synthetic-gemini-key-44"}), "GEMINI_MODEL", "together")
        self.assert_fails(self.deploy({"GEMINI_API_KEY": "synthetic-gemini-key-44", "GEMINI_MODEL": "bad model"}),
                          "GEMINI_MODEL", "1 to 100")
        self.assert_fails(self.deploy({"ACCOUNT_EXIT_RECORD_POLICY": "delete"}), "ACCOUNT_EXIT_RECORD_POLICY",
                          "must stay unset")
        self.assertEqual(self.deploy({"ACCOUNT_EXIT_RECORD_POLICY": ""})[0], 0)
        self.assert_fails(self.deploy({"AUTO_CREATE_SCHEMA": "true"}), "AUTO_CREATE_SCHEMA", "must be false")
        self.assert_fails(self.deploy({"UPLOAD_DIR": "/tmp/dogs"}), "UPLOAD_DIR", "Compose pins")

    def test_tuning_values_use_the_range_the_code_accepts(self):
        cases = {
            "LOGIN_RATE_LIMIT_REQUESTS": (["1", "10", ""], ["0", "-1", "abc", "1.5"]),
            "ACCOUNT_EXIT_PASSWORD_FAILURE_WINDOW_SECONDS": (["900", ""], ["0"]),
            "WALK_MARKER_MAX_PER_WALK": (["1"], ["0", "", "x"]),
            "AUTO_END_AFTER_SECONDS": (["3600", "-5", "0"], ["", "1.5", "ten"]),
            "LIVE_WALK_STALE_SECONDS": (["60"], ["", "6e1"]),
            "WALK_OVERDUE_GRACE_MINUTES": (["0", "15"], ["-1", ""]),
            "WALK_MARKER_MAX_ACCURACY_M": (["0", "12.5", "50"], ["-1", "Infinity", "NaN", "", "fifty"]),
        }
        for key, (good, bad) in cases.items():
            for value in good:
                with self.subTest(key=key, value=value):
                    self.assertEqual(self.deploy({key: value})[0], 0)
            for value in bad:
                with self.subTest(key=key, value=value):
                    self.assert_fails(self.deploy({key: value}), key, "must be")

    def test_file_format_problems(self):
        self.assert_fails(self.deploy(extra_lines=["APP_ENV=development"]), "APP_ENV", "duplicate key")
        self.assert_fails(self.deploy(extra_lines=["export FOO=1"]), "line", "not a KEY=VALUE line")
        self.assert_fails(self.deploy(extra_lines=["lowercase=1"]), "line", "not a KEY=VALUE line")
        self.assert_fails(self.deploy(extra_lines=["FEATURE_X=1"]), "FEATURE_X", "unknown key")
        self.assert_fails(self.deploy({"PLEDGE_CURRENT_VERSION": " pledge-v1"}), "PLEDGE_CURRENT_VERSION",
                          "surrounding spaces")
        self.assert_fails(self.deploy({"PLEDGE_CURRENT_VERSION": '"pledge-v1"'}), "PLEDGE_CURRENT_VERSION", "quotes")
        self.assert_fails(self.deploy({"PLEDGE_CURRENT_VERSION": "pledge-v1 # note"}), "PLEDGE_CURRENT_VERSION",
                          "inline comment")
        for key in sorted(checker.SOLAPI_KEYS):
            with self.subTest(key=key):
                self.assert_fails(self.deploy(extra_lines=[f"{key}=x"]), key, "SOLAPI is not integrated")
                self.assert_fails(self.deploy(extra_lines=[f"# {key}="]), key, "SOLAPI is not integrated")

    def test_the_api_and_db_files_stay_separate(self):
        self.assert_fails(self.deploy(extra_lines=[f"POSTGRES_PASSWORD={DB_PASSWORD}"]), "POSTGRES_PASSWORD",
                          "belongs to the DB preparation file")
        self.assert_fails(self.deploy(extra_lines=["# POSTGRES_PASSWORD="]), "POSTGRES_PASSWORD",
                          "remove the commented key")
        self.assert_fails(self.deploy(db_extra=[f"PHONE_VERIFICATION_HMAC_KEY={PHONE_KEY}"]),
                          "PHONE_VERIFICATION_HMAC_KEY", "belongs to the API runtime file")
        self.assert_fails(self.deploy(db_extra=["API_PORT=8000"]), "API_PORT", "only POSTGRES_USER")
        self.assert_fails(self.deploy(db={**VALID_DB, "POSTGRES_PASSWORD": ""}), "POSTGRES_PASSWORD",
                          "required value is missing")
        self.assert_fails(self.deploy(db={**VALID_DB, "POSTGRES_DB": "toi-db"}), "POSTGRES_DB", "letters, digits")
        self.assert_fails(self.deploy(db={**VALID_DB, "POSTGRES_DB": "other_db"}), "DATABASE_URL",
                          "must match POSTGRES_DB")
        self.assert_fails(self.deploy(db={**VALID_DB, "POSTGRES_USER": "toi_api"}), "DATABASE_URL", "API-only account")
        self.assert_fails(self.deploy({"DATABASE_URL": f"postgresql://toi_api:{DB_PASSWORD}@db:5432/toi_db"}),
                          "DATABASE_URL", "must not reuse POSTGRES_PASSWORD")
        for url in (f"postgresql://toi_api:{API_PASSWORD}@localhost:5432/toi_db", "sqlite://",
                    f"postgresql://toi_api:{API_PASSWORD}@db:5433/toi_db", "postgresql://db:5432/toi_db",
                    f"postgresql://toi_api:{API_PASSWORD}@db:port/toi_db"):
            with self.subTest(url=url.replace(API_PASSWORD, "***")):
                self.assert_fails(self.deploy({"DATABASE_URL": url}), "DATABASE_URL", "postgresql URL")
        self.assertEqual(self.deploy(db=None)[0], 0)  # the DB file is optional

    def test_exit_codes_for_usage_and_unreadable_files(self):
        self.assertEqual(self.run_checker("deploy", "--api", self.dir / "missing.env")[0], 2)
        self.assertEqual(self.run_checker("template", self.dir / "missing.env")[0], 2)
        self.assertEqual(self.run_checker()[0], 2)
        self.assertEqual(self.run_checker("deploy")[0], 2)
        self.assertEqual(self.run_checker("unknown")[0], 2)


class TemplateModeTests(CheckerTestCase):
    TEMPLATES = ((ROOT / ".env.example", False), (ROOT / "deploy" / ".env.api.example", False),
                 (ROOT / "deploy" / ".env.example", True))

    def test_the_repository_templates_pass(self):
        for path, local_compose in self.TEMPLATES:
            with self.subTest(template=path.name):
                args = ["template", path] + (["--local-compose"] if local_compose else [])
                code, output = self.run_checker(*args)
                self.assertEqual(code, 0, output)

    def template(self, edit, local_compose=False):
        text = (ROOT / "deploy" / ".env.api.example").read_text(encoding="utf-8")
        args = ["template", self.write("x.example", edit(text))] + (["--local-compose"] if local_compose else [])
        return self.run_checker(*args)

    def test_template_problems_are_detected(self):
        self.assert_fails(self.template(lambda t: t.replace("PHONE_VERIFICATION_HMAC_KEY=\n",
                                                            f"PHONE_VERIFICATION_HMAC_KEY={PHONE_KEY}\n")),
                          "PHONE_VERIFICATION_HMAC_KEY", "secret value must be empty")
        self.assert_fails(self.template(lambda t: t.replace("SMS_MOCK_FIXED_CODE=\n", f"SMS_MOCK_FIXED_CODE={FIXED}\n")),
                          "SMS_MOCK_FIXED_CODE", "secret value must be empty")
        self.assert_fails(self.template(lambda t: t.replace("\nDATABASE_URL=\n", "\n# DATABASE_URL=\n")),
                          "DATABASE_URL", "uncommented KEY= line")
        self.assert_fails(self.template(lambda t: re.sub(r"# WALK_OVERDUE_GRACE_MINUTES=0\n?", "", t)),
                          "WALK_OVERDUE_GRACE_MINUTES", "missing from the template")
        self.assert_fails(self.template(lambda t: t + "# SOLAPI_API_KEY=\n"), "SOLAPI_API_KEY", "SOLAPI")
        self.assert_fails(self.template(lambda t: t.replace("# ACCOUNT_EXIT_RECORD_POLICY=\n",
                                                            "ACCOUNT_EXIT_RECORD_POLICY=delete\n")),
                          "ACCOUNT_EXIT_RECORD_POLICY", "must stay unset")
        self.assert_fails(self.template(lambda t: t + "POSTGRES_USER=toi\n"), "POSTGRES_USER", "unknown key")
        self.assert_fails(self.template(lambda t: t + "APP_ENV=local\n"), "APP_ENV", "duplicate key")
        self.assert_fails(self.template(lambda t: t, local_compose=True), "POSTGRES_PASSWORD", "uncommented KEY= line")
        self.assert_fails(self.template(lambda t: t.replace("APP_ENV=development", "APP_ENV=staging")), "APP_ENV",
                          "must be local")


class IsolationTests(CheckerTestCase):
    HOSTILE = {"SMS_MOCK_FIXED_CODE": "bad", "APP_ENV": "production", "SOLAPI_API_KEY": "x",
               "ACCOUNT_EXIT_RECORD_POLICY": "delete", "DATABASE_URL": "sqlite://", "PRIVACY_REQUIRED_CONSENTS": "X"}

    def test_process_environment_and_a_local_env_file_do_not_change_the_result(self):
        baseline = self.deploy()
        with patch.dict(os.environ, self.HOSTILE):
            self.assertEqual(self.deploy(), baseline)
            failing = self.deploy({"APP_ENV": "production"})
        self.assertEqual(self.deploy({"APP_ENV": "production"}), failing)
        # A separate process with a hostile environment and a hostile .env in its working directory.
        self.write(".env", "SMS_MOCK_FIXED_CODE=bad\nSOLAPI_API_KEY=x\n")
        api = self.write("runtime.env", render(VALID_API))
        env = {**os.environ, **self.HOSTILE, "PYTHONUTF8": "1"}
        result = subprocess.run([sys.executable, "-I", str(SCRIPT), "deploy", "--api", str(api)],
                                cwd=self.dir, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn(FIXED, result.stdout + result.stderr)

    def test_the_checker_never_reads_the_environment_or_a_dotenv_loader(self):
        source = SCRIPT.read_text(encoding="utf-8")
        for forbidden in ("import os", "getenv(", "environ[", "environ.get", "load_dotenv", "import app", "from app"):
            self.assertNotIn(forbidden, source)


class AgreementTests(unittest.TestCase):
    """The checker's key list and rules must follow the application."""

    def code_keys(self):
        keys = set()
        for path in (ROOT / "app").rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            keys.update(re.findall(r"os\.(?:getenv|environ\.get)\(\s*\"([A-Z][A-Z0-9_]*)\"", text))
            for prefix in re.findall(r"RateLimiter\.from_env\(\s*\"[^\"]+\",\s*\"([A-Z_]+)\"", text):
                keys.update({f"{prefix}_RATE_LIMIT_REQUESTS", f"{prefix}_RATE_LIMIT_WINDOW_SECONDS"})
            for prefix in re.findall(r"FailureLimiter\.from_env\(\s*\"([A-Z_]+)\"", text):
                keys.update({f"{prefix}_FAILURE_LIMIT", f"{prefix}_FAILURE_WINDOW_SECONDS"})
        keys.add(sms_provider.FIXED_CODE_SETTING)
        keys.update({"SOLAPI_API_KEY", "SOLAPI_API_SECRET", "SOLAPI_SENDER_NUMBER"})  # read as a tuple
        return keys

    def test_every_setting_the_code_reads_is_known_and_nothing_else(self):
        self.assertEqual(self.code_keys(), set(checker.API_KEYS) | set(checker.SOLAPI_KEYS))

    def test_indirect_reads_are_the_known_ones(self):
        indirect = []
        for path in (ROOT / "app").rglob("*.py"):
            for match in re.finditer(r"os\.getenv\(\s*([^\"\s)][^)]*)\)", path.read_text(encoding="utf-8")):
                indirect.append((path.relative_to(ROOT).as_posix(), match.group(1).strip()))
        self.assertEqual(sorted(indirect), [("app/rate_limit.py", "name"), ("app/sms_provider.py", "FIXED_CODE_SETTING"),
                                            ("app/sms_provider.py", "FIXED_CODE_SETTING"), ("app/sms_provider.py", "name")])

    def test_rules_mirror_the_application_constants(self):
        self.assertEqual(checker.MOCK_ENVIRONMENTS, sms_provider.MOCK_ALLOWED_ENVIRONMENTS)
        self.assertEqual(checker.MOCK_ENVIRONMENTS, location_geocoder.MOCK_ENVIRONMENTS)
        self.assertEqual(checker.HMAC_KEY_MIN_LENGTH, phone_service.HMAC_KEY_MIN_LENGTH)
        self.assertEqual(checker.HMAC_KEY_MIN_LENGTH, shelter_code_service.HMAC_KEY_MIN_LENGTH)
        self.assertEqual(checker.FIXED_CODE.pattern, sms_provider.FIXED_CODE_FORMAT.pattern)
        self.assertEqual(checker.PLEDGE_VERSION_MAX_LENGTH, pledge_service.PLEDGE_VERSION_MAX_LENGTH)
        self.assertEqual(checker.DOCUMENT_VERSION_MAX_LENGTH, privacy_policy.DOCUMENT_VERSION_MAX_LENGTH)
        self.assertEqual(checker.CONSENT_TYPES, set(privacy_policy.CONSENT_FIELDS))

    def test_the_auth_test_modules_clear_the_fixed_code(self):
        for name in ("test_onboarding_basic_info", "test_age_review", "test_age_review_access",
                     "test_onboarding_integration_postgres", "test_signup_postgres", "test_users_me_onboarding"):
            with self.subTest(module=name):
                self.assertIn('"SMS_MOCK_FIXED_CODE": ""', (ROOT / "tests" / f"{name}.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
