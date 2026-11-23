import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.account_rules import PASSWORD_MAX_LENGTH
from app.api_contract import ContractError, ContractErrorResponse, ContractRoute, ok
from app.auth import get_contract_session, hash_password, token_digest, verify_password
from app.database import get_db
from app.models import AuthSession, User
from app.rate_limit import RateLimiter
from app.schemas import EmptyContractResponse, LoginRequest, LoginResponse, TokenResponse

router = APIRouter(prefix="/auth", tags=["auth"])
# Login and logout answer with {code, data, message}. GET /auth/me was removed in
# Issue #32; GET /users/me is the only "who am I" route.
contract = APIRouter(route_class=ContractRoute)
SESSION_SECONDS = 3600
DUMMY_HASH = hash_password(secrets.token_urlsafe(32))
# Per client IP; every attempt counts (also 422 and successes). Interim default,
# override with LOGIN_RATE_LIMIT_REQUESTS / LOGIN_RATE_LIMIT_WINDOW_SECONDS.
LOGIN_RATE_LIMITER = RateLimiter.from_env("login", "LOGIN", 10, 60,
                                          message="로그인 요청이 너무 많습니다. 잠시 후 다시 시도해 주세요.")
UNAUTHORIZED_RESPONSE = {
    "model": ContractErrorResponse,
    "description": "AUTHENTICATION_REQUIRED(헤더 없음, Bearer 아님), INVALID_ACCESS_TOKEN(위조, 폐기, 길이 초과), "
                   "SESSION_EXPIRED",
}


def invalid_credentials() -> ContractError:
    # One code and message for every failure, so the response never reveals whether an account exists.
    return ContractError(401, "INVALID_CREDENTIALS", "아이디와 비밀번호를 확인해 주세요.",
                         headers={"WWW-Authenticate": "Bearer"})


def authenticate(db: Session, login_id: str, password: str) -> User:
    # No stored password can be longer, since sign-up caps it; skip the lookup entirely.
    if len(password) > PASSWORD_MAX_LENGTH:
        raise invalid_credentials()
    # Same lower() expression as uq_users_login_id_lower; login_id is the only lookup key.
    users = db.query(User).filter(func.lower(User.login_id) == login_id).all()
    user = users[0] if len(users) == 1 else None
    stored = user.password if user and (user.password or "").startswith("scrypt$") else DUMMY_HASH
    # Always run one scrypt check, so unknown accounts take as long as wrong passwords.
    valid = verify_password(password, stored)
    if not user or stored == DUMMY_HASH or not valid:
        raise invalid_credentials()
    return user


@contract.post("/login", response_model=LoginResponse, dependencies=[Depends(LOGIN_RATE_LIMITER)],
               responses={401: {"model": ContractErrorResponse,
                                "description": "INVALID_CREDENTIALS. 미등록 계정, 비밀번호 불일치, 사용할 수 없는 비밀번호 해시"},
                          422: {"model": ContractErrorResponse,
                                "description": "IDENTIFIER_REQUIRED, PASSWORD_REQUIRED, INVALID_IDENTIFIER_FORMAT(100자 초과)"},
                          429: {"model": ContractErrorResponse,
                                "description": "TOO_MANY_REQUESTS. 클라이언트 IP별 로그인 시도 제한 초과"},
                          500: {"model": ContractErrorResponse, "description": "INTERNAL_SERVER_ERROR"}})
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    user = authenticate(db, payload.identifier, payload.password)
    token = secrets.token_urlsafe(32)
    db.add(AuthSession(token_hash=token_digest(token), user_id=user.id,
                       expires_at=datetime.now(timezone.utc) + timedelta(seconds=SESSION_SECONDS)))
    db.commit()
    data = TokenResponse(access_token=token, expires_in=SESSION_SECONDS).model_dump()
    response = ok(data, "로그인에 성공했습니다.")
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return response


@contract.post("/logout", response_model=EmptyContractResponse,
               responses={401: UNAUTHORIZED_RESPONSE,
                          500: {"model": ContractErrorResponse, "description": "INTERNAL_SERVER_ERROR"}})
def logout(session=Depends(get_contract_session), db: Session = Depends(get_db)):
    # Only this token's session is removed; the user's other sessions stay valid.
    # A retried logout finds no session and gets 401 INVALID_ACCESS_TOKEN.
    db.delete(session)
    db.commit()
    response = ok({}, "로그아웃이 완료되었습니다.")
    response.headers["Cache-Control"] = "no-store"
    return response


router.include_router(contract)
