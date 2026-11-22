"""Custom admin pages without a model: Clients → Analytics and Costs → Monthly report."""

from datetime import UTC, datetime
from typing import final

from sqladmin import BaseView, expose
from starlette.requests import Request
from starlette.responses import Response

from medllmbot.admin.i18n import gettext, lazy_gettext, ngettext
from medllmbot.admin.views import CLIENTS, COSTS
from medllmbot.analytics import PERIODS, collect, parse_period
from medllmbot.costs import build_report
from medllmbot.db.repository import Repository


def make_cost_report_view(repo: Repository) -> type[BaseView]:
    """SQLAdmin creates views without arguments, so the repository
    is passed through a closure: each app has its own DB.
    """

    @final
    class CostReportView(BaseView):
        name = lazy_gettext("Monthly report")
        icon = "fa-solid fa-chart-column"
        category = COSTS
        identity = "costs"

        # In SQLAdmin the expose decorator and TemplateResponse are typed with Any and a bare dict;
        # this can only be fixed in the library
        @expose("/costs", methods=["GET"], identity="costs")  # pyright: ignore[reportAny]
        async def report(self, request: Request) -> Response:
            today = datetime.now(UTC).date()
            months = build_report(await repo.usage_by_month(), today)
            return await self.templates.TemplateResponse(  # pyright: ignore[reportUnknownMemberType]
                request,
                "costs.html",
                {
                    "title": gettext("Claude API costs"),
                    "subtitle": gettext("by month, UTC"),
                    "months": months,
                    "help_identity": "costs",
                },
            )

    return CostReportView


def make_analytics_view(repo: Repository) -> type[BaseView]:
    """Question analytics: topics, share of dialogs with a lead, unanswered questions. Repository via closure."""

    @final
    class AnalyticsView(BaseView):
        name = lazy_gettext("Analytics")
        icon = "fa-solid fa-chart-pie"
        category = CLIENTS
        identity = "analytics"

        @expose("/analytics", methods=["GET"], identity="analytics")  # pyright: ignore[reportAny]
        async def report(self, request: Request) -> Response:
            days = parse_period(request.query_params.get("days"))
            config = await repo.get_config()
            # Audit log times are UTC without a time zone
            report = await collect(repo, config.profile, days, datetime.now(UTC).replace(tzinfo=None))
            return await self.templates.TemplateResponse(  # pyright: ignore[reportUnknownMemberType]
                request,
                "analytics.html",
                {
                    "title": gettext("Question analytics"),
                    "subtitle": ngettext("over the last %(days)s day, UTC", "over the last %(days)s days, UTC", days)
                    % {"days": days},
                    "report": report,
                    "days": days,
                    "periods": PERIODS,
                    "profile": config.profile,
                    "retention_days": config.audit_retention_days,
                    "help_identity": "analytics",
                },
            )

    return AnalyticsView
