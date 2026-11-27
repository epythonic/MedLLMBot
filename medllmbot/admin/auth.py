"""Admin login, session in a signed cookie.

Two kinds of accounts: the owner's username and password from .env, and extra accounts from the «Administrators»
section (the admin_users table) with an email and a scrypt password hash.
"""

import asyncio
import hashlib
import hmac
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import final, override

from sqladmin.authentication import AuthenticationBackend
from starlette.requests import Request
from starlette.responses import Response

from medllmbot.admin.i18n import gettext
from medllmbot.admin.passwords import dummy_hash, verify_password
from medllmbot.config import AdminSettings
from medllmbot.db.models import AdminUser
from medllmbot.db.repository import Repository

# The session lasts a working day, not Starlette's default 14 days
SESSION_MAX_AGE = 8 * 3600
# request.state attribute with the AdminIdentity of a checked session
IDENTITY_STATE = "admin_identity"


@dataclass(frozen=True)
class AdminIdentity:
    """Who is signed in, for the admin header: the owner from .env or an account from «Administrators»."""

    name: str  # the owner's username or the account's email
    account_id: int | None = None  # None: the owner, who has no row in admin_users

    @property
    def is_owner(self) -> bool:
        return self.account_id is None

    @property
    def initial(self) -> str:
        """The avatar letter: the first letter or digit of the name."""
        return next((char.upper() for char in self.name if char.isalnum()), "?")


def current_admin(request: Request) -> AdminIdentity | None:
    """Who the page is shown to; None where the session is not checked (the login page)."""
    identity: object = getattr(request.state, IDENTITY_STATE, None)
    return identity if isinstance(identity, AdminIdentity) else None


@final
class LoginThrottle:
    """Password brute-force protection: at most max_failures failed logins per window seconds from one address.

    Counters live in process memory: restarting the admin resets them, which is enough for a single admin.
    """

    max_failures: int
    window: float
    _clock: Callable[[], float]
    _failures: dict[str, list[float]]

    def __init__(self, max_failures: int = 5, window: float = 900.0, clock: Callable[[], float] = time.monotonic) -> None:
        self.max_failures = max_failures
        self.window = window
        self._clock = clock
        self._failures = {}

    def blocked(self, key: str) -> bool:
        return len(self._recent(key)) >= self.max_failures

    def failed(self, key: str) -> None:
        self._failures[key] = [*self._recent(key), self._clock()]

    def reset(self, key: str) -> None:
        self._failures.pop(key, None)

    def _recent(self, key: str) -> list[float]:
        since = self._clock() - self.window
        recent = [moment for moment in self._failures.get(key, []) if moment > since]
        if recent:
            self._failures[key] = recent
        else:
            self._failures.pop(key, None)  # idle addresses do not pile up in memory
        return recent


@final
class AdminAuth(AuthenticationBackend):
    username: str
    password: str
    throttle: LoginThrottle
    _repo: Repository
    _secret_key: bytes
    _session_token: str

    def __init__(self, settings: AdminSettings, repo: Repository, throttle: LoginThrottle | None = None) -> None:
        secret_key = settings.admin_secret_key.get_secret_value()
        super().__init__(secret_key=secret_key, max_age=SESSION_MAX_AGE, https_only=settings.admin_https_only)
        self.username = settings.admin_username
        self.password = settings.admin_password.get_secret_value()
        self.throttle = throttle or LoginThrottle()
        self._repo = repo
        self._secret_key = secret_key.encode()
        # The cookie holds a fingerprint of username and password, not the username: changing the password in .env
        # ends all old sessions
        self._session_token = self._fingerprint(f"{self.username}\n{self.password}")

    def _fingerprint(self, value: str) -> str:
        return hmac.new(self._secret_key, value.encode(), hashlib.sha256).hexdigest()

    def _account_token(self, user: AdminUser) -> str:
        """Session token of an account: a new password or a new hash ends the account's old sessions."""
        return self._fingerprint(f"account\n{user.id}\n{user.password_hash}")

    async def _session_for(self, username: str, password: str) -> dict[str, str | int] | None:
        """Session contents for valid credentials, None for invalid ones."""
        # compare_digest compares in constant time, with no timing leak
        owner = secrets.compare_digest(username.encode(), self.username.encode()) & (
            secrets.compare_digest(password.encode(), self.password.encode())
        )
        if owner:
            return {"admin": self._session_token}
        user = await self._repo.admin_user_by_email(username)
        # An unknown email is checked against a dummy hash: it takes as long as a wrong password and does not reveal
        # which emails have accounts. scrypt is CPU-bound, so it runs off the event loop
        stored = user.password_hash if user is not None else dummy_hash()
        matches = await asyncio.to_thread(verify_password, password, stored)
        if user is None or not matches or not user.is_active:
            return None
        await self._repo.mark_admin_login(user.id)
        return {"admin": self._account_token(user), "account": user.id}

    @override
    async def login(self, request: Request) -> bool:
        client = request.client.host if request.client else "unknown"
        if self.throttle.blocked(client):
            # The login page shows the error as is, so it is translated here, in the request's language
            request.state.login_error = " ".join(
                (gettext("Too many failed login attempts."), gettext("Try again in 15 minutes."))
            )
            return False
        form = await request.form()
        username = str(form.get("username", ""))
        password = str(form.get("password", ""))
        session = await self._session_for(username, password)
        if session is None:
            self.throttle.failed(client)
            request.state.login_error = gettext("Wrong username or password.")
            return False
        self.throttle.reset(client)
        request.session.clear()
        request.session.update(session)
        return True

    @override
    async def logout(self, request: Request) -> bool:
        request.session.clear()
        return True

    @override
    async def authenticate(self, request: Request) -> Response | bool:
        identity = await self._session_identity(request)
        if identity is not None:
            # The header shows who is signed in (current_admin): the check has just loaded the account anyway
            setattr(request.state, IDENTITY_STATE, identity)
            return True
        if request.headers.get("HX-Request") == "true":
            # The session expired during an HTMX request: instead of the login page inside a fragment,
            # ask HTMX to navigate to it as a whole page
            return Response(status_code=401, headers={"HX-Redirect": str(request.url_for("admin:login"))})
        return False

    async def _session_identity(self, request: Request) -> AdminIdentity | None:
        """Whose session the request carries; None if there is none or it has ended."""
        token: object = request.session.get("admin")
        if not isinstance(token, str):
            return None
        if secrets.compare_digest(token, self._session_token):
            return AdminIdentity(self.username)
        user = await self._session_account(request, token)
        return AdminIdentity(user.email, user.id) if user is not None else None

    async def _session_account(self, request: Request, token: str) -> AdminUser | None:
        """The session's account, if it still exists, is active and has the password the session was opened with."""
        account_id: object = request.session.get("account")
        if not isinstance(account_id, int):
            return None
        user = await self._repo.get_admin_user(account_id)
        if user is None or not user.is_active or not secrets.compare_digest(token, self._account_token(user)):
            return None
        return user
