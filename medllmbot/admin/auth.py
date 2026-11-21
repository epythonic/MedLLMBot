"""Вход в админку по логину и паролю из .env, сессия в подписанной cookie."""

import hashlib
import hmac
import secrets
import time
from collections.abc import Callable
from typing import final, override

from sqladmin.authentication import AuthenticationBackend
from starlette.requests import Request
from starlette.responses import Response

from medllmbot.config import AdminSettings

# Сессия живёт рабочий день, а не 14 дней по умолчанию Starlette
SESSION_MAX_AGE = 8 * 3600
LOCKED_OUT = "Слишком много неудачных попыток входа. Попробуйте через 15 минут."
WRONG_CREDENTIALS = "Неверный логин или пароль."


@final
class LoginThrottle:
    """Защита от перебора пароля: не больше max_failures неудачных входов за window секунд с одного адреса.

    Счётчики в памяти процесса: перезапуск админки их сбрасывает, для одной админки этого достаточно.
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
            self._failures.pop(key, None)  # молчащие адреса не копятся в памяти
        return recent


@final
class AdminAuth(AuthenticationBackend):
    username: str
    password: str
    throttle: LoginThrottle
    _session_token: str

    def __init__(self, settings: AdminSettings, throttle: LoginThrottle | None = None) -> None:
        secret_key = settings.admin_secret_key.get_secret_value()
        super().__init__(secret_key=secret_key, max_age=SESSION_MAX_AGE, https_only=settings.admin_https_only)
        self.username = settings.admin_username
        self.password = settings.admin_password.get_secret_value()
        self.throttle = throttle or LoginThrottle()
        # В cookie кладём не логин, а отпечаток логина и пароля: смена пароля в .env завершает все старые сессии
        self._session_token = hmac.new(
            secret_key.encode(), f"{self.username}\n{self.password}".encode(), hashlib.sha256
        ).hexdigest()

    @override
    async def login(self, request: Request) -> bool:
        client = request.client.host if request.client else "unknown"
        if self.throttle.blocked(client):
            request.state.login_error = LOCKED_OUT
            return False
        form = await request.form()
        username = str(form.get("username", ""))
        password = str(form.get("password", ""))
        # compare_digest — сравнение за постоянное время, без утечки через тайминги
        ok = secrets.compare_digest(username.encode(), self.username.encode()) & (
            secrets.compare_digest(password.encode(), self.password.encode())
        )
        if ok:
            self.throttle.reset(client)
            request.session.update({"admin": self._session_token})
        else:
            self.throttle.failed(client)
            request.state.login_error = WRONG_CREDENTIALS
        return ok

    @override
    async def logout(self, request: Request) -> bool:
        request.session.clear()
        return True

    @override
    async def authenticate(self, request: Request) -> Response | bool:
        token: object = request.session.get("admin")
        if isinstance(token, str) and secrets.compare_digest(token, self._session_token):
            return True
        if request.headers.get("HX-Request") == "true":
            # Сессия истекла во время HTMX-запроса: вместо страницы входа внутри фрагмента
            # просим HTMX перейти на неё целиком
            return Response(status_code=401, headers={"HX-Redirect": str(request.url_for("admin:login"))})
        return False
