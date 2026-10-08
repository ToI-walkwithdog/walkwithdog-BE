"""Role checks share the same hashed bearer sessions as all private APIs."""
from fastapi import Depends, HTTPException

from app.auth import get_current_user
from app.models import User, UserRole


def require_role(*roles: UserRole):
    def checker(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role not in roles:
            raise HTTPException(status_code=403, detail="권한이 없습니다.")
        return current_user
    return checker


require_login = require_role(UserRole.USER, UserRole.SHELTER, UserRole.ADMIN)
require_shelter = require_role(UserRole.SHELTER, UserRole.ADMIN)
require_admin = require_role(UserRole.ADMIN)


def require_shelter_access(current_user: User, shelter_id: int | None):
    """Check ownership before any dog fields or calculated traits are changed."""
    if current_user.role == UserRole.ADMIN:
        return
    if (current_user.role != UserRole.SHELTER or current_user.shelter_id is None
            or current_user.shelter_id != shelter_id):
        raise HTTPException(status_code=403, detail="담당 보호소의 강아지만 관리할 수 있습니다.")
