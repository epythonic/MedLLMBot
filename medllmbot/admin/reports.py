"""Страница «Расходы → Отчёт по месяцам»: сколько стоил бот и прогноз на текущий месяц."""

from datetime import UTC, datetime
from typing import final

from sqladmin import BaseView, expose
from starlette.requests import Request
from starlette.responses import Response

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
