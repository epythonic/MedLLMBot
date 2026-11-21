"""Свои страницы админки без модели: «Клиенты → Аналитика» и «Расходы → Отчёт по месяцам»."""

from datetime import UTC, datetime
from typing import final

from sqladmin import BaseView, expose
from starlette.requests import Request
from starlette.responses import Response

from medllmbot.admin.views import CLIENTS
from medllmbot.analytics import PERIODS, collect, parse_period
from medllmbot.costs import build_report
from medllmbot.db.repository import Repository


def make_cost_report_view(repo: Repository) -> type[BaseView]:
    """SQLAdmin создаёт представления без аргументов, поэтому репозиторий
    передаём через замыкание — у каждого приложения своя БД.
    """

    @final
    class CostReportView(BaseView):
        name = "Отчёт по месяцам"
        icon = "fa-solid fa-chart-column"
        category = "Расходы"
        identity = "costs"

        # В SQLAdmin декоратор expose и TemplateResponse типизированы через Any и голый dict —
        # исправить это можно только в библиотеке
        @expose("/costs", methods=["GET"], identity="costs")  # pyright: ignore[reportAny]
        async def report(self, request: Request) -> Response:
            today = datetime.now(UTC).date()
            months = build_report(await repo.usage_by_month(), today)
            return await self.templates.TemplateResponse(  # pyright: ignore[reportUnknownMemberType]
                request,
                "costs.html",
                {"title": "Расходы на Claude API", "subtitle": "по месяцам, UTC", "months": months, "help_identity": "costs"},
            )

    return CostReportView


def make_analytics_view(repo: Repository) -> type[BaseView]:
    """Аналитика вопросов: темы, доля диалогов с заявкой, вопросы без ответа. Репозиторий — через замыкание."""

    @final
    class AnalyticsView(BaseView):
        name = "Аналитика"
        icon = "fa-solid fa-chart-pie"
        category = CLIENTS
        identity = "analytics"

        @expose("/analytics", methods=["GET"], identity="analytics")  # pyright: ignore[reportAny]
        async def report(self, request: Request) -> Response:
            days = parse_period(request.query_params.get("days"))
            config = await repo.get_config()
            # Время в журнале — UTC без часового пояса
            report = await collect(repo, config.profile, days, datetime.now(UTC).replace(tzinfo=None))
            return await self.templates.TemplateResponse(  # pyright: ignore[reportUnknownMemberType]
                request,
                "analytics.html",
                {
                    "title": "Аналитика вопросов",
                    "subtitle": f"за последние {days} дней, UTC",
                    "report": report,
                    "days": days,
                    "periods": PERIODS,
                    "profile": config.profile,
                    "retention_days": config.audit_retention_days,
                    "help_identity": "analytics",
                },
            )

    return AnalyticsView
