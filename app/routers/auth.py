import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.account_rules import PASSWORD_MAX_LENGTH
from app.api_contract import ContractError, ContractErrorResponse, ContractRoute, ok
from app.auth import get_current_session, get_current_user, hash_password, token_digest, verify_password
from app.database import get_db
from app.models import AuthSession, User
from app.schemas import LoginRequest, LoginResponse, TokenResponse, UserResponse

router = APIRouter(prefix="/auth", tags=["auth"])
# Login answers with {code, data, message}; logout and /auth/me keep FastAPI errors.
contract = APIRouter(route_class=ContractRoute)
SESSION_SECONDS = 3600
DUMMY_HASH = hash_password(secrets.token_urlsafe(32))


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


@contract.post("/login", response_model=LoginResponse,
               responses={401: {"model": ContractErrorResponse,
                                "description": "INVALID_CREDENTIALS. 미등록 계정, 비밀번호 불일치, 사용할 수 없는 비밀번호 해시"},
                          422: {"model": ContractErrorResponse,
                                "description": "IDENTIFIER_REQUIRED, PASSWORD_REQUIRED, INVALID_IDENTIFIER_FORMAT(100자 초과)"},
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


router.include_router(contract)


@router.post("/logout", status_code=204)
def logout(session=Depends(get_current_session), db: Session = Depends(get_db)):
    db.delete(session)
    db.commit()
    return Response(status_code=204)


@router.get("/me", response_model=UserResponse)
def get_me(current_user: User = Depends(get_current_user)):
    return current_user
