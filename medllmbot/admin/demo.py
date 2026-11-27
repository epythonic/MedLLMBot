"""«Demo data» page: refresh the demo or switch it between the dental clinic and the electronics shop.

The same reset as `seed --reset --profile …` on the server, without SSH: clients, chats, leads, orders and the log
are deleted; the knowledge base, model prices, bot settings and the fictional clients (dated from now) are restored.
Admin accounts, spending totals and the daily limit stay.

The page exists only with ADMIN_DEMO_TOOLS=true (the demo server): in a real business's admin panel one click would
delete its clients and leads. The reset is a POST with the HX-Request header, after a confirmation in the browser.
"""

import logging
from dataclasses import dataclass
from typing import final

from sqladmin import BaseView
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

from medllmbot.admin.fragments import bad_request
from medllmbot.admin.i18n import gettext, lazy_gettext
from medllmbot.admin.live import is_htmx, route, route_name
from medllmbot.db.models import BusinessProfile
from medllmbot.db.repository import Repository
from medllmbot.db.seed import PROFILE_OPTIONS, restore_demo

log = logging.getLogger(__name__)

DEMO = "demo"
DEMO_RESET = "demo-reset"
DEMO_ICON = "fa-solid fa-wand-magic-sparkles"


@dataclass(frozen=True)
class DemoAction:
    """A button on the page: refresh the current profile or switch to another one."""

    profile: str  # the --profile option value: clinic, shop
    label: str
    confirm: str
    icon: str
    current: bool


def demo_actions(current: BusinessProfile) -> list[DemoAction]:
    """The current profile's «Refresh» first, then a switch to each other profile, in the request's language."""
    actions: list[DemoAction] = []
    for option, kind in sorted(PROFILE_OPTIONS.items(), key=lambda item: item[1] is not current):
        name = gettext(kind.value)
        if kind is current:
            label = gettext("Refresh the demo")
            question = gettext("Delete clients, leads and the log and restore the «%(profile)s» demo?")
            icon = "fa-solid fa-rotate"
        else:
            label = gettext("Switch to «%(profile)s»") % {"profile": name}
            question = gettext("Delete clients, leads and the log and switch the demo to «%(profile)s»?")
            icon = "fa-solid fa-shuffle"
        actions.append(DemoAction(option, label, question % {"profile": name}, icon, current=kind is current))
    return actions


def make_demo_view(repo: Repository) -> type[BaseView]:
    """The repository is passed through a closure: SQLAdmin creates views without arguments."""

    @final
    class DemoView(BaseView):
        name = lazy_gettext("Demo data")
        icon = DEMO_ICON
        identity = DEMO

        @route(f"/{DEMO}", identity=DEMO)
        async def page(self, request: Request) -> Response:
            config = await repo.get_config()
            restored = PROFILE_OPTIONS.get(request.query_params.get("restored", ""))
            return await self.templates.TemplateResponse(  # pyright: ignore[reportUnknownMemberType]
                request,
                "demo.html",
                {
                    "title": gettext("Demo data"),
                    "subtitle": gettext("refresh or switch the business"),
                    "profile": config.profile,
                    "counts": await repo.activity_counts(),
                    "actions": demo_actions(config.profile),
                    "reset_url": str(request.url_for(route_name(DEMO_RESET))),
                    "restored": restored,
                    "help_identity": DEMO,
                },
            )

        @route(f"/{DEMO}/reset", identity=DEMO_RESET, methods=["POST"])
        async def reset(self, request: Request) -> Response:
            form = await request.form()
            kind = PROFILE_OPTIONS.get(str(form.get("profile", "")))
            if not is_htmx(request) or kind is None:
                return HTMLResponse(str(bad_request()), status_code=400)
            await restore_demo(repo.sessions, kind)
            log.info("Demo restored from the admin panel: %s", kind.name.lower())
            # A whole new page: the menu, counts and buttons all change; the notice says what happened
            page = request.url_for(route_name(DEMO)).include_query_params(restored=kind.name.lower())
            return HTMLResponse("", headers={"HX-Redirect": str(page)})

    return DemoView
