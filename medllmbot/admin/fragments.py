"""HTML-фрагменты для HTMX. Строятся на сервере; Markup.format экранирует все подставляемые значения."""

from markupsafe import Markup

from medllmbot.db.models import LeadStatus

# Как часто открытый список спрашивает сервер о новых записях
POLL_EVERY = "15s"

LEAD_NOT_FOUND = Markup('<span class="text-danger">Заявка не найдена — обновите страницу</span>')
BAD_REQUEST = Markup('<span class="text-danger">Некорректный запрос — обновите страницу</span>')


def lead_status_select(url: str, status: LeadStatus, *, saved: bool = False) -> Markup:
    """Статус заявки в списке: выбор сразу сохраняется (hx-post), ответ сервера заменяет список."""
    options = Markup("").join(
        Markup('<option value="{}"{}>{}</option>').format(
            option.name, Markup(" selected") if option is status else "", option.value
        )
        for option in LeadStatus
    )
    css = "form-select form-select-sm w-auto lead-status" + (" is-saved" if saved else "")
    return Markup(
        '<select class="{}" name="status" aria-label="Статус заявки" hx-post="{}" hx-trigger="change" '
        'hx-swap="outerHTML" hx-disabled-elt="this">{}</select>'
    ).format(css, url, options)


def poll_loader(url: str) -> Markup:
    """Пустой блок, который сразу после загрузки страницы запрашивает точку отсчёта для опроса."""
    return Markup('<div class="d-none" hx-get="{}" hx-trigger="load" hx-swap="outerHTML"></div>').format(url)


def new_records_poller(url: str, *, text: str | None = None, warning: bool = False) -> Markup:
    """Блок раз в POLL_EVERY спрашивает сервер о новых записях и заменяет себя ответом.

    url уже содержит точку отсчёта (?after=N). text — плашка о новых записях; None — новых нет.
    """
    if text is None:
        return Markup('<div class="d-none" hx-get="{}" hx-trigger="every {}" hx-swap="outerHTML"></div>').format(
            url, POLL_EVERY
        )
    return Markup(
        '<div class="col-12" hx-get="{}" hx-trigger="every {}" hx-swap="outerHTML">'
        '<div class="alert alert-{} d-flex align-items-center mb-0" role="status">'
        '<span>{}</span><a class="btn btn-sm ms-auto" href="">Показать</a>'
        "</div></div>"
    ).format(url, POLL_EVERY, "warning" if warning else "info", text)


def client_block_loader(url: str) -> Markup:
    """Место под кнопку блокировки: её состояние подгружается после открытия страницы."""
    return Markup('<span class="client-block" hx-get="{}" hx-trigger="load" hx-swap="outerHTML"></span>').format(url)


def client_block_control(url: str, chat_id: int, *, blocked: bool | None) -> Markup:
    """Кнопка «Заблокировать» / «Разблокировать». blocked=None — клиента нет в базе."""
    if blocked is None:
        return Markup('<span class="client-block ms-2 text-secondary">клиент не найден</span>')
    if blocked:
        return Markup(
            '<span class="client-block ms-2"><span class="badge bg-red-lt me-2">Заблокирован</span>'
            '<button type="button" class="btn btn-sm" hx-post="{}" hx-vals=\'{{"blocked": "false"}}\' '
            'hx-target="closest .client-block" hx-swap="outerHTML" hx-disabled-elt="this">Разблокировать</button></span>'
        ).format(url)
    return Markup(
        '<span class="client-block ms-2">'
        '<button type="button" class="btn btn-sm btn-outline-danger" hx-post="{}" hx-vals=\'{{"blocked": "true"}}\' '
        'hx-confirm="Заблокировать клиента {}? Бот перестанет ему отвечать." '
        'hx-target="closest .client-block" hx-swap="outerHTML" hx-disabled-elt="this">Заблокировать клиента</button></span>'
    ).format(url, chat_id)


# ---------- Порядок перетаскиванием ----------

REORDER_SAVED = Markup(
    '<span class="text-success"><i class="fa-solid fa-check me-1"></i>'
    "Порядок сохранён — бот видит его со следующего вопроса.</span>"
)
REORDER_CONFLICT = Markup(
    '<span class="text-danger">Список изменился, пока вы его сортировали: обновите страницу и повторите.</span>'
)


def sort_handle(item_id: int) -> Markup:
    """Ячейка «Порядок»: за значок строку перетаскивают, скрытое поле передаёт id в новом порядке."""
    return Markup(
        '<span class="drag-handle" title="Перетащите, чтобы поменять порядок">'
        '<i class="fa-solid fa-grip-vertical"></i></span>'
        '<input type="hidden" name="order" value="{}">'
    ).format(item_id)
