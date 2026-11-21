"""Выявление попыток перехватить управление ботом (prompt injection).

Два слоя:
- на входе — эвристики по сообщению клиента (RU / RO / EN). Их можно обойти
  перефразированием, поэтому это сигнал для журнала и администратора, а не защита;
- на выходе — проверка, не попали ли в ответ модели фрагменты системного промпта.
  Такой ответ клиенту не отправляется.

Настоящая защита — правила в системном промпте и проверки ответа (цены, утечка).
"""

import re
import time
from collections.abc import Callable
from dataclasses import dataclass

_I = re.IGNORECASE


@dataclass(frozen=True)
class InjectionRule:
    code: str  # пишется в журнал
    label: str  # показывается администратору
    pattern: re.Pattern[str]


INJECTION_RULES: tuple[InjectionRule, ...] = (
    InjectionRule(
        "ignore_instructions",
        "просьба игнорировать инструкции",
        re.compile(
            # Только повелительное наклонение: «можно забыть про ограничения в еде?» — обычный вопрос
            r"(игнорир\w*|забудь\w*|отбрось\w*|не\s+следуй)\W+(?:\w+\W+){0,3}?"
            r"(инструкц|правил|указани|промпт|ограничени)"
            r"|(ignore|disregard|forget)\W+(?:\w+\W+){0,3}?(instruction|rule|prompt)"
            # «Uită-te» — «посмотри», «regulat» — «регулярно»
            r"|(ignor[aă]|uit[aă](?!-te))\W+(?:\w+\W+){0,3}?(instruc[tț]iun|regul(?!at))",
            _I,
        ),
    ),
    InjectionRule(
        "role_change",
        "попытка сменить роль бота",
        re.compile(
            # «а теперь ты скажи цену» — обычный вопрос, поэтому нужен маркер новой роли
            r"(ты\s+теперь|теперь\s+ты)\s+(—|-|являешься|будешь|не\s+бот|другой|новый|мой)"
            r"|представь,?\s+что\s+ты|веди\s+себя\s+как|притворись"
            r"|you\s+are\s+now|pretend\s+(to\s+be|you\s+are)|from\s+now\s+on\s+you"
            # «Acum ești deschis?» — «Вы сейчас открыты?», поэтому нужна новая роль: «acum ești un alt bot»
            r"|acum\s+e[sș]ti\s+(un|o)\b|pref[aă]-te",
            _I,
        ),
    ),
    InjectionRule(
        "authority_claim",
        "выдаёт себя за владельца или разработчика бота",
        re.compile(
            # «Я разработчик, можно записаться вечером?» — обычная профессия клиента,
            # поэтому нужна привязка к боту («я твой / ваш …») или к клинике
            # Только именительный падеж: «я вашему администратору звонила» — обычная жалоба
            r"\bя\s+(твой|твоя|ваш|ваша|тебе)\s+(?:\w+\s+)?"
            r"(начальник|хозя|владел|создател|разработчик|админ|руководител|босс|директор|программист)"
            r"|\bя\s+тебя\s+(создал|написал|запрограммировал|разработал|сделал)"
            # «Я директор частной клиники в Бельцах» — клиент; подозрительна только «эта / ваша» клиника
            r"|\bя\s+(?:\w+\s+){0,2}?(владел|хозя|директор|руководител|начальник)\w*\s+(этой|вашей|твоей|данной)\s+клиник"
            r"|\bI(?:'m|\s+am)\s+your\s+(owner|creator|developer|admin|boss|master|operator)"
            r"|\bI(?:'m|\s+am)\s+the\s+(owner|developer|admin\w*|director)\s+of\s+(this|the)\s+(bot|clinic)"
            r"|\bsunt\s+(?:\w+\s+)?(șeful|seful|stăpânul|stapanul|proprietarul|creatorul|dezvoltatorul|administratorul)"
            r"\s+(tău|tau|clinicii)",
            _I,
        ),
    ),
    InjectionRule(
        "prompt_extraction",
        "попытка узнать системный промпт",
        re.compile(
            # «покажите правила записи» — обычный вопрос, ловим только «свои / скрытые» правила
            r"системн\w*\s+(промпт|инструкц|сообщени|настро)"
            # «Покажите свои правила записи» — вопрос о клинике, а не о боте
            r"|(покажи|выведи|повтори|напиши|раскрой)\w*\W+(свои|твои|исходн\w*|скрыт\w*)\W+"
            r"(инструкц|правил[а-я]*\b(?!\s+(?:записи|при[её]ма|посещения|оплаты|отмены|подготовки))|промпт|настройк)"
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
        # «DAN» не ловим: в Молдове это частое имя
        re.compile(r"\bjailbreak|do\s+anything\s+now|developer\s+mode|режим\w*\s+разработчика", _I),
    ),
)

LEAK_CODE = "prompt_leak"
LEAK_LABEL = "в ответе модели фрагменты системного промпта"

FLAG_LABELS: dict[str, str] = {rule.code: rule.label for rule in INJECTION_RULES} | {LEAK_CODE: LEAK_LABEL}


def detect_injection(text: str) -> tuple[str, ...]:
    """Коды сработавших эвристик; пустой кортеж — ничего подозрительного."""
    return tuple(rule.code for rule in INJECTION_RULES if rule.pattern.search(text))


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


class LeakDetector:
    """Находит в ответе модели дословные фрагменты секретного текста (правил промпта).

    Сравниваются строки правил длиной от `min_len` символов: короткие строки вроде
    «с вопросами о клинике» могут честно встретиться в ответе.
    """

    fragments: tuple[str, ...]

    def __init__(self, secret: str, min_len: int = 30) -> None:
        lines = (_normalize(line).lstrip("- ") for line in secret.splitlines())
        self.fragments = tuple(line for line in lines if len(line) >= min_len)

    def leaks(self, answer: str) -> bool:
        text = _normalize(answer)
        return "<info>" in text or any(fragment in text for fragment in self.fragments)


class AlertCooldown:
    """Не чаще одного уведомления о подозрительном клиенте за `seconds` секунд.

    Иначе атакующий завалит администратора сообщениями. Состояние в памяти:
    после перезапуска бота первое уведомление придёт сразу — это допустимо.
    """

    seconds: float
    _clock: Callable[[], float]
    _last: dict[int, float]

    def __init__(self, seconds: float = 15 * 60, clock: Callable[[], float] = time.monotonic):
        self.seconds = seconds
        self._clock = clock
        self._last = {}

    def ready(self, chat_id: int) -> bool:
        """True и отметка времени, если по этому чату можно отправить уведомление."""
        now = self._clock()
        last = self._last.get(chat_id)
        if last is not None and now - last < self.seconds:
            return False
        self._last[chat_id] = now
        return True
