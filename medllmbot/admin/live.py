"""Quick admin actions on HTMX.

- A lead's status changes right in the leads list.
- Open leads and audit log lists check for new records every 15 seconds and show a badge.
- A client can be blocked from an audit log record page.
- The order of services, FAQ items and company info sections changes by dragging rows.

Routes return HTML fragments (fragments.py) in the request's language; all logic stays on the server.
SQLAdmin checks login (expose → login_required). Modifying requests are accepted only
with the HX-Request header: another site cannot send it without CORS permission.
"""

from collections.abc import Callable
from typing import final, override

from markupsafe import Markup
from sqladmin import BaseView, expose
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

from medllmbot.admin.fragments import (
    bad_request,
    client_block_control,
    client_block_loader,
    lead_not_found,
    lead_status_select,
    new_records_poller,
    reorder_conflict,
    reorder_saved,
    sort_handle,
)
from medllmbot.admin.i18n import gettext, lazy_gettext
from medllmbot.db.models import AuditEvent, FaqItem, InfoSection, Lead, LeadStatus, Service
from medllmbot.db.repository import Repository

# Route identities: templates and formatters build the URL via url_for("admin:view-<identity>")
LEAD_STATUS = "lead-status"
NEW_LEADS = "new-leads"
NEW_AUDIT_EVENTS = "new-audit-events"
CLIENT_BLOCK_STATE = "client-block-state"
CLIENT_BLOCK = "client-block"
REORDER = "reorder"


def route_name(identity: str) -> str:
    return f"admin:view-{identity}"


def _route[H](path: str, *, identity: str, methods: list[str] | None = None) -> Callable[[H], H]:
    # SQLAdmin's expose is untyped (returns Any), so the type is narrowed once here
    decorator: Callable[[H], H] = expose(path, methods=methods, identity=identity)
    return decorator


def is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def _fragment(html: Markup, status_code: int = 200) -> Response:
    return HTMLResponse(str(html), status_code=status_code)


# SQLite stores nothing larger: such an id surely does not exist, and a query with it would fail with OverflowError
MAX_ID = 2**63 - 1


def _path_int(request: Request, name: str) -> int | None:
    value: object = request.path_params.get(name)
    return value if isinstance(value, int) and value <= MAX_ID else None


def _query_int(request: Request, name: str) -> int | None:
    value = request.query_params.get(name, "")
    return int(value) if value.isdecimal() and int(value) <= MAX_ID else None


def _poll_url(request: Request, after_id: int) -> str:
    return str(request.url.include_query_params(after=after_id))


# ---------- SQLAdmin column formatters ----------


def lead_status_cell(model: Lead, _attr: object, request: Request) -> Markup:
    """Status column in the leads list: a dropdown that saves immediately."""
    return lead_status_select(str(request.url_for(route_name(LEAD_STATUS), lead_id=model.id)), model.status)


def sort_handle_cell(model: Service | FaqItem | InfoSection, _attr: object) -> Markup:
    """Sort column in the knowledge base: the icon used to drag the row."""
    return sort_handle(model.id)


def audit_chat_id_cell(model: AuditEvent, _attr: object, request: Request) -> Markup:
    """Chat ID on an audit log record page, with a button to block the client."""
    url = str(request.url_for(route_name(CLIENT_BLOCK_STATE), chat_id=model.chat_id))
    return Markup("{} {}").format(model.chat_id, client_block_loader(url))


# ---------- Fragment routes ----------


def make_live_view(repo: Repository) -> type[BaseView]:
    """SQLAdmin creates views without arguments, so the repository is passed through a closure."""

    def block_control(request: Request, chat_id: int, blocked: bool | None) -> Response:
        url = str(request.url_for(route_name(CLIENT_BLOCK), chat_id=chat_id))
        return _fragment(client_block_control(url, chat_id, blocked=blocked))

    @final
    class LiveView(BaseView):
        name = lazy_gettext("Quick actions")
        identity = "live"

        @override
        def is_visible(self, request: Request) -> bool:
            # Only routes for HTMX fragments; not shown in the menu
            return False

        @_route("/htmx/lead/{lead_id:int}/status", methods=["POST"], identity=LEAD_STATUS)
        async def lead_status(self, request: Request) -> Response:
            lead_id = _path_int(request, "lead_id")
            raw = (await request.form()).get("status")
            status = LeadStatus.__members__.get(raw) if isinstance(raw, str) else None
            if not is_htmx(request) or lead_id is None or status is None:
                return _fragment(bad_request(), 400)
            lead = await repo.set_lead_status(lead_id, status)
            if lead is None:
                return _fragment(lead_not_found(), 404)
            return _fragment(lead_status_select(str(request.url), lead.status, saved=True))

        @_route("/htmx/new-leads", identity=NEW_LEADS)
        async def new_leads(self, request: Request) -> Response:
            after = _query_int(request, "after")
            records = await repo.new_leads(after or 0)
            if after is None:  # first request from the page: remember the baseline
                return _fragment(new_records_poller(_poll_url(request, records.last_id)))
            text = gettext("New leads: %(count)s") % {"count": records.count} if records.count else None
            return _fragment(new_records_poller(_poll_url(request, after), text=text))

        @_route("/htmx/new-audit-events", identity=NEW_AUDIT_EVENTS)
        async def new_audit_events(self, request: Request) -> Response:
            after = _query_int(request, "after")
            records = await repo.new_audit_events(after or 0)
            if after is None:
                return _fragment(new_records_poller(_poll_url(request, records.last_id)))
            text = None
            if records.count and records.suspicious:
                text = gettext("New log events: %(count)s, suspicious among them: %(suspicious)s") % {
                    "count": records.count,
                    "suspicious": records.suspicious,
                }
            elif records.count:
                text = gettext("New log events: %(count)s") % {"count": records.count}
            return _fragment(new_records_poller(_poll_url(request, after), text=text, warning=records.suspicious > 0))

        @_route("/htmx/client/{chat_id:int}/block-state", identity=CLIENT_BLOCK_STATE)
        async def client_block_state(self, request: Request) -> Response:
            chat_id = _path_int(request, "chat_id")
            if chat_id is None:
                return _fragment(bad_request(), 400)
            client = await repo.get_client(chat_id)
            return block_control(request, chat_id, client.is_blocked if client else None)

        @_route("/htmx/client/{chat_id:int}/block", methods=["POST"], identity=CLIENT_BLOCK)
        async def client_block(self, request: Request) -> Response:
            chat_id = _path_int(request, "chat_id")
            raw = (await request.form()).get("blocked")
            if not is_htmx(request) or chat_id is None or raw not in ("true", "false"):
                return _fragment(bad_request(), 400)
            client = await repo.set_client_blocked(chat_id, raw == "true")
            return block_control(request, chat_id, client.is_blocked if client else None)

        @_route("/htmx/reorder/{section}", methods=["POST"], identity=REORDER)
        async def reorder(self, request: Request) -> Response:
            raw = (await request.form()).getlist("order")
            ids = [int(value) for value in raw if isinstance(value, str) and value.isdecimal()]
            if not is_htmx(request) or not ids or len(ids) != len(raw):
                return _fragment(bad_request(), 400)
            match request.path_params.get("section"):
                case "service":
                    saved = await repo.reorder(Service, ids)
                case "faq-item":
                    saved = await repo.reorder(FaqItem, ids)
                case "info-section":
                    saved = await repo.reorder(InfoSection, ids)
                case _:
                    return _fragment(bad_request(), 404)
            return _fragment(reorder_saved()) if saved else _fragment(reorder_conflict(), 409)

    return LiveView
