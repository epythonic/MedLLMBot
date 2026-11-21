"""Аналитика вопросов клиентов: частые темы, доля диалогов с заявкой и вопросы, на которые бот не ответил.

Считается на лету по журналу (`audit_log`) и заявкам за выбранный период:
- поправленные словари тем сразу действуют и на прошлые вопросы;
- объём ограничен сроком хранения журнала (`bot_config.audit_retention_days`);
- Claude не вызывается: аналитика ничего не стоит, а переписка клиентов больше никуда не отправляется.

Темы — начала слов на русском и румынском (и немного английского) для каждого профиля бизнеса.
Вопрос может попасть в несколько тем. Сообщение без совпадений с телефоном — «Оставили телефон»
(обычно это ответ на просьбу бота оставить контакты), остальные — «Другое».
"""

import enum
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Self, override

from medllmbot.db.models import AuditOutcome, BusinessProfile
from medllmbot.db.repository import LeadRow, QuestionRow, Repository
from medllmbot.guards import extract_phone
from medllmbot.tools import FOUND, ORDER_STATUS_NAME, SEARCH_CATALOG_NAME

PERIODS: tuple[int, ...] = (7, 30, 90)  # периоды страницы аналитики, дней
DEFAULT_PERIOD = 30
# Клиент не писал дольше суток — следующее сообщение начинает новый диалог
DIALOG_GAP = timedelta(hours=24)
MAX_GAPS = 50  # групп вопросов без ответа на странице: самые частые
MAX_EXAMPLES = 3  # последних вопросов под названием темы
OTHER = "Другое"
CONTACTS = "Оставили телефон"

# Не вопросы: приветствие, не текст и сообщения, которые бот пропустил без ответа
NOT_QUESTIONS: frozenset[AuditOutcome] = frozenset(
    {AuditOutcome.GREETING, AuditOutcome.NOT_TEXT, AuditOutcome.RATE_LIMITED, AuditOutcome.BLOCKED}
)
QUESTION_OUTCOMES: tuple[AuditOutcome, ...] = tuple(o for o in AuditOutcome if o not in NOT_QUESTIONS)
# Клиент получил заготовку «Уточню у администратора…» вместо ответа модели
_NO_ANSWER: frozenset[AuditOutcome] = frozenset({AuditOutcome.REFUSAL, AuditOutcome.EMPTY, AuditOutcome.TRUNCATED})

# Сравнение без регистра, «ё» и румынских диакритик: «Приём» = «прием», «preț» = «pret».
# Типографский апостроф Claude — обычный: «don’t» = «don't»
_FOLD = str.maketrans({"ё": "е", "ă": "a", "â": "a", "î": "i", "ș": "s", "ş": "s", "ț": "t", "ţ": "t", "’": "'"})


def normalize(text: str) -> str:
    """Текст для сравнения: строчные буквы, «ё» → «е», румынские буквы без диакритики и обычный апостроф,
    одиночные пробелы.
    """
    return " ".join(text.lower().translate(_FOLD).split())


# ---------- Темы ----------


@dataclass(frozen=True)
class Topic:
    name: str
    pattern: re.Pattern[str]

    @classmethod
    def of(cls, name: str, *stems: str) -> Self:
        """Тема по началам слов: «цен» находит «цена», «цены», «ценник».

        Стемы — регулярные выражения для текста после `normalize`; `\\b` в конце — слово целиком.
        Стем начинается с начала слова: перед ним не буква и не цифра.
        """
        return cls(name, re.compile(r"(?<!\w)(?:" + "|".join(stems) + ")"))

    def matches(self, normalized: str) -> bool:
        return self.pattern.search(normalized) is not None


# Темы обоих профилей.
# Не цены: «почему», «центр», «стоит ли», «дорогу», румынское «pretenție»
PRICES = Topic.of(
    "Цены",
    r"цен(?!тр)", r"стои(?!т ли\b)", r"прайс", r"тариф", r"почем\b", r"дорого\b", r"дороже",
    r"дорог(?:ие|ая|ое|ой|ую|ого|их)\b", r"дешев", r"бесплатн", r"скидк", r"акци",
    r"pret(?:ul|uri|urile)?\b", r"cost", r"tarif", r"reducer", r"gratuit", r"price", r"discount",
)  # fmt: skip
PAYMENT = Topic.of(
    "Оплата и рассрочка",
    r"оплат", r"заплат", r"плати", r"картой", r"карточк", r"по карте", r"наличн", r"безнал", r"рассроч",
    r"кредит", r"частями", r"страхов", r"перевод", r"счет\b",
    r"plat", r"card(?:ul)?\b", r"numerar", r"rate\b", r"credit", r"asigur", r"transfer", r"pay", r"installment",
)  # fmt: skip
# «Работаете ли с компаниями?», «работаете по страховке?» — не о часах работы, поэтому «работаете» — только
# со словом о времени. «Время работы» и «режим работы» не подходят: так спрашивают об аккумуляторе и пылесосе
LOCATION = Topic.of(
    "Адрес и часы работы",
    r"адрес", r"наход", r"добрат", r"доехат", r"проехат", r"парков", r"шоурум", r"часы работы", r"график работы",
    r"до скольки", r"во сколько (?:открыва|закрыва|работаете)", r"открыт[ыо]\b", r"открывает", r"закрывает",
    r"работаете (?:сегодня|завтра|сейчас)", r"выходн", r"суббот", r"воскресен", r"в праздн",
    r"adres", r"orar", r"program(?:ul)? de lucru", r"deschis", r"inchide", r"ajung", r"parcar", r"sambat", r"duminic",
    r"address", r"opening hours", r"working hours", r"are you open", r"open on",
)  # fmt: skip

CLINIC_TOPICS: tuple[Topic, ...] = (
    PRICES,
    Topic.of(
        "Запись на приём",
        r"запис", r"запиш", r"прием", r"визит", r"свободн\w* (?:время|окн|мест|дат)", r"окошк",
        r"programa", r"programez", r"inscri", r"appointment", r"book",
    ),
    LOCATION,
    # «Скол» — только словом целиком: иначе в тему попал бы каждый вопрос «сколько стоит»
    Topic.of(
        "Боль и срочная помощь",
        r"болит", r"болят", r"боль\b", r"болью", r"боли\b", r"больно", r"ноет", r"ноют", r"отек", r"опух", r"флюс",
        r"кровоточ", r"кровит", r"травм", r"скол\b", r"сколол", r"откол", r"сломал", r"треснул", r"выпал", r"сроч",
        r"остр", r"durere", r"doare", r"umfl", r"urgen", r"pain", r"hurt", r"toothache", r"emergency",
    ),
    Topic.of(
        "Лечение зубов",
        r"леч", r"вылеч", r"кариес", r"пломб", r"пульпит", r"канал", r"нерв", r"удал(?:ить|ят|ять|яют|яете|ени|ите|ил)", r"мудрост", r"рентген",
        r"снимок", r"анестез", r"обезбол", r"наркоз",
        r"trata", r"carie", r"plomb", r"canal", r"extract", r"scoate", r"radiograf", r"anestez",
        r"filling", r"cavity", r"root canal", r"x-ray",
    ),
    Topic.of(
        "Имплантация и протезирование",
        r"имплант", r"протез", r"корон", r"винир", r"мост",
        r"implant", r"protez", r"coroan", r"fatet", r"crown", r"veneer", r"denture",
    ),
    Topic.of(
        "Гигиена и отбеливание",
        r"чистк", r"почистит", r"гигиен", r"отбел", r"камн", r"камен", r"налет", r"air ?flow",
        r"albir", r"detartraj", r"igien", r"curat", r"whiten", r"cleaning",
    ),
    Topic.of(
        "Брекеты и прикус",
        r"брекет", r"элайнер", r"капп?(?:а|ы|у|ой)\b", r"прикус", r"ортодонт", r"выравнив", r"кривы",
        r"ortodon", r"bracket", r"aparat dentar", r"gutier", r"braces", r"aligner",
    ),
    Topic.of(
        "Детская стоматология",
        r"ребен", r"дет(?:и|ей|ям|ск|ок)", r"сын(?:\b|а|у|ом)", r"доч", r"малыш", r"молочн",
        r"copi[il]", r"pediatr", r"child", r"kid",
    ),
    PAYMENT,
)  # fmt: skip

SHOP_TOPICS: tuple[Topic, ...] = (
    Topic.of(
        "Подбор товара",
        r"посовет", r"совет", r"порекоменд", r"рекоменд", r"выбрат", r"выбер", r"выбор", r"подобрат", r"подбер",
        r"лучш", r"отлича", r"разниц", r"сравн", r"какой (?:взять|купить)",
        r"recomand", r"sfat", r"alege", r"diferent", r"compar", r"recommend", r"better", r"best", r"which one",
    ),
    PRICES,
    # «Наличными» — это оплата, а не наличие: после «налич» нужна гласная. «Гарантия закончилась» — не наличие
    Topic.of(
        "Наличие",
        r"налич[иея]", r"в продаже", r"остал", r"закончились", r"поступ",
        r"stoc", r"disponibil", r"in stock", r"available",
    ),
    # Номер заказа (DE-10023, DE10023, ДЕ–10023 с кириллицей) — после слова «заказ» или в начале сообщения:
    # так клиент отвечает на просьбу бота. Код модели в середине вопроса («Есть WH-1000XM5?») — не заказ,
    # «заказать» — покупка. «Трекер» — это фитнес-браслет, поэтому только «трек-номер»
    Topic.of(
        "Статус заказа",
        r"статус", r"(?:мой|моего|моим|наш|нашего) заказ", r"где заказ", r"номер заказа", r"посылк",
        r"заказ(?:а|у|е|ом)? (?:№ ?|# ?|номер )?[a-zа-я]{0,4}[-–—]?\d{3,}\b",
        r"^(?:№ ?|# ?)?[a-zа-я]{1,4}[-–—]\d{3,}\b", r"^(?:№ ?|# ?)?[a-z]{1,4} ?\d{5,}\b",
        r"отслед", r"трек[- ]номер",
        r"comanda mea", r"comenzi", r"statusul", r"my order", r"order status", r"order number", r"tracking",
    ),
    Topic.of(
        "Покупка и оформление",
        r"купит", r"куплю", r"покупк", r"приобрест", r"заказат", r"закажу", r"оформ", r"корзин",
        r"cumpar", r"achizit", r"buy", r"purchase",
    ),
    Topic.of(
        "Доставка и самовывоз",
        r"достав", r"курьер", r"привез", r"привоз", r"самовывоз", r"забрат", r"пункт выдачи",
        r"livr", r"curier", r"ridica(?:re|rea)?\b", r"deliver", r"shipping", r"pick ?up",
    ),
    PAYMENT,
    Topic.of(
        "Возврат и обмен",
        r"возврат", r"вернут", r"верну\b", r"вернет", r"обмен", r"поменят", r"return", r"schimb", r"refund",
    ),
    Topic.of(
        "Гарантия и ремонт",
        r"гарант", r"ремонт", r"сервис", r"слома", r"не работает", r"не включа", r"не заряжа", r"брак", r"неисправ",
        r"дефект", r"garant", r"repar", r"defect", r"warranty", r"repair", r"broken",
    ),
    LOCATION,
)  # fmt: skip

TOPICS: dict[BusinessProfile, tuple[Topic, ...]] = {
    BusinessProfile.CLINIC: CLINIC_TOPICS,
    BusinessProfile.SHOP: SHOP_TOPICS,
}


def topics_of(text: str, topics: Sequence[Topic]) -> tuple[str, ...]:
    """Темы вопроса в порядке словаря. Без совпадений: с телефоном — «Оставили телефон», иначе «Другое»."""
    normalized = normalize(text)
    found = tuple(topic.name for topic in topics if topic.matches(normalized))
    if found:
        return found
    return (CONTACTS,) if extract_phone(text) else (OTHER,)


# ---------- Вопросы без ответа ----------


class GapReason(enum.Enum):
    """Почему клиент не получил ответа по существу."""

    NO_INFO = "Нет в базе знаний"
    NOT_FOUND = "Нет в каталоге"
    UNKNOWN_PRICE = "Цена не из каталога"
    NO_ANSWER = "Бот не ответил"

    @override
    def __str__(self) -> str:
        return self.value


_WORD = r"[^\s.!?;:]+"  # слово со знаками препинания, но без конца предложения
_IN_HOURS = r"в (?:рабочее время|часы работы|рабочие часы)"
_WILL_ANSWER = r"ответ(?:ит|ят|им)\b"
# Так правило промпта велит отвечать, если ответа нет в базе знаний: «администратор ответит в рабочее время».
# «Ответит» — будущее время: «администратор отвечает в Telegram в рабочее время» — это сведения о клинике.
# «Подтвердит», «уточнит» не подходят: так бот говорит о цене и наличии в обычных ответах. Между «ответит»
# и временем — слова одного предложения без союза «и»: «перезвонит в рабочее время и ответит» — это не то
_DEFERS = re.compile(
    "|".join(
        (
            rf"(?<!\w){_WILL_ANSWER}(?:\s+(?!и\b){_WORD}){{0,5}}?\s+{_IN_HOURS}",
            rf"(?<!\w){_IN_HOURS}(?:\s+(?!и\b){_WORD}){{0,3}}?\s+{_WILL_ANSWER}",
            # «Administratorul vă va răspunde în timpul programului»
            rf"(?<!\w)(?:va|vor) raspunde(?:\s+{_WORD}){{0,5}}?\s+"
            r"in (?:timpul programului|(?:timpul )?orel(?:e|or) de (?:lucru|program)|program)",
            rf"(?<!\w)will (?:{_WORD}\s+)?(?:answer|reply|respond|get back)(?:\s+{_WORD}){{0,5}}?\s+"
            r"(?:during|in) (?:business|working|office) hours",
        )
    )
)
# Обещание позвонить — это запись или контакты, а не пробел в базе знаний:
# «Оставьте номер — администратор перезвонит в рабочее время», «администратор ответит на звонок в часы работы»
_CALLBACK = re.compile(r"(?<!\w)(?:перезвон|позвон|звон|свяж|va suna|vor suna|call)")
# Прямое признание: «у меня нет такой информации». «Для данных процедур … нет» — не о сведениях
_NO_DATA = re.compile(
    "|".join(
        (
            rf"(?<!\w)нет (?:{_WORD}\s+)?(?:информации|данных|сведений)\b",
            rf"(?<!\w)(?:информации|сведений)(?:\s+{_WORD}){{0,4}}?\s+нет\b",
            rf"(?<!\w)nu (?:am|avem|dispun de|detin) (?:{_WORD}\s+)?informati",
            r"(?<!\w)(?:don't|do not) have (?:any |the )?(?:information|details)",
        )
    )
)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def defers_to_staff(answer: str) -> bool:
    """Бот ответил, что такой информации нет или что на вопрос ответит сотрудник. По фразе — приблизительно.

    Проверяется каждое предложение: в одном бот может отказать, в другом — пообещать звонок.
    """
    return any(
        _NO_DATA.search(sentence) or (_DEFERS.search(sentence) and not _CALLBACK.search(sentence))
        for sentence in _SENTENCE_END.split(normalize(answer))
    )


def _searched_nothing(tool_calls: str | None) -> bool:
    """Бот искал в каталоге, и ни один поиск ничего не нашёл."""
    searches = [
        line for line in (tool_calls or "").splitlines() if line.startswith(f"{SEARCH_CATALOG_NAME} ") and FOUND in line
    ]
    return bool(searches) and all(line.endswith(f"{FOUND} 0") for line in searches)


def _checked_order(tool_calls: str | None) -> bool:
    """Бот проверял заказ: «не нашёл заказ» — ошибка в номере, а не пробел в базе знаний."""
    return any(line.startswith(ORDER_STATUS_NAME) for line in (tool_calls or "").splitlines())


def gap_reason(question: QuestionRow) -> GapReason | None:
    """Почему бот не ответил на вопрос; None — ответил.

    Технические сбои (ошибка API, лимит расходов, слишком длинное сообщение) — не пробел в базе знаний:
    их видно в «Журнале».
    """
    if question.outcome is AuditOutcome.PRICE_REPLACED:
        return GapReason.UNKNOWN_PRICE
    answered = question.outcome is AuditOutcome.ANSWERED
    # Пустой поиск важнее отказа: он подсказывает, чего не хватает в каталоге
    if (answered or question.outcome in _NO_ANSWER) and _searched_nothing(question.tool_calls):
        return GapReason.NOT_FOUND
    if question.outcome in _NO_ANSWER:
        return GapReason.NO_ANSWER
    if answered and not _checked_order(question.tool_calls) and defers_to_staff(question.answer or ""):
        return GapReason.NO_INFO
    return None


def _gap_key(text: str) -> str:
    """Одинаковые вопросы без учёта регистра, пробелов и знаков препинания: «Есть брекеты?» = «есть брекеты».

    Сообщение из одних эмодзи или знаков сравнивается целиком: «👍» и «😡» — разные строки.
    """
    normalized = normalize(text)
    return " ".join(re.findall(r"\w+", normalized)) or normalized


# ---------- Сводка ----------


def percent(part: int, whole: int) -> int | None:
    """Целый процент с округлением половины вверх (1 из 8 — 13%); None, если считать не из чего."""
    return (200 * part + whole) // (2 * whole) if whole else None


@dataclass(frozen=True)
class TopicStat:
    name: str
    questions: int
    share: int  # процент от всех вопросов; вопрос бывает в нескольких темах, поэтому в сумме больше 100
    dialogs: int  # диалогов, где была тема
    dialogs_with_lead: int
    examples: tuple[str, ...]  # последние вопросы темы

    @property
    def lead_percent(self) -> int | None:
        return percent(self.dialogs_with_lead, self.dialogs)

    @property
    def share_text(self) -> str:
        """Доля для страницы: редкая тема — «<1%», а не «0%»."""
        return "<1%" if self.questions and not self.share else f"{self.share}%"


@dataclass(frozen=True)
class Gap:
    """Вопрос без ответа; одинаковые вопросы собраны в одну строку."""

    text: str  # последняя формулировка
    count: int
    reason: GapReason  # причина в последний раз
    answer: str | None  # что получил клиент в последний раз
    last_at: datetime
    event_id: int  # последняя запись журнала


@dataclass(frozen=True)
class Analytics:
    questions: int
    clients: int
    dialogs: int
    dialogs_with_lead: int
    unanswered: int  # вопросов без ответа, с повторами
    topics: tuple[TopicStat, ...]  # самые частые сверху, «Оставили телефон» и «Другое» — последними
    gaps: tuple[Gap, ...]  # не больше MAX_GAPS самых частых
    gap_groups: int  # всего разных вопросов без ответа, кроме тех, на которые бот потом ответил

    @property
    def lead_percent(self) -> int | None:
        return percent(self.dialogs_with_lead, self.dialogs)

    @property
    def unanswered_percent(self) -> int | None:
        return percent(self.unanswered, self.questions)


@dataclass
class _Dialog:
    topics: set[str] = field(default_factory=set[str])
    questions: int = 0
    has_lead: bool = False


def _dialogs(questions: Sequence[QuestionRow], leads: Sequence[LeadRow], topics: dict[int, tuple[str, ...]]) -> list[_Dialog]:
    """Диалоги: сообщения одного клиента с паузами не дольше `DIALOG_GAP`.

    Заявка относится к диалогу, в котором клиент оставил телефон. Диалоги без вопросов не считаются:
    например, телефон в сообщении с признаками атаки.
    """
    timeline: dict[int, list[tuple[datetime, QuestionRow | None]]] = {}
    for question in questions:
        timeline.setdefault(question.chat_id, []).append((question.created_at, question))
    for lead in leads:
        timeline.setdefault(lead.chat_id, []).append((lead.created_at, None))

    dialogs: list[_Dialog] = []
    for points in timeline.values():
        points.sort(key=lambda point: (point[0], point[1] is None))  # в одну секунду вопрос раньше заявки
        current: _Dialog | None = None
        last_at: datetime | None = None
        for at, question in points:
            if current is None or last_at is None or at - last_at > DIALOG_GAP:
                current = _Dialog()
                dialogs.append(current)
            last_at = at
            if question is None:
                current.has_lead = True
            else:
                current.questions += 1
                current.topics.update(topics[question.event_id])
    return [dialog for dialog in dialogs if dialog.questions]


def _topic_stats(
    questions: Sequence[QuestionRow], topics: dict[int, tuple[str, ...]], dialogs: Sequence[_Dialog], order: Sequence[str]
) -> tuple[TopicStat, ...]:
    counts = Counter(name for names in topics.values() for name in names)
    in_dialogs = Counter(name for dialog in dialogs for name in dialog.topics)
    with_lead = Counter(name for dialog in dialogs if dialog.has_lead for name in dialog.topics)
    examples: dict[str, list[str]] = {name: [] for name in counts}
    for question in reversed(questions):  # последние вопросы — первыми
        if extract_phone(question.text):
            continue  # примеры видны всем, кто открыл страницу, — без телефонов клиентов
        text = " ".join(question.text.split())
        for name in topics[question.event_id]:
            if len(examples[name]) < MAX_EXAMPLES and text not in examples[name]:
                examples[name].append(text)

    position = {name: i for i, name in enumerate(order)}
    last = {CONTACTS: 1, OTHER: 2}  # служебные группы — после тем
    names = sorted(counts, key=lambda name: (last.get(name, 0), -counts[name], position.get(name, len(position))))
    total = len(questions)
    return tuple(
        TopicStat(
            name=name,
            questions=counts[name],
            share=percent(counts[name], total) or 0,
            dialogs=in_dialogs[name],
            dialogs_with_lead=with_lead[name],
            examples=tuple(examples[name]),
        )
        for name in names
    )


def _gaps(questions: Sequence[QuestionRow]) -> tuple[list[Gap], int]:
    """Вопросы без ответа, сгруппированные по тексту, и сколько всего вопросов без ответа."""
    groups: dict[str, Gap] = {}
    unanswered = 0
    for question in questions:  # старые первыми: последняя запись группы перезаписывает формулировку
        reason = gap_reason(question)
        key = _gap_key(question.text)
        if reason is None:
            # Позже бот ответил на такой же вопрос — базу знаний уже дополнили, строка больше не нужна
            groups.pop(key, None)
            continue
        unanswered += 1
        count = groups[key].count + 1 if key in groups else 1
        groups[key] = Gap(
            text=" ".join(question.text.split()),
            count=count,
            reason=reason,
            answer=question.answer,
            last_at=question.created_at,
            event_id=question.event_id,
        )
    ranked = sorted(groups.values(), key=lambda gap: (-gap.count, -gap.event_id))
    return ranked, unanswered


def build_analytics(questions: Sequence[QuestionRow], leads: Sequence[LeadRow], kind: BusinessProfile) -> Analytics:
    """Сводка по вопросам и заявкам. `questions` — старые первыми, как их отдаёт репозиторий."""
    dictionary = TOPICS[kind]
    topics = {question.event_id: topics_of(question.text, dictionary) for question in questions}
    dialogs = _dialogs(questions, leads, topics)
    gaps, unanswered = _gaps(questions)
    return Analytics(
        questions=len(questions),
        clients=len({question.chat_id for question in questions}),
        dialogs=len(dialogs),
        dialogs_with_lead=sum(dialog.has_lead for dialog in dialogs),
        unanswered=unanswered,
        topics=_topic_stats(questions, topics, dialogs, [topic.name for topic in dictionary]),
        gaps=tuple(gaps[:MAX_GAPS]),
        gap_groups=len(gaps),
    )


async def collect(repo: Repository, kind: BusinessProfile, days: int, now: datetime) -> Analytics:
    """Аналитика за `days` дней до `now` — UTC без часового пояса, как время в БД."""
    since = now - timedelta(days=days)
    questions = await repo.questions_since(since, QUESTION_OUTCOMES)
    return build_analytics(questions, await repo.leads_since(since), kind)


def parse_period(value: str | None) -> int:
    """Период из адреса страницы: 7, 30 или 90 дней; что-то другое — 30."""
    return next((days for days in PERIODS if value == str(days)), DEFAULT_PERIOD)
