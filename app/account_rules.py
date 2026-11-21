"""Account field rules shared by sign-up and the availability checks.

Each function returns the stored form of a value or raises a 422 ContractError.
The database compares lower() of the stored form, so these rules and the
unique indexes on users decide duplicates the same way.
"""
import re
import string
import unicodedata

from email_validator import EmailNotValidError, validate_email

from app.api_contract import REQUIRED_FIELD_MESSAGE, ContractError

LOGIN_ID_MIN_LENGTH, LOGIN_ID_MAX_LENGTH = 4, 20
NICKNAME_MIN_LENGTH, NICKNAME_MAX_LENGTH = 3, 20
EMAIL_MAX_LENGTH = 100
PASSWORD_MIN_LENGTH, PASSWORD_MAX_LENGTH = 15, 128

LOGIN_ID_PATTERN = re.compile(r"[a-z0-9_]+")
NICKNAME_PATTERN = re.compile(r"[가-힣A-Za-z0-9_]+")
PASSWORD_SPECIAL_CHARACTERS = frozenset(string.punctuation)


def invalid(code, message, data):
    return ContractError(422, code, message, data)


# Availability checks use their own *_REQUIRED codes; sign-up uses REQUIRED_FIELD.
REQUIRED_MESSAGES = {"LOGIN_ID_REQUIRED": "아이디를 입력해주세요.",
                     "NICKNAME_REQUIRED": "닉네임을 입력해주세요."}


def required_text(value, field, required_code):
    if not isinstance(value, str) or not value.strip():
        message = REQUIRED_MESSAGES.get(required_code, REQUIRED_FIELD_MESSAGE)
        raise invalid(required_code, message, {"field": field})
    return value


def check_length(value, field, code, message, minimum, maximum):
    if not minimum <= len(value) <= maximum:
        raise invalid(code, message, {"field": field, "min_length": minimum, "max_length": maximum})


def normalize_login_id(value, required_code="REQUIRED_FIELD"):
    value = required_text(value, "login_id", required_code).strip()
    # Only ASCII is lowercased; non-ASCII letters must not fold into valid IDs.
    if value.isascii():
        value = value.lower()
    check_length(value, "login_id", "INVALID_LOGIN_ID_LENGTH", "아이디는 4자 이상 20자 이하로 입력해주세요.",
                 LOGIN_ID_MIN_LENGTH, LOGIN_ID_MAX_LENGTH)
    if not LOGIN_ID_PATTERN.fullmatch(value):
        raise invalid("INVALID_LOGIN_ID_FORMAT", "아이디는 영문 소문자, 숫자, 밑줄(_)만 사용할 수 있습니다.",
                      {"field": "login_id"})
    return value


def normalize_nickname(value, required_code="REQUIRED_FIELD"):
    value = required_text(value, "nickname", required_code)
    value = unicodedata.normalize("NFC", value).strip()
    check_length(value, "nickname", "INVALID_NICKNAME_LENGTH", "닉네임은 3자 이상 20자 이하로 입력해주세요.",
                 NICKNAME_MIN_LENGTH, NICKNAME_MAX_LENGTH)
    if not NICKNAME_PATTERN.fullmatch(value):
        raise invalid("INVALID_NICKNAME_FORMAT", "닉네임은 한글, 영문, 숫자, 밑줄(_)만 사용할 수 있습니다.",
                      {"field": "nickname"})
    return value


def normalize_email(value):
    value = required_text(value, "email", "REQUIRED_FIELD").strip()
    error = invalid("INVALID_EMAIL", "올바른 이메일 주소를 입력해주세요.", {"field": "email"})
    if len(value) > EMAIL_MAX_LENGTH:
        raise error
    try:
        normalized = validate_email(value, check_deliverability=False).normalized.lower()
    except EmailNotValidError:
        raise error from None
    if len(normalized) > EMAIL_MAX_LENGTH:
        raise error
    return normalized


PASSWORD_RULES = (
    ("uppercase", "PASSWORD_UPPERCASE_REQUIRED", "비밀번호에 영문 대문자를 1개 이상 포함해주세요.",
     lambda c: "A" <= c <= "Z"),
    ("lowercase", "PASSWORD_LOWERCASE_REQUIRED", "비밀번호에 영문 소문자를 1개 이상 포함해주세요.",
     lambda c: "a" <= c <= "z"),
    ("number", "PASSWORD_NUMBER_REQUIRED", "비밀번호에 숫자를 1개 이상 포함해주세요.",
     lambda c: "0" <= c <= "9"),
    ("special", "PASSWORD_SPECIAL_REQUIRED", "비밀번호에 특수문자를 1개 이상 포함해주세요.",
     lambda c: c in PASSWORD_SPECIAL_CHARACTERS),
)


def validate_password(value):
    """Return the password unchanged: no trimming or normalization before hashing."""
    if not isinstance(value, str) or value == "":
        raise invalid("REQUIRED_FIELD", REQUIRED_FIELD_MESSAGE, {"field": "password"})
    check_length(value, "password", "INVALID_PASSWORD_LENGTH", "비밀번호는 15자 이상 128자 이하로 입력해주세요.",
                 PASSWORD_MIN_LENGTH, PASSWORD_MAX_LENGTH)
    for rule, code, message, matches in PASSWORD_RULES:
        if not any(matches(character) for character in value):
            raise invalid(code, message, {"field": "password", "rule": rule})
    return value
