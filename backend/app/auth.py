"""Supabase bearer authentication and reusable role dependencies."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import Depends, Request

UserRole = Literal["support_agent", "business_owner"]
VALID_ROLES: frozenset[str] = frozenset({"support_agent", "business_owner"})


class AuthError(Exception):
    """Safe, client-facing authentication/authorization failure."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


@dataclass(frozen=True)
class AuthenticatedUser:
    user_id: str
    role: UserRole
    email: str | None = None
    development: bool = False

    @property
    def is_support_agent(self) -> bool:
        return self.role == "support_agent"


class SupabaseAuthenticator:
    """Validate access tokens and load the server-owned profile role.

    The caller supplies the already-created service-role Supabase client. The
    service key never leaves the backend; it is used only to verify the token
    and read the profile row.
    """

    def __init__(self, client: Any) -> None:
        self.client = client

    def authenticate(self, token: str) -> AuthenticatedUser:
        if not token:
            raise AuthError(401, "AUTH_REQUIRED", "A valid access token is required.")
        auth = getattr(self.client, "auth", None)
        get_user = getattr(auth, "get_user", None)
        if not callable(get_user):
            raise AuthError(503, "AUTH_PROVIDER_UNAVAILABLE", "Authentication is temporarily unavailable.")
        try:
            response = get_user(token)
        except Exception as exc:
            raise AuthError(401, "INVALID_TOKEN", "The access token is invalid or expired.") from exc

        user = getattr(response, "user", None)
        response_data = getattr(response, "data", None)
        if user is None:
            user = getattr(response_data, "user", None)
        if user is None and isinstance(response, dict):
            user = response.get("user")
        if user is None and isinstance(response_data, dict):
            user = response_data.get("user")
        user_id = getattr(user, "id", None) if user is not None else None
        if user_id is None and isinstance(user, dict):
            user_id = user.get("id")
        if not user_id:
            raise AuthError(401, "INVALID_TOKEN", "The access token is invalid or expired.")

        profile = self._profile(str(user_id))
        role = profile.get("role") if profile else None
        if role not in VALID_ROLES:
            raise AuthError(403, "PROFILE_REQUIRED", "Your account does not have an enabled PayPilot role.")
        email = getattr(user, "email", None)
        if email is None and isinstance(user, dict):
            email = user.get("email")
        return AuthenticatedUser(user_id=str(user_id), role=role, email=email)

    def _profile(self, user_id: str) -> dict[str, Any] | None:
        try:
            response = (
                self.client.table("profiles")
                .select("id,role")
                .eq("id", user_id)
                .limit(1)
                .execute()
            )
        except Exception as exc:
            raise AuthError(503, "PROFILE_UNAVAILABLE", "Account authorization is temporarily unavailable.") from exc
        rows = getattr(response, "data", None) or []
        return rows[0] if rows else None


def bearer_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise AuthError(401, "AUTH_REQUIRED", "A valid access token is required.")
    return token.strip()


def build_auth_dependencies(settings: Any, authenticator: SupabaseAuthenticator | Any | None) -> tuple[Any, Any]:
    """Build request-scoped auth and support-agent dependencies for an app."""

    async def current_user(request: Request) -> AuthenticatedUser:
        if not settings.require_auth:
            return AuthenticatedUser(
                user_id="00000000-0000-0000-0000-000000000001",
                role="support_agent",
                email="dev@localhost",
                development=True,
            )
        token = bearer_token(request)
        if authenticator is None:
            raise AuthError(503, "AUTH_PROVIDER_UNAVAILABLE", "Authentication is temporarily unavailable.")
        try:
            return await asyncio.to_thread(authenticator.authenticate, token)
        except AuthError:
            raise
        except Exception as exc:
            raise AuthError(503, "AUTH_PROVIDER_UNAVAILABLE", "Authentication is temporarily unavailable.") from exc

    async def support_agent(user: AuthenticatedUser = Depends(current_user)) -> AuthenticatedUser:
        if not user.is_support_agent:
            raise AuthError(403, "FORBIDDEN", "This operation requires a support-agent role.")
        return user

    return current_user, support_agent
