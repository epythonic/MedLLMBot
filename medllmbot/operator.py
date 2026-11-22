"""Live operator: a client asks for a human, the bot hands the dialog to the administrator in Telegram and goes quiet.

- The client writes «позовите менеджера», «хочу поговорить с человеком» or the /operator command.
- The administrator gets a notification with the latest dialog messages and a «Вернуть бота» button.
- While the operator has the dialog, client messages go to the administrator and Claude is not called.
  When the administrator replies to a bot message, the bot forwards the reply to the client.
- The bot answers on its own again after the «Вернуть бота» button or `bot_config.operator_hours` hours
  after the client's request or the operator's last reply.

This module holds request detection and texts; the Telegram handlers are in `operator_handlers.py`.
"""

import re
from collections.abc import Sequence
from datetime import datetime, timedelta

from anthropic.types import MessageParam

from medllmbot.text import normalize

OPERATOR_COMMAND = "operator"
END_BUTTON = "Вернуть бота"
_END_PREFIX = "operator:end:"  # «Вернуть бота» button data: + the client's chat_id
HISTORY_IN_NOTICE = 6  # latest dialog messages in the notification to the administrator
LINE_PREVIEW = 300  # characters of one history message in the notification

# Whom the client calls: «оператора», «менеджера», «админа», «живого человека»
# «Оператор связи» is a mobile carrier, not a person
_STAFF = r"(?:оператор|менеджер|администратор|админ|человек|сотрудник|консультант)\w*+(?!\s+связи)"
# Whom they want: «хочу менеджера», but not «хочу администратору оставить отзыв»
_STAFF_ACC = r"(?:оператора|менеджера|администратора|админа|человека|сотрудника|консультанта)\b"
_ALIVE = r"(?:(?:живо|настоящ|реальн)\w*\s+)?"
# Between the verb and whom they call: a comma, «пожалуйста»
_GAP = r"(?:[\s,]+(?:пожалуйста|плиз|please|pls))?[\s,]+"
# A request for a human. Not a request: «мобильный оператор», «администратор перезвонит?», «поговорить с врачом»,
# «как связаться с администратором?» (a question about contacts, the bot answers it), «я настоящий человек»
_ASKS = re.compile(
    "|".join(
        (
            rf"(?<!\w)(?:позов\w*|позвать|пригласи\w*|вызов\w*|вызвать|дайте|нужен|нужна){_GAP}{_ALIVE}{_STAFF}",
            rf"(?<!\w)(?:соедин|переключ|перевед|свяж)\w*{_GAP}(?:меня\s+|нас\s+)?(?:с|на)\s+{_ALIVE}{_STAFF}",
            rf"(?<!как )(?<!\w)(?:поговорить|пообщаться|связаться|переписываться|общаться|говорить)\s+с\s+{_ALIVE}{_STAFF}",
            rf"(?<!\w)хочу{_GAP}{_ALIVE}{_STAFF_ACC}",
            rf"(?<!\w)можно{_GAP}{_ALIVE}{_STAFF_ACC}\s*\??$",
            r"(?<!\w)не\s+(?:хочу|буду)\s+(?:\w+\s+)?(?:с\s+)?(?:ботом|роботом)",
            # «Vreau să vorbesc cu un om», «Pot vorbi cu un operator?», «Chemați un operator»
            r"(?<!\w)(?:vreau|pot|as vrea|as dori|doresc)\s+(?:sa\s+)?(?:vorbesc|vorbi|discut|discuta)\s+cu\s+(?:un\s+|o\s+)?"
            r"(?:om|operator|manager|administrator|persoana|consultant)",
            r"(?<!\w)(?:chemati|cheama|vreau|doresc)\s+(?:un\s+|o\s+)?(?:operator|manager|administrator)\w*",
            r"(?<!\w)nu\s+vreau\s+(?:sa\s+)?(?:vorbesc|discut)\s+cu\s+(?:un\s+)?(?:robot|bot)",
            r"(?<!\w)operator uman",
            r"(?<!\w)(?:talk|speak|chat)\s+(?:to|with)\s+(?:a\s+|an\s+|the\s+)?"
            r"(?:human|person|manager|operator|real person|agent|someone)",
            r"(?<!\w)(?:i need|i want|give me|get me|connect me (?:to|with))\s+(?:a\s+|an\s+)?"
            r"(?:human|real person|manager|operator|agent)",
        )
    )
)


def asks_for_operator(text: str) -> bool:
    """The client asks for a live human instead of the bot."""
    return _ASKS.search(normalize(text)) is not None


def operator_deadline(now: datetime, hours: int) -> datetime:
    """Until when the bot stays silent: every operator reply extends the deadline."""
    return now + timedelta(hours=hours)


def with_operator(until: datetime | None, now: datetime) -> bool:
    """The operator has the dialog: the deadline is set and has not passed yet."""
    return until is not None and until > now


def end_button_data(chat_id: int) -> str:
    return f"{_END_PREFIX}{chat_id}"


def parse_end_button(data: str | None) -> int | None:
    """The client's chat_id from the «Вернуть бота» button; None if it is another button or the data is corrupt."""
    if data is None or not data.startswith(_END_PREFIX):
        return None
    try:
        return int(data.removeprefix(_END_PREFIX))
    except ValueError:
        return None


def is_end_button(data: str | None) -> bool:
    return data is not None and data.startswith(_END_PREFIX)


def _short(text: str, limit: int = LINE_PREVIEW) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _content(message: MessageParam) -> str:
    """Text of a history message. The repository stores history as strings; there are no API blocks here."""
    content = message["content"]
    return content if isinstance(content, str) else ""


def client_label(name: str, chat_id: int) -> str:
    """Who is writing, in every message to the administrator: «@ivan (chat 42)»."""
    return f"{name} (chat {chat_id})"


def handoff_notice(client: str, request: str, history: Sequence[MessageParam], hours: int) -> str:
    """First notification: the client asks for a human. `client` comes from `client_label`."""
    lines = [f"Клиент {client} просит оператора:", _short(request, 1000)]
    if history:
        lines += ["", "Последние сообщения:"]
        lines += [f"{'Клиент' if message['role'] == 'user' else 'Бот'}: {_short(_content(message))}" for message in history]
    lines += [
        "",
        "Ответьте на это сообщение — ответ уйдёт клиенту. Бот в этом чате молчит, пока вы не нажмёте "
        f"«{END_BUTTON}» или пока не пройдёт {hours} ч без ваших ответов.",
    ]
    return "\n".join(lines)


def client_message_notice(client: str, text: str) -> str:
    """A client message while the operator has the dialog."""
    return f"{client}:\n{text}"


# Replies to the administrator
DELIVERY_FORBIDDEN = "Не удалось отправить клиенту: он заблокировал бота или удалил чат."
DELIVERY_FAILED = "Не удалось отправить клиенту: Telegram вернул ошибку. Попробуйте ещё раз или другим сообщением."
ENDED = "Бот снова отвечает клиенту."
CLIENT_LEFT = "клиент нажал /start и вернулся к боту."
ALREADY_ENDED = "Клиент уже с ботом."
NOT_ALLOWED = "Это может только администратор."
