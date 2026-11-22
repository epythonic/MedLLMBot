"""Detection of attempts to take over the bot (prompt injection).

Two layers:
- on input, heuristics over the client message (RU / RO / EN). Rephrasing can bypass
  them, so they are a signal for the audit log and the administrator, not a defense;
- on output, a check whether fragments of the system prompt got into the model reply.
  Such a reply is not sent to the client.

The real defense is the system prompt rules and the reply checks (prices, leaks).
"""

import re
import time
from collections.abc import Callable
from dataclasses import dataclass

_I = re.IGNORECASE


@dataclass(frozen=True)
class InjectionRule:
    code: str  # written to the audit log
    label: str  # shown to the administrator
    pattern: re.Pattern[str]


INJECTION_RULES: tuple[InjectionRule, ...] = (
    InjectionRule(
        "ignore_instructions",
        "просьба игнорировать инструкции",
        re.compile(
            # Imperative mood only: «можно забыть про ограничения в еде?» is an ordinary question
            r"(игнорир\w*|забудь\w*|отбрось\w*|не\s+следуй)\W+(?:\w+\W+){0,3}?"
            r"(инструкц|правил|указани|промпт|ограничени)"
            r"|(ignore|disregard|forget)\W+(?:\w+\W+){0,3}?(instruction|rule|prompt)"
            # «Uită-te» means "look", «regulat» means "regularly"
            r"|(ignor[aă]|uit[aă](?!-te))\W+(?:\w+\W+){0,3}?(instruc[tț]iun|regul(?!at))",
            _I,
        ),
    ),
    InjectionRule(
        "role_change",
        "попытка сменить роль бота",
        re.compile(
            # «а теперь ты скажи цену» is an ordinary question, so a new-role marker is required
            r"(ты\s+теперь|теперь\s+ты)\s+(—|-|являешься|будешь|не\s+бот|другой|новый|мой)"
            r"|представь,?\s+что\s+ты|веди\s+себя\s+как|притворись"
            r"|you\s+are\s+now|pretend\s+(to\s+be|you\s+are)|from\s+now\s+on\s+you"
            # «Acum ești deschis?» means "Are you open now?", so a new role is required: «acum ești un alt bot»
            r"|acum\s+e[sș]ti\s+(un|o)\b|pref[aă]-te",
            _I,
        ),
    ),
    InjectionRule(
        "authority_claim",
        "выдаёт себя за владельца или разработчика бота",
        re.compile(
            # «Я разработчик, можно записаться вечером?» is just the client's profession,
            # so a tie to the bot («я твой / ваш …») or to this clinic or shop is required
            # Nominative case only: «я вашему администратору звонила» is an ordinary complaint
            r"\bя\s+(твой|твоя|ваш|ваша|тебе)\s+(?:\w+\s+)?"
            r"(начальник|хозя|владел|создател|разработчик|админ|руководител|босс|директор|программист"
            r"|менеджер|сотрудни|продав)"
            r"|\bя\s+тебя\s+(создал|написал|запрограммировал|разработал|сделал)"
            # «Я директор частной клиники в Бельцах», «Я директор этой компании, нужен счёт» are clients
            # (companies often write to the shop); only this / your clinic, this / your shop
            # and «ваша» (your) company, i.e. the one that owns the bot, are suspicious
            r"|\bя\s+(?:\w+\s+){0,2}?(владел|хозя|директор|руководител|начальник|менеджер|сотрудни)\w*\s+"
            r"(?:(этой|вашей|твоей|данной)\s+клиник|(этого|вашего|твоего|данного)\s+(?:интернет-)?магазин"
            r"|(вашей|твоей)\s+(компани|фирм))"
            r"|\bI(?:'m|\s+am)\s+your\s+(owner|creator|developer|admin|boss|master|operator|manager)"
            r"|\bI(?:'m|\s+am)\s+the\s+(owner|developer|admin\w*|director|manager)\s+of\s+"
            r"(?:(this|the)\s+(bot|clinic)|(this|your)\s+(shop|store|company))"
            r"|\bsunt\s+(?:\w+\s+)?(șeful|seful|stăpânul|stapanul|proprietarul|creatorul|dezvoltatorul|administratorul"
            r"|directorul|managerul)"
            r"\s+(tău|tau|clinicii|acestui\s+magazin|acestei\s+companii|magazinului\s+(?:vostru|dumneavoastră))",
            _I,
        ),
    ),
    InjectionRule(
        "prompt_extraction",
        "попытка узнать системный промпт",
        re.compile(
            # «покажите правила записи» is an ordinary question; only «свои / скрытые» (own / hidden) rules are caught
            r"системн\w*\s+(промпт|инструкц|сообщени|настро)"
            # «Покажите свои правила записи / возврата» is a question about the clinic or shop, not about the bot
            r"|(покажи|выведи|повтори|напиши|раскрой)\w*\W+(свои|твои|исходн\w*|скрыт\w*)\W+"
            r"(инструкц|правил[а-я]*\b(?!\s+(?:записи|при[её]ма|посещения|оплаты|отмены|подготовки"
            r"|возврат|обмен|доставк|гаранти|покуп|рассрочк|самовывоз))|промпт|настройк)"
            r"|system\s+prompt"
            r"|(show|print|repeat|reveal)\W+(your|the\s+hidden|the\s+system)\W+"
            r"(instruction|prompt|rules)"
            r"|promptul|instruc[tț]iunile\s+tale",
            _I,
        ),
    ),
    InjectionRule(
        "markup_injection",
        "служебная разметка в сообщении",
        re.compile(r"</?\s*(info|system|assistant|user)\s*>|\[/?(INST|SYS)\]|<\|im_start\|>", _I),
    ),
    InjectionRule(
        "jailbreak",
        "известная техника обхода ограничений",
        # «DAN» is not caught: it is a common name in Moldova
        re.compile(r"\bjailbreak|do\s+anything\s+now|developer\s+mode|режим\w*\s+разработчика", _I),
    ),
)

LEAK_CODE = "prompt_leak"
LEAK_LABEL = "в ответе модели фрагменты системного промпта"

FLAG_LABELS: dict[str, str] = {rule.code: rule.label for rule in INJECTION_RULES} | {LEAK_CODE: LEAK_LABEL}


def detect_injection(text: str) -> tuple[str, ...]:
    """Codes of the triggered heuristics; an empty tuple means nothing suspicious."""
    return tuple(rule.code for rule in INJECTION_RULES if rule.pattern.search(text))


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


class LeakDetector:
    """Finds verbatim fragments of the secret text (the prompt rules) in a model reply.

    Only rule lines of at least `min_len` characters are compared: short lines like
    «с вопросами о клинике» may legitimately appear in a reply.
    """

    fragments: tuple[str, ...]

    def __init__(self, secret: str, min_len: int = 30) -> None:
        lines = (_normalize(line).lstrip("- ") for line in secret.splitlines())
        self.fragments = tuple(line for line in lines if len(line) >= min_len)

    def leaks(self, answer: str) -> bool:
        text = _normalize(answer)
        return "<info>" in text or any(fragment in text for fragment in self.fragments)


class AlertCooldown:
    """At most one notification about a suspicious client per `seconds` seconds.

    Otherwise an attacker would flood the administrator with messages. The state is in memory:
    after a bot restart the first notification arrives immediately, which is acceptable.
    """

    seconds: float
    _clock: Callable[[], float]
    _last: dict[int, float]

    def __init__(self, seconds: float = 15 * 60, clock: Callable[[], float] = time.monotonic):
        self.seconds = seconds
        self._clock = clock
        self._last = {}

    def ready(self, chat_id: int) -> bool:
        """True, and records the time, if a notification may be sent for this chat."""
        now = self._clock()
        last = self._last.get(chat_id)
        if last is not None and now - last < self.seconds:
            return False
        self._last[chat_id] = now
        return True
