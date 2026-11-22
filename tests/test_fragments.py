"""HTML fragments for HTMX: states, hx-* attributes and escaping."""

import re

from medllmbot.admin.fragments import (
    POLL_EVERY,
    client_block_control,
    client_block_loader,
    lead_status_select,
    new_records_poller,
    poll_loader,
    sort_handle,
)
from medllmbot.db.models import LeadStatus

URL = "/admin/htmx/x"


class TestLeadStatusSelect:
    def test_selected_status_and_russian_labels(self) -> None:
        html = str(lead_status_select(URL, LeadStatus.IN_PROGRESS))

        assert '<option value="IN_PROGRESS" selected>В работе</option>' in html
        assert '<option value="NEW">Новая</option>' in html
        assert html.count(" selected") == 1
        assert f'hx-post="{URL}"' in html
        assert 'hx-trigger="change"' in html
        assert "is-saved" not in html

    def test_saved_state_is_highlighted(self) -> None:
        assert "lead-status is-saved" in str(lead_status_select(URL, LeadStatus.DONE, saved=True))

    def test_url_is_escaped(self) -> None:
        html = str(lead_status_select('/x?a=1&b="2"', LeadStatus.NEW))
        assert 'hx-post="/x?a=1&amp;b=&#34;2&#34;"' in html


class TestPoller:
    def test_loader_asks_for_baseline_once(self) -> None:
        html = str(poll_loader(URL))
        assert 'hx-trigger="load"' in html
        assert 'class="d-none"' in html

    def test_without_news_is_hidden_but_keeps_polling(self) -> None:
        html = str(new_records_poller(f"{URL}?after=5"))
        assert 'class="d-none"' in html
        assert f'hx-trigger="every {POLL_EVERY}"' in html
        assert 'hx-get="/admin/htmx/x?after=5"' in html

    def test_banner(self) -> None:
        html = str(new_records_poller(URL, text="Новых заявок: 2"))
        assert "alert-info" in html
        assert "Новых заявок: 2" in html
        assert 'href=""' in html  # «Показать» reloads the current list with its filters

    def test_warning_banner(self) -> None:
        assert "alert-warning" in str(new_records_poller(URL, text="…", warning=True))

    def test_text_is_escaped(self) -> None:
        html = str(new_records_poller(URL, text="<script>alert(1)</script>"))
        assert "<script>" not in html
        assert "&lt;script&gt;" in html


class TestClientBlockControl:
    def test_loader(self) -> None:
        html = str(client_block_loader(URL))
        assert 'hx-trigger="load"' in html
        assert f'hx-get="{URL}"' in html

    def test_block_button_asks_for_confirmation(self) -> None:
        html = str(client_block_control(URL, 42, blocked=False))
        assert "Заблокировать клиента" in html
        assert 'hx-confirm="Заблокировать клиента 42? Бот перестанет ему отвечать."' in html
        assert """hx-vals='{"blocked": "true"}'""" in html
        assert f'hx-post="{URL}"' in html

    def test_unblock_button(self) -> None:
        html = str(client_block_control(URL, 42, blocked=True))
        assert "Заблокирован" in html
        assert "Разблокировать" in html
        assert """hx-vals='{"blocked": "false"}'""" in html
        assert "hx-confirm" not in html

    def test_unknown_client(self) -> None:
        html = str(client_block_control(URL, 42, blocked=None))
        assert "клиент не найден" in html
        assert not re.search(r"hx-(post|get)", html)


def test_sort_handle_carries_id() -> None:
    html = str(sort_handle(7))
    assert 'class="drag-handle"' in html
    assert '<input type="hidden" name="order" value="7">' in html
