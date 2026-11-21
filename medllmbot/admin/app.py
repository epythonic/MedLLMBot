"""Сборка FastAPI-приложения с SQLAdmin."""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqladmin import Admin
from sqladmin.i18n import I18nConfig
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.middleware.base import RequestResponseEndpoint

from medllmbot.admin.auth import AdminAuth
from medllmbot.admin.forms import LOCALE
from medllmbot.admin.help import FLAG_HELP, OUTCOME_HELP, HelpView, help_topic
from medllmbot.admin.live import make_live_view
from medllmbot.admin.reports import make_cost_report_view
from medllmbot.admin.views import VIEWS
from medllmbot.config import AdminSettings
from medllmbot.costs import format_usd
from medllmbot.db.engine import create_engine, create_sessionmaker
from medllmbot.db.repository import Repository

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
# Свои статические файлы админки (HTMX) — без CDN: в админке персональные данные клиентов
STATIC_DIR = Path(__file__).parent / "static"


# Заголовки для каждого ответа: страницу нельзя встроить в чужой сайт (clickjacking), скрипты и стили —
# только свои. 'unsafe-inline' нужен встроенным скриптам SQLAdmin и шаблонов; eval запрещён
SECURITY_HEADERS: dict[str, str] = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; font-src 'self' data:; connect-src 'self'; "
        "frame-ancestors 'none'; form-action 'self'; base-uri 'self'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}
# Статика кэшируется, а страницы с персональными данными — нет
CACHEABLE_PREFIXES = ("/static/", "/admin/statics/")
BAD_REQUEST_PAGE = (
    "<!doctype html><html lang='ru'><meta charset='utf-8'><title>Некорректный запрос</title>"
    "<p>Некорректный адрес или параметр запроса. <a href='/admin/'>На главную админки</a></p></html>"
)


async def security_headers(request: Request, call_next: RequestResponseEndpoint) -> Response:
    response = await call_next(request)
    response.headers.update(SECURITY_HEADERS)
    if not request.url.path.startswith(CACHEABLE_PREFIXES):
        response.headers["Cache-Control"] = "no-store"
    return response


async def bad_request(request: Request, exc: Exception) -> Response:
    """Кривой id в адресе («/lead/edit/abc») или число вне диапазона SQLite — это 400, а не 500."""
    log.warning("Некорректный запрос %s: %r", request.url.path, exc)
    return HTMLResponse(BAD_REQUEST_PAGE, status_code=400)


def create_app(settings: AdminSettings, engine: AsyncEngine | None = None) -> FastAPI:
    engine = engine or create_engine(settings.db_path)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        yield
        await engine.dispose()

    # Публичное API не нужно — отключаем автодокументацию
    app = FastAPI(title="MedLLMBot Admin", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    app.middleware("http")(security_headers)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse("/admin/")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> RedirectResponse:
        # Браузер запрашивает /favicon.ico сам, даже на страницах без <link rel="icon">
        return RedirectResponse("/static/favicon.svg", status_code=301)

    admin = Admin(
        app,
        engine,
        title="MedLLMBot — админка",
        favicon_url="/static/favicon.svg",
        authentication_backend=AdminAuth(settings),
        templates_dir=str(TEMPLATES_DIR),
        # Интерфейс всегда на русском, как и справка: язык браузера не учитываем
        i18n_config=I18nConfig(default_locale=LOCALE, language_cookie_name=None, language_header_name=None),
    )
    # У SQLAdmin своё вложенное приложение со своим обработчиком 500 — регистрируем обработчик в нём
    for error in (ValueError, OverflowError):
        admin.admin.add_exception_handler(error, bad_request)
    admin.templates.env.filters["usd"] = format_usd
    # Справка «?»: шаблон sqladmin/layout.html находит справку раздела через help_topic.
    # Тип globals Jinja выводит из своего словаря по умолчанию, поэтому расширяем его явно
    template_globals = cast(dict[str, object], admin.templates.env.globals)
    template_globals.update(help_topic=help_topic, outcome_help=OUTCOME_HELP, flag_help=FLAG_HELP)
    for view in VIEWS:
        admin.add_view(view)
    repo = Repository(create_sessionmaker(engine))
    admin.add_view(make_cost_report_view(repo))
    admin.add_view(make_live_view(repo))
    admin.add_view(HelpView)

    return app
