"""Live operator: a client asks for a human, the bot hands the dialog to the administrator in Telegram and goes quiet.

- The client writes «позовите менеджера», «хочу поговорить с человеком» or the /operator command.
- Every operator (the owner from ADMIN_ID and the Telegram staff, `medllmbot/team.py`) gets a notification
  with the latest dialog messages and a “Back to bot” button.
- While an operator has the dialog, client messages go to the operators and Claude is not called.
  When an operator replies to a bot message, the bot forwards the reply to the client; the first reply takes
  the dialog, and from then on only that operator gets the client's messages.
- The bot answers on its own again after the “Back to bot” button or `bot_config.operator_hours` hours
  after the client's request or the operator's last reply.

This module holds request detection and texts; the Telegram handlers are in `operator_handlers.py`.
Texts for the administrator are msgids, translated into `bot_config.admin_language`.
"""

import re
from collections.abc import Sequence
from datetime import datetime, timedelta

from anthropic.types import MessageParam

from medllmbot.i18n import N_, Gettext
from medllmbot.text import normalize

OPERATOR_COMMAND = "operator"
END_BUTTON = N_("Back to bot")
_END_PREFIX = "operator:end:"  # the button's callback data: + the client's chat_id
HISTORY_IN_NOTICE = 6  # latest dialog messages in the notification to the administrator
LINE_PREVIEW = 300  # characters of one history message in the notification
HINT_CLIENTS = 5  # waiting clients listed in the hint to the administrator, each with its own button
BUTTON_NAME = 32  # characters of a client name on a button

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


def handoff_notice(client: str, request: str, history: Sequence[MessageParam], hours: int, _: Gettext) -> str:
    """First notification: the client asks for a human. `client` comes from `client_label`, `_` translates
    into the administrator's language."""
    lines = [_("Client %(client)s asks for an operator:") % {"client": client}, _short(request, 1000)]
    if history:
        client_role, bot_role = _("Client"), _("Bot")
        lines += ["", _("Latest messages:")]
        lines += [
            f"{client_role if message['role'] == 'user' else bot_role}: {_short(_content(message))}" for message in history
        ]
    reply_hint = _(
        "Reply to this message and your reply goes to the client. The bot stays silent in this chat until you press "
        "“%(button)s” or %(hours)s h pass without your replies."
    )
    lines += ["", reply_hint % {"button": _(END_BUTTON), "hours": hours}]
    return "\n".join(lines)


def client_message_notice(client: str, text: str) -> str:
    """A client message while the operator has the dialog."""
    return f"{client}:\n{text}"


def operator_hint(waiting: Sequence[str], _: Gettext) -> str:
    """For the administrator who wrote to the bot without a reply while clients wait for the operator.

    `waiting` holds `client_label` lines, the latest handed over first; the first HINT_CLIENTS are listed.
    """
    lines = [_("This message was not sent to anyone. Waiting for the operator:"), *waiting[:HINT_CLIENTS]]
    if len(waiting) > HINT_CLIENTS:
        lines.append(f"+{len(waiting) - HINT_CLIENTS}")
    how = _(
        "To answer a client, reply to my message about them: swipe it to the left or choose “Reply” in its menu. "
        "“%(button)s” hands the client back to the bot; while clients wait, the bot does not answer your own messages."
    )
    lines += ["", how % {"button": _(END_BUTTON)}]
    return "\n".join(lines)


def end_button_text(client: str, _: Gettext) -> str:
    """The “Back to bot” button for one of several clients: with the client's name."""
    return f"{_(END_BUTTON)}: {_short(client, BUTTON_NAME)}"


# Replies to the administrator (msgids, translated into the administrator's language)
DELIVERY_FORBIDDEN = N_("Could not send to the client: they blocked the bot or deleted the chat.")
DELIVERY_FAILED = N_("Could not send to the client: Telegram returned an error. Try again or send another message.")
ENDED = N_("The bot is answering the client again.")
CLIENT_LEFT = N_("the client pressed /start and went back to the bot.")
ALREADY_ENDED = N_("The client is already with the bot.")
# Several operators: the first reply takes the dialog
TAKEN = N_("Client %(client)s is handled by %(name)s.")
BUSY = N_(
    "Not sent: client %(client)s is handled by %(name)s. To take the dialog, press “%(button)s” under a message "
    "about this client, then reply again."
)
NOT_ALLOWED = N_("Only the administrator can do this.")
