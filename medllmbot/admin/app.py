"""Assembles the FastAPI app with SQLAdmin."""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from markupsafe import Markup
from sqladmin import Admin
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.middleware.base import RequestResponseEndpoint

from medllmbot.admin import i18n
from medllmbot.admin.auth import AdminAuth
from medllmbot.admin.help import FLAG_HELP, GAP_HELP, OUTCOME_HELP, HelpView, help_topic
from medllmbot.admin.live import make_live_view
from medllmbot.admin.reports import make_analytics_view, make_cost_report_view
from medllmbot.admin.views import VIEWS
from medllmbot.analytics import TOPICS
from medllmbot.config import AdminSettings
from medllmbot.costs import format_usd
from medllmbot.db.engine import create_engine, create_sessionmaker
from medllmbot.db.repository import Repository

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
# The admin's own static files (HTMX), no CDN: the admin holds clients' personal data
STATIC_DIR = Path(__file__).parent / "static"


# Headers for every response: the page cannot be embedded in another site (clickjacking), scripts and styles
# are same-origin only. 'unsafe-inline' is needed for inline scripts in SQLAdmin and templates; eval is forbidden
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
# Static files are cached, pages with personal data are not
CACHEABLE_PREFIXES = ("/static/", "/admin/statics/")
BAD_REQUEST_PAGE = Markup(
    "<!doctype html><html lang='{lang}'><meta charset='utf-8'><title>{title}</title>"
    "<p>{text} <a href='/admin/'>{home}</a></p></html>"
)


def bad_request_page() -> str:
    """The 400 page is plain HTML outside the templates, so it is translated here, in the request's language."""
    return BAD_REQUEST_PAGE.format(
        lang=i18n.get_locale(),
        title=i18n.gettext("Bad request"),
        text=i18n.gettext("Malformed address or request parameter."),
        home=i18n.gettext("To the admin home page"),
    )


async def security_headers(request: Request, call_next: RequestResponseEndpoint) -> Response:
    response = await call_next(request)
    response.headers.update(SECURITY_HEADERS)
    if not request.url.path.startswith(CACHEABLE_PREFIXES):
        response.headers["Cache-Control"] = "no-store"
    return response


async def bad_request(request: Request, exc: Exception) -> Response:
    """A malformed id in the URL («/lead/edit/abc») or a number out of SQLite range is a 400, not a 500."""
    log.warning("Bad request %s: %r", request.url.path, exc)
    return HTMLResponse(bad_request_page(), status_code=400)


def create_app(settings: AdminSettings, engine: AsyncEngine | None = None) -> FastAPI:
    engine = engine or create_engine(settings.db_path)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        yield
        await engine.dispose()

    # No public API needed, so auto-docs are disabled
    app = FastAPI(title="MedLLMBot Admin", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    app.middleware("http")(security_headers)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse("/admin/")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> RedirectResponse:
        # The browser requests /favicon.ico on its own, even on pages without <link rel="icon">
        return RedirectResponse("/static/favicon.svg", status_code=301)

    i18n.install()
    admin = Admin(
        app,
        engine,
        title=i18n.lazy_gettext("MedLLMBot admin"),
        favicon_url="/static/favicon.svg",
        authentication_backend=AdminAuth(settings),
        templates_dir=str(TEMPLATES_DIR),
        # RU/RO/EN: the switcher's choice, then the browser language, otherwise English
        i18n_config=i18n.I18N_CONFIG,
    )
    # SQLAdmin has its own nested app with its own 500 handler, so the handler is registered there
    for error in (ValueError, OverflowError):
        admin.admin.add_exception_handler(error, bad_request)
    admin.templates.env.filters["usd"] = format_usd
    # «?» help: the sqladmin/layout.html template finds a section's help via help_topic.
    # Jinja infers the globals type from its default dict, so it is widened explicitly
    template_globals = cast(dict[str, object], admin.templates.env.globals)
    template_globals.update(
        help_topic=help_topic,
        outcome_help=OUTCOME_HELP,
        flag_help=FLAG_HELP,
        gap_help=GAP_HELP,
        analytics_topics=TOPICS,
    )
    for view in VIEWS:
        admin.add_view(view)
    repo = Repository(create_sessionmaker(engine))
    admin.add_view(make_analytics_view(repo))  # goes to the end of the «Clients» category
    admin.add_view(make_cost_report_view(repo))
    admin.add_view(make_live_view(repo))
    admin.add_view(HelpView)

    return app
