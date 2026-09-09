"""
Auth helpers for demo JWT tokens.

Two roles: REVIEWER and RELEASE_APPROVER.
Tokens are HS256 JWTs. The secret comes from settings.

In production, replace with a real IdP. This is demo-grade auth only.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from creditlock.settings import get_settings

Role = Literal["REVIEWER", "RELEASE_APPROVER"]

_bearer = HTTPBearer(auto_error=False)


def _get_validated_jwt_secret() -> str:
    settings = get_settings()
    secret = settings.jwt_secret
    if not secret or len(secret) < 32:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="JWT_SECRET is missing or weak (must be at least 32 characters). Production startup failed closed.",
        )
    return secret


def create_token(
    actor_id: str,
    role: Role,
    expires_minutes: int | None = None,
    demo_session_id: str | None = None,
    production_id: str | None = None,
) -> str:
    settings = get_settings()
    secret = _get_validated_jwt_secret()
    exp_minutes = expires_minutes or settings.jwt_expire_minutes
    payload: dict[str, Any] = {
        "sub": actor_id,
        "role": role,
        "exp": datetime.now(UTC) + timedelta(minutes=exp_minutes),
        "iat": datetime.now(UTC),
    }
    if demo_session_id:
        payload["demo_session_id"] = demo_session_id
    if production_id:
        payload["production_id"] = production_id
    return jwt.encode(payload, secret, algorithm=settings.jwt_algorithm)


def check_token_production_access(actor: dict[str, Any], production_id: str) -> None:
    """
    Reject access if token is not authorized for the specified production_id.

    Invariants:
    - For production IDs beginning with 'prod_demo_', token MUST have 'production_id'
      and 'demo_session_id' matching the requested production_id. Unbound tokens or
      cross-session tokens receive HTTP 403 Forbidden.
    - For all other production IDs, if token has a 'production_id' set and it differs
      from requested production_id, receive HTTP 403 Forbidden.
    """
    token_pid = actor.get("production_id")
    token_sid = actor.get("demo_session_id")

    if production_id.startswith("prod_demo_"):
        if not token_pid or not token_sid or token_pid != production_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied: Token is not authorized for demo production '{production_id}'.",
            )
    elif token_pid and token_pid != production_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access denied: Token bound to production '{token_pid}' cannot access '{production_id}'.",
        )


def _decode_token(token: str) -> dict[str, Any]:
    settings = get_settings()
    secret = _get_validated_jwt_secret()
    return jwt.decode(token, secret, algorithms=[settings.jwt_algorithm])


def get_current_actor(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
) -> dict[str, Any]:
    """
    Dependency: returns the decoded token payload after validating core identity claims.
    Raises 401 if token is missing, expired, invalid, or subject ('sub') is missing/empty.
    Raises 403 if role claim is missing, empty, or unrecognized.
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        payload = _decode_token(credentials.credentials)
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    sub = payload.get("sub")
    if not sub or not str(sub).strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token subject 'sub' claim is missing or empty.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    role = payload.get("role")
    if not role or role not in ("REVIEWER", "RELEASE_APPROVER"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Invalid or untrusted role '{role}'. Must be 'REVIEWER' or 'RELEASE_APPROVER'.",
        )

    return payload


def require_role(
    required_role: Role,
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """
    Dependency factory: raises 403 if actor does not have the required role.
    Must be composed after get_current_actor.
    """

    def _check(
        actor: Annotated[dict[str, Any], Depends(get_current_actor)],
    ) -> dict[str, Any]:
        if actor.get("role") != required_role:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{required_role}' required. Actor has role '{actor.get('role')}'.",
            )
        return actor

    return _check
