"""Backend-for-Frontend (BFF) authentication with Entra ID.

* The server is a *confidential client*, but it has NO client secret: MSAL proves its
  identity with a client assertion, a token for the app's Managed Identity
  (Federated Identity Credential on the app registration).
* The browser never sees an OAuth token. It only gets an opaque, random session ID in a
  `__Host-` cookie (Secure, HttpOnly, SameSite=Lax). Session data lives on the server.
* State-changing requests need a per-session CSRF token header and a matching Origin.
"""

from __future__ import annotations

import hmac
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass
from urllib.parse import urlsplit

import msal
from fastapi import Depends, HTTPException, Request, Response, status

from .config import Settings, get_settings
from .identity import TOKEN_EXCHANGE_SCOPE

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
CSRF_HEADER = "X-CSRF-Token"


@dataclass
class User:
    user_id: str  # Entra object ID (oid)
    name: str
    roles: list[str]
    csrf_token: str

    def has_role(self, role: str) -> bool:
        return role in self.roles


# --------------------------------------------------------------------------
# Server-side session store
# --------------------------------------------------------------------------


class SessionStore:
    """In-memory, single-instance store.

    For more than one app instance, swap this for Azure Cache for Redis using
    Entra ID auth (no access keys).
    """

    def __init__(self, ttl_seconds: int) -> None:
        self._ttl = ttl_seconds
        self._data: dict[str, tuple[float, dict]] = {}
        self._lock = threading.Lock()

    def create(self, data: dict) -> str:
        sid = secrets.token_urlsafe(32)
        with self._lock:
            self._purge()
            self._data[sid] = (time.time() + self._ttl, data)
        return sid

    def get(self, sid: str | None) -> dict | None:
        if not sid:
            return None
        with self._lock:
            entry = self._data.get(sid)
            if not entry or entry[0] < time.time():
                self._data.pop(sid, None)
                return None
            return entry[1]

    def delete(self, sid: str | None) -> None:
        if sid:
            with self._lock:
                self._data.pop(sid, None)

    def _purge(self) -> None:
        now = time.time()
        for k in [k for k, (exp, _) in self._data.items() if exp < now]:
            del self._data[k]


class RateLimiter:
    def __init__(self, limit: int, window_seconds: int = 3600) -> None:
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        now = time.time()
        with self._lock:
            q = self._hits.setdefault(key, deque())
            while q and q[0] < now - self.window:
                q.popleft()
            if len(q) >= self.limit:
                raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                                    "Too many photo checks. Please try again later.")
            q.append(now)


@dataclass
class AuthState:
    settings: Settings
    sessions: SessionStore
    msal_app: msal.ConfidentialClientApplication | None
    analysis_limiter: RateLimiter

    @property
    def cookie_name(self) -> str:
        # __Host- requires Secure, Path=/ and no Domain: prevents subdomain cookie injection.
        return "__Host-tyre_session" if self.settings.is_production else "tyre_session"


def build_auth_state(settings: Settings, credential) -> AuthState:
    msal_app = None
    if not settings.dev_auth_bypass:
        def client_assertion() -> str:
            # Short-lived Managed Identity token used instead of a client secret.
            return credential.get_token(TOKEN_EXCHANGE_SCOPE).token

        msal_app = msal.ConfidentialClientApplication(
            client_id=settings.entra_client_id,
            authority=settings.entra_authority,
            client_credential={"client_assertion": client_assertion},
        )
    return AuthState(
        settings=settings,
        sessions=SessionStore(settings.session_ttl_seconds),
        msal_app=msal_app,
        analysis_limiter=RateLimiter(settings.analyses_per_hour_per_user),
    )


def get_auth_state(request: Request) -> AuthState:
    return request.app.state.auth


def set_session_cookie(response: Response, auth: AuthState, sid: str, max_age: int) -> None:
    response.set_cookie(
        auth.cookie_name, sid, max_age=max_age, path="/",
        secure=auth.settings.is_production, httponly=True, samesite="lax",
    )


def clear_session_cookie(response: Response, auth: AuthState) -> None:
    response.delete_cookie(auth.cookie_name, path="/", secure=auth.settings.is_production,
                           httponly=True, samesite="lax")


# --------------------------------------------------------------------------
# Login flow helpers (used by routes in main.py)
# --------------------------------------------------------------------------


def start_login(auth: AuthState) -> tuple[str, str]:
    """Return (pre-auth session id, Entra authorize URL). MSAL adds PKCE, state and nonce."""
    assert auth.msal_app is not None
    flow = auth.msal_app.initiate_auth_code_flow(scopes=[], redirect_uri=auth.settings.redirect_uri)
    sid = auth.sessions.create({"flow": flow})
    return sid, flow["auth_uri"]


def finish_login(auth: AuthState, pre_sid: str | None, query: dict) -> str:
    """Validate the callback, then create a fresh session (prevents session fixation)."""
    assert auth.msal_app is not None
    pre = auth.sessions.get(pre_sid)
    auth.sessions.delete(pre_sid)
    if not pre or "flow" not in pre:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Login session expired. Please sign in again.")
    try:
        result = auth.msal_app.acquire_token_by_auth_code_flow(pre["flow"], query)
    except ValueError as exc:  # state mismatch / tampered response
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid sign-in response.") from exc
    if "error" in result:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign-in failed.")
    claims = result.get("id_token_claims") or {}
    if not claims.get("oid"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign-in failed.")
    return create_user_session(
        auth,
        user_id=claims["oid"],
        name=claims.get("name") or claims.get("preferred_username") or "Customer",
        roles=list(claims.get("roles") or []),
    )


def create_user_session(auth: AuthState, *, user_id: str, name: str, roles: list[str]) -> str:
    return auth.sessions.create({
        "user": {"user_id": user_id, "name": name, "roles": roles,
                 "csrf_token": secrets.token_urlsafe(32)},
        "analyses": {},
    })


# --------------------------------------------------------------------------
# FastAPI dependencies
# --------------------------------------------------------------------------


def get_session(request: Request, auth: AuthState = Depends(get_auth_state)) -> dict:
    session = auth.sessions.get(request.cookies.get(auth.cookie_name))
    if not session or "user" not in session:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Please sign in.")
    if request.method not in SAFE_METHODS:
        _verify_csrf(request, auth.settings, session["user"]["csrf_token"])
    return session


def current_user(session: dict = Depends(get_session)) -> User:
    return User(**session["user"])


def require_admin(user: User = Depends(current_user),
                  settings: Settings = Depends(get_settings)) -> User:
    if not user.has_role(settings.admin_app_role):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Staff access only.")
    return user


def _verify_csrf(request: Request, settings: Settings, expected: str) -> None:
    origin = request.headers.get("origin")
    if origin:
        allowed = urlsplit(settings.public_base_url)
        got = urlsplit(origin)
        if (got.scheme, got.netloc) != (allowed.scheme, allowed.netloc):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Cross-origin request blocked.")
    token = request.headers.get(CSRF_HEADER, "")
    if not token or not hmac.compare_digest(token, expected):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Missing or invalid CSRF token.")
