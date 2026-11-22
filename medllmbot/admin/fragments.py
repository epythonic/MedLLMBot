"""HTML fragments for HTMX. Built on the server; Markup.format escapes every interpolated value.

Texts are translated at call time, in the language of the current request, and passed to Markup.format
as arguments: a translation is not trusted HTML, so it is escaped like any other value.
"""

from markupsafe import Markup

from medllmbot.admin.i18n import gettext
from medllmbot.db.models import LeadStatus

# How often an open list asks the server for new records
POLL_EVERY = "15s"


def _danger(text: str) -> Markup:
    return Markup('<span class="text-danger">{}</span>').format(text)


def lead_not_found() -> Markup:
    return _danger(gettext("Lead not found — refresh the page"))


def bad_request() -> Markup:
    return _danger(gettext("Invalid request — refresh the page"))


def lead_status_select(url: str, status: LeadStatus, *, saved: bool = False) -> Markup:
    """Lead status in the list: the choice is saved at once (hx-post), the server response replaces the list."""
    options = Markup("").join(
        Markup('<option value="{}"{}>{}</option>').format(
            option.name, Markup(" selected") if option is status else "", gettext(option.value)
        )
        for option in LeadStatus
    )
    css = "form-select form-select-sm w-auto lead-status" + (" is-saved" if saved else "")
    return Markup(
        '<select class="{}" name="status" aria-label="{}" hx-post="{}" hx-trigger="change" '
        'hx-swap="outerHTML" hx-disabled-elt="this">{}</select>'
    ).format(css, gettext("Lead status"), url, options)


def poll_loader(url: str) -> Markup:
    """Empty block that requests the polling baseline right after the page loads."""
    return Markup('<div class="d-none" hx-get="{}" hx-trigger="load" hx-swap="outerHTML"></div>').format(url)


def new_records_poller(url: str, *, text: str | None = None, warning: bool = False) -> Markup:
    """Block that asks the server for new records every POLL_EVERY and replaces itself with the response.

    url already contains the baseline (?after=N). text is the new-records badge; None means nothing new.
    """
    if text is None:
        return Markup('<div class="d-none" hx-get="{}" hx-trigger="every {}" hx-swap="outerHTML"></div>').format(
            url, POLL_EVERY
        )
    return Markup(
        '<div class="col-12" hx-get="{}" hx-trigger="every {}" hx-swap="outerHTML">'
        '<div class="alert alert-{} d-flex align-items-center mb-0" role="status">'
        '<span>{}</span><a class="btn btn-sm ms-auto" href="">{}</a>'
        "</div></div>"
    ).format(url, POLL_EVERY, "warning" if warning else "info", text, gettext("Show"))


def client_block_loader(url: str) -> Markup:
    """Placeholder for the block button: its state is loaded after the page opens."""
    return Markup('<span class="client-block" hx-get="{}" hx-trigger="load" hx-swap="outerHTML"></span>').format(url)


def client_block_control(url: str, chat_id: int, *, blocked: bool | None) -> Markup:
    """«Block client» / «Unblock» button. blocked=None means the client is not in the DB."""
    if blocked is None:
        return Markup('<span class="client-block ms-2 text-secondary">{}</span>').format(gettext("client not found"))
    if blocked:
        return Markup(
            '<span class="client-block ms-2"><span class="badge bg-red-lt me-2">{}</span>'
            '<button type="button" class="btn btn-sm" hx-post="{}" hx-vals=\'{{"blocked": "false"}}\' '
            'hx-target="closest .client-block" hx-swap="outerHTML" hx-disabled-elt="this">{}</button></span>'
        ).format(gettext("Blocked"), url, gettext("Unblock"))
    confirm = gettext("Block client %(chat_id)s? The bot will stop replying to them.") % {"chat_id": chat_id}
    return Markup(
        '<span class="client-block ms-2">'
        '<button type="button" class="btn btn-sm btn-outline-danger" hx-post="{}" hx-vals=\'{{"blocked": "true"}}\' '
        'hx-confirm="{}" '
        'hx-target="closest .client-block" hx-swap="outerHTML" hx-disabled-elt="this">{}</button></span>'
    ).format(url, confirm, gettext("Block client"))


# ---------- Drag-and-drop ordering ----------


def reorder_saved() -> Markup:
    return Markup('<span class="text-success"><i class="fa-solid fa-check me-1"></i>{}</span>').format(
        gettext("Order saved — the bot uses it from the next question.")
    )


def reorder_conflict() -> Markup:
    return _danger(gettext("The list changed while you were sorting it: refresh the page and try again."))


def sort_handle(item_id: int) -> Markup:
    """Sort column cell: the row is dragged by the icon, a hidden field sends the id in the new order."""
    return Markup(
        '<span class="drag-handle" title="{}">'
        '<i class="fa-solid fa-grip-vertical"></i></span>'
        '<input type="hidden" name="order" value="{}">'
    ).format(gettext("Drag to change the order"), item_id)
