"""Живой оператор: клиент просит человека — бот передаёт диалог администратору в Telegram и молчит.

- Клиент пишет «позовите менеджера», «хочу поговорить с человеком» или команду /operator.
- Администратор получает уведомление с последними сообщениями диалога и кнопкой «Вернуть бота».
- Пока диалог у оператора, сообщения клиента уходят администратору, Claude не вызывается.
  Ответ администратора на сообщение бота (reply) бот пересылает клиенту.
- Бот снова отвечает сам после кнопки «Вернуть бота» или через `bot_config.operator_hours` часов
  после просьбы клиента или последнего ответа оператора.

Здесь — распознавание просьбы и тексты; хендлеры Telegram — в `operator_handlers.py`.
"""

import re
from collections.abc import Sequence
from datetime import datetime, timedelta

from anthropic.types import MessageParam

from medllmbot.text import normalize

OPERATOR_COMMAND = "operator"
END_BUTTON = "Вернуть бота"
_END_PREFIX = "operator:end:"  # данные кнопки «Вернуть бота»: + chat_id клиента
HISTORY_IN_NOTICE = 6  # последних сообщений диалога в уведомлении администратору
LINE_PREVIEW = 300  # символов одного сообщения истории в уведомлении

# Кого клиент зовёт: «оператора», «менеджера», «админа», «живого человека»
# «Оператор связи» — это мобильная связь, а не человек
_STAFF = r"(?:оператор|менеджер|администратор|админ|человек|сотрудник|консультант)\w*+(?!\s+связи)"
# Кого хотят: «хочу менеджера», но не «хочу администратору оставить отзыв»
_STAFF_ACC = r"(?:оператора|менеджера|администратора|админа|человека|сотрудника|консультанта)\b"
_ALIVE = r"(?:(?:живо|настоящ|реальн)\w*\s+)?"
# Между глаголом и тем, кого зовут: запятая, «пожалуйста»
_GAP = r"(?:[\s,]+(?:пожалуйста|плиз|please|pls))?[\s,]+"
# Просьба позвать человека. Не просьба: «мобильный оператор», «администратор перезвонит?», «поговорить с врачом»,
# «как связаться с администратором?» (это вопрос о контактах — на него ответит бот), «я настоящий человек»
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
    """Клиент просит живого человека вместо бота."""
    return _ASKS.search(normalize(text)) is not None


def operator_deadline(now: datetime, hours: int) -> datetime:
    """До какого времени бот молчит: каждый ответ оператора продлевает срок."""
    return now + timedelta(hours=hours)


def with_operator(until: datetime | None, now: datetime) -> bool:
    """Диалог у оператора: срок задан и ещё не прошёл."""
    return until is not None and until > now


def end_button_data(chat_id: int) -> str:
    return f"{_END_PREFIX}{chat_id}"


def parse_end_button(data: str | None) -> int | None:
    """chat_id клиента из кнопки «Вернуть бота»; None — это не она или данные испорчены."""
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
    """Текст сообщения истории. Репозиторий хранит историю строками; блоков API здесь не бывает."""
    content = message["content"]
    return content if isinstance(content, str) else ""


def client_label(name: str, chat_id: int) -> str:
    """Кто пишет — в каждом сообщении администратору: «@ivan (chat 42)»."""
    return f"{name} (chat {chat_id})"


def handoff_notice(client: str, request: str, history: Sequence[MessageParam], hours: int) -> str:
    """Первое уведомление: клиент просит человека. `client` — из `client_label`."""
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
    """Сообщение клиента, пока диалог у оператора."""
    return f"{client}:\n{text}"


# Ответы администратору
DELIVERY_FORBIDDEN = "Не удалось отправить клиенту: он заблокировал бота или удалил чат."
DELIVERY_FAILED = "Не удалось отправить клиенту: Telegram вернул ошибку. Попробуйте ещё раз или другим сообщением."
ENDED = "Бот снова отвечает клиенту."
CLIENT_LEFT = "клиент нажал /start и вернулся к боту."
ALREADY_ENDED = "Клиент уже с ботом."
NOT_ALLOWED = "Это может только администратор."
