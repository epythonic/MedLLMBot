"""Client question analytics: frequent topics, the share of dialogs with a lead and questions the bot did not answer.

Computed on the fly from the audit log (`audit_log`) and leads for the selected period:
- edited topic dictionaries apply to past questions right away;
- the volume is bounded by the audit log retention period (`bot_config.audit_retention_days`);
- Claude is not called: analytics costs nothing, and client conversations are not sent anywhere else.

Topics are word beginnings in Russian and Romanian (and a little English) for each business profile.
A question may fall into several topics. A message with no matches but with a phone number goes to
«Оставили телефон» (usually a reply to the bot asking for contacts), the rest go to «Другое».
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
from medllmbot.i18n import N_
from medllmbot.text import normalize
from medllmbot.tools import FOUND, ORDER_STATUS_NAME, SEARCH_CATALOG_NAME

PERIODS: tuple[int, ...] = (7, 30, 90)  # analytics page periods, in days
DEFAULT_PERIOD = 30
# The client has not written for over a day, so the next message starts a new dialog
DIALOG_GAP = timedelta(hours=24)
MAX_GAPS = 50  # unanswered question groups on the page: the most frequent ones
MAX_EXAMPLES = 3  # latest questions under the topic name
OTHER = N_("Other")
CONTACTS = N_("Left a phone number")

# Not questions to the bot: greeting, non-text, messages the bot skipped without a reply, and the conversation
# with the live operator, which a human handles. The request for an operator itself is a question, counted separately
NOT_QUESTIONS: frozenset[AuditOutcome] = frozenset(
    {
        AuditOutcome.GREETING,
        AuditOutcome.NOT_TEXT,
        AuditOutcome.RATE_LIMITED,
        AuditOutcome.BLOCKED,
        AuditOutcome.TO_OPERATOR,
        AuditOutcome.FROM_OPERATOR,
    }
)
QUESTION_OUTCOMES: tuple[AuditOutcome, ...] = tuple(o for o in AuditOutcome if o not in NOT_QUESTIONS)
# The client got the canned «Уточню у администратора…» instead of a model reply
_NO_ANSWER: frozenset[AuditOutcome] = frozenset({AuditOutcome.REFUSAL, AuditOutcome.EMPTY, AuditOutcome.TRUNCATED})

# ---------- Topics ----------


@dataclass(frozen=True)
class Topic:
    name: str
    pattern: re.Pattern[str]

    @classmethod
    def of(cls, name: str, *stems: str) -> Self:
        """A topic by word beginnings: «цен» finds «цена», «цены», «ценник».

        Stems are regular expressions for text after `normalize`; a trailing `\\b` means a whole word.
        A stem starts at a word start: no letter or digit before it.
        """
        return cls(name, re.compile(r"(?<!\w)(?:" + "|".join(stems) + ")"))

    def matches(self, normalized: str) -> bool:
        return self.pattern.search(normalized) is not None


# Topics shared by both profiles.
# Not prices: «почему», «центр», «стоит ли», «дорогу», Romanian «pretenție»
PRICES = Topic.of(
    N_("Prices"),
    r"цен(?!тр)", r"стои(?!т ли\b)", r"прайс", r"тариф", r"почем\b", r"дорого\b", r"дороже",
    r"дорог(?:ие|ая|ое|ой|ую|ого|их)\b", r"дешев", r"бесплатн", r"скидк", r"акци",
    r"pret(?:ul|uri|urile)?\b", r"cost", r"tarif", r"reducer", r"gratuit", r"price", r"discount",
)  # fmt: skip
PAYMENT = Topic.of(
    N_("Payment and installments"),
    r"оплат", r"заплат", r"плати", r"картой", r"карточк", r"по карте", r"наличн", r"безнал", r"рассроч",
    r"кредит", r"частями", r"страхов", r"перевод", r"счет\b",
    r"plat", r"card(?:ul)?\b", r"numerar", r"rate\b", r"credit", r"asigur", r"transfer", r"pay", r"installment",
)  # fmt: skip
# «Работаете ли с компаниями?», «работаете по страховке?» are not about opening hours, so «работаете» counts only
# with a time word. «Время работы» and «режим работы» do not fit: people ask that about batteries and vacuums.
# Romanian «afla» means both "to be located" and "to find out" («cum pot afla statusul»): location only after «unde»
LOCATION = Topic.of(
    N_("Address and opening hours"),
    r"адрес", r"наход", r"добрат", r"доехат", r"проехат", r"парков", r"шоурум", r"часы работы", r"график работы",
    r"до скольки", r"во сколько (?:открыва|закрыва|работаете)", r"открыт[ыо]\b", r"открывает", r"закрывает",
    r"работаете (?:сегодня|завтра|сейчас)", r"выходн", r"суббот", r"воскресен", r"в праздн",
    r"adres", r"orar", r"program(?:ul)? de lucru", r"deschis", r"inchide", r"ajung", r"parcar", r"sambat", r"duminic",
    r"unde (?:va |se )?afl", r"unde sunteti\b", r"locati",
    r"address", r"opening hours", r"working hours", r"are you open", r"open on",
)  # fmt: skip

CLINIC_TOPICS: tuple[Topic, ...] = (
    PRICES,
    Topic.of(
        N_("Appointments"),
        r"запис", r"запиш", r"прием", r"визит", r"свободн\w* (?:время|окн|мест|дат)", r"окошк",
        r"programa", r"programez", r"inscri", r"appointment", r"book",
    ),
    LOCATION,
    # «Скол» only as a whole word: otherwise every «сколько стоит» question would land in this topic
    Topic.of(
        N_("Pain and urgent care"),
        r"болит", r"болят", r"боль\b", r"болью", r"боли\b", r"больно", r"ноет", r"ноют", r"отек", r"опух", r"флюс",
        r"кровоточ", r"кровит", r"травм", r"скол\b", r"сколол", r"откол", r"сломал", r"треснул", r"выпал", r"сроч",
        r"остр", r"durere", r"doare", r"umfl", r"urgen", r"pain", r"hurt", r"toothache", r"emergency",
    ),
    Topic.of(
        N_("Dental treatment"),
        r"леч", r"вылеч", r"кариес", r"пломб", r"пульпит", r"канал", r"нерв", r"удал(?:ить|ят|ять|яют|яете|ени|ите|ил)", r"мудрост", r"рентген",
        r"снимок", r"анестез", r"обезбол", r"наркоз",
        r"trata", r"carie", r"plomb", r"canal", r"extract", r"scoate", r"radiograf", r"anestez",
        r"filling", r"cavity", r"root canal", r"x-ray",
    ),
    Topic.of(
        N_("Implants and prosthetics"),
        r"имплант", r"протез", r"корон", r"винир", r"мост",
        r"implant", r"protez", r"coroan", r"fatet", r"crown", r"veneer", r"denture",
    ),
    Topic.of(
        N_("Hygiene and whitening"),
        r"чистк", r"почистит", r"гигиен", r"отбел", r"камн", r"камен", r"налет", r"air ?flow",
        r"albir", r"detartraj", r"igien", r"curat", r"whiten", r"cleaning",
    ),
    Topic.of(
        N_("Braces and bite"),
        r"брекет", r"элайнер", r"капп?(?:а|ы|у|ой)\b", r"прикус", r"ортодонт", r"выравнив", r"кривы",
        r"ortodon", r"bracket", r"aparat dentar", r"gutier", r"braces", r"aligner",
    ),
    Topic.of(
        N_("Children's dentistry"),
        r"ребен", r"дет(?:и|ей|ям|ск|ок)", r"сын(?:\b|а|у|ом)", r"доч", r"малыш", r"молочн",
        r"copi[il]", r"pediatr", r"child", r"kid",
    ),
    PAYMENT,
)  # fmt: skip

SHOP_TOPICS: tuple[Topic, ...] = (
    Topic.of(
        N_("Product choice"),
        r"посовет", r"совет", r"порекоменд", r"рекоменд", r"выбрат", r"выбер", r"выбор", r"подобрат", r"подбер",
        r"лучш", r"отлича", r"разниц", r"сравн", r"какой (?:взять|купить)",
        r"recomand", r"sfat", r"alege", r"diferent", r"compar", r"recommend", r"better", r"best", r"which one",
    ),
    PRICES,
    # «Наличными» is payment, not availability: «налич» needs a vowel after it.
    # «Гарантия закончилась» is not availability
    Topic.of(
        N_("Availability"),
        r"налич[иея]", r"в продаже", r"остал", r"закончились", r"поступ",
        r"stoc", r"disponibil", r"in stock", r"available",
    ),
    # An order number (DE-10023, DE10023, ДЕ–10023 in Cyrillic) after the word «заказ» or at the message start:
    # that is how the client answers the bot's request. A model code mid-question («Есть WH-1000XM5?») is not an
    # order, «заказать» is a purchase. «Трекер» is a fitness band, so only «трек-номер»
    Topic.of(
        N_("Order status"),
        r"статус", r"(?:мой|моего|моим|наш|нашего) заказ", r"где заказ", r"номер заказа", r"посылк",
        r"заказ(?:а|у|е|ом)? (?:№ ?|# ?|номер )?[a-zа-я]{0,4}[-–—]?\d{3,}\b",
        r"^(?:№ ?|# ?)?[a-zа-я]{1,4}[-–—]\d{3,}\b", r"^(?:№ ?|# ?)?[a-z]{1,4} ?\d{5,}\b",
        r"отслед", r"трек[- ]номер",
        r"comanda mea", r"comenzi", r"statusul", r"my order", r"order status", r"order number", r"tracking",
    ),
    Topic.of(
        N_("Purchase and checkout"),
        r"купит", r"куплю", r"покупк", r"приобрест", r"заказат", r"закажу", r"оформ", r"корзин",
        r"cumpar", r"achizit", r"buy", r"purchase",
    ),
    Topic.of(
        N_("Delivery and pickup"),
        r"достав", r"курьер", r"привез", r"привоз", r"самовывоз", r"забрат", r"пункт выдачи",
        r"livr", r"curier", r"ridica(?:re|rea)?\b", r"deliver", r"shipping", r"pick ?up",
    ),
    PAYMENT,
    Topic.of(
        N_("Returns and exchanges"),
        r"возврат", r"вернут", r"верну\b", r"вернет", r"обмен", r"поменят", r"return", r"schimb", r"refund",
    ),
    Topic.of(
        N_("Warranty and repair"),
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
    """Question topics in dictionary order. No matches: «Оставили телефон» with a phone, otherwise «Другое»."""
    normalized = normalize(text)
    found = tuple(topic.name for topic in topics if topic.matches(normalized))
    if found:
        return found
    return (CONTACTS,) if extract_phone(text) else (OTHER,)


# ---------- Unanswered questions ----------


class GapReason(enum.Enum):
    """Why the client did not get a substantive answer."""

    NO_INFO = N_("Not in the knowledge base")
    NOT_FOUND = N_("Not in the catalog")
    UNKNOWN_PRICE = N_("Price not from the catalog")
    NO_ANSWER = N_("Bot did not answer")

    @override
    def __str__(self) -> str:
        return self.value


_WORD = r"[^\s.!?;:]+"  # a word with punctuation, but without a sentence end
_IN_HOURS = r"в (?:рабочее время|часы работы|рабочие часы)"
_WILL_ANSWER = r"ответ(?:ит|ят|им)\b"
# This is how the prompt rule says to reply when the knowledge base has no answer:
# «администратор ответит в рабочее время». «Ответит» is future tense:
# «администратор отвечает в Telegram в рабочее время» is information about the clinic.
# «Подтвердит», «уточнит» do not fit: the bot says them about price and availability in normal replies.
# Between «ответит» and the time, only words of one sentence without the conjunction «и»:
# «перезвонит в рабочее время и ответит» is not it
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
# A promise to call back is about booking or contacts, not a knowledge base gap:
# «Оставьте номер — администратор перезвонит в рабочее время», «администратор ответит на звонок в часы работы»
_CALLBACK = re.compile(r"(?<!\w)(?:перезвон|позвон|звон|свяж|va suna|vor suna|call)")
# A direct admission: «у меня нет такой информации». «Для данных процедур … нет» is not about information
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
    """The bot replied that it has no such information or that staff will answer. Approximate, by phrase.

    Each sentence is checked: the bot may decline in one and promise a call in another.
    """
    return any(
        _NO_DATA.search(sentence) or (_DEFERS.search(sentence) and not _CALLBACK.search(sentence))
        for sentence in _SENTENCE_END.split(normalize(answer))
    )


def _searched_nothing(tool_calls: str | None) -> bool:
    """The bot searched the catalog, and no search found anything."""
    searches = [
        line for line in (tool_calls or "").splitlines() if line.startswith(f"{SEARCH_CATALOG_NAME} ") and FOUND in line
    ]
    return bool(searches) and all(line.endswith(f"{FOUND} 0") for line in searches)


def _checked_order(tool_calls: str | None) -> bool:
    """The bot checked an order: "order not found" is a wrong number, not a knowledge base gap."""
    return any(line.startswith(ORDER_STATUS_NAME) for line in (tool_calls or "").splitlines())


def gap_reason(question: QuestionRow) -> GapReason | None:
    """Why the bot did not answer the question; None if it did.

    Technical failures (API error, spending limit, message too long) are not knowledge base gaps:
    they are visible in «Журнал» (the audit log).
    """
    if question.outcome is AuditOutcome.PRICE_REPLACED:
        return GapReason.UNKNOWN_PRICE
    answered = question.outcome is AuditOutcome.ANSWERED
    # An empty search outranks a refusal: it hints at what the catalog is missing
    if (answered or question.outcome in _NO_ANSWER) and _searched_nothing(question.tool_calls):
        return GapReason.NOT_FOUND
    if question.outcome in _NO_ANSWER:
        return GapReason.NO_ANSWER
    if answered and not _checked_order(question.tool_calls) and defers_to_staff(question.answer or ""):
        return GapReason.NO_INFO
    return None


def _gap_key(text: str) -> str:
    """Same questions ignoring case, spaces and punctuation: «Есть брекеты?» = «есть брекеты».

    A message of only emoji or punctuation is compared whole: «👍» and «😡» are different strings.
    """
    normalized = normalize(text)
    return " ".join(re.findall(r"\w+", normalized)) or normalized


# ---------- Summary ----------


def percent(part: int, whole: int) -> int | None:
    """Whole percent with round-half-up (1 of 8 is 13%); None if there is nothing to count."""
    return (200 * part + whole) // (2 * whole) if whole else None


@dataclass(frozen=True)
class TopicStat:
    name: str
    questions: int
    share: int  # percent of all questions; a question may be in several topics, so the total exceeds 100
    dialogs: int  # dialogs where the topic came up
    dialogs_with_lead: int
    examples: tuple[str, ...]  # latest questions of the topic

    @property
    def lead_percent(self) -> int | None:
        return percent(self.dialogs_with_lead, self.dialogs)

    @property
    def share_text(self) -> str:
        """Share for the page: a rare topic shows «<1%», not «0%»."""
        return "<1%" if self.questions and not self.share else f"{self.share}%"


@dataclass(frozen=True)
class Gap:
    """An unanswered question; identical questions are grouped into one row."""

    text: str  # latest wording
    count: int
    reason: GapReason  # reason the last time
    answer: str | None  # what the client got the last time
    last_at: datetime
    event_id: int  # latest audit log entry


@dataclass(frozen=True)
class Analytics:
    questions: int
    clients: int
    dialogs: int
    dialogs_with_lead: int
    unanswered: int  # unanswered questions, including repeats
    topics: tuple[TopicStat, ...]  # most frequent first, «Оставили телефон» and «Другое» last
    gaps: tuple[Gap, ...]  # at most MAX_GAPS most frequent
    gap_groups: int  # total distinct unanswered questions, except those the bot answered later
    handoffs: int = 0  # how many times clients asked for a live human

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
    """Dialogs: messages from one client with pauses no longer than `DIALOG_GAP`.

    A lead belongs to the dialog in which the client left the phone number. Dialogs without questions do not count:
    for example, a phone number in a message with signs of an attack.
    """
    timeline: dict[int, list[tuple[datetime, QuestionRow | None]]] = {}
    for question in questions:
        timeline.setdefault(question.chat_id, []).append((question.created_at, question))
    for lead in leads:
        timeline.setdefault(lead.chat_id, []).append((lead.created_at, None))

    dialogs: list[_Dialog] = []
    for points in timeline.values():
        points.sort(key=lambda point: (point[0], point[1] is None))  # same second: the question goes first
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
    for question in reversed(questions):  # latest questions first
        if extract_phone(question.text):
            continue  # examples are visible to anyone who opens the page, so no client phone numbers
        text = " ".join(question.text.split())
        for name in topics[question.event_id]:
            if len(examples[name]) < MAX_EXAMPLES and text not in examples[name]:
                examples[name].append(text)

    position = {name: i for i, name in enumerate(order)}
    last = {CONTACTS: 1, OTHER: 2}  # service groups go after the topics
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
    """Unanswered questions grouped by text, and the total number of unanswered questions."""
    groups: dict[str, Gap] = {}
    unanswered = 0
    for question in questions:  # oldest first: the latest entry of a group overwrites the wording
        reason = gap_reason(question)
        key = _gap_key(question.text)
        if reason is None:
            # The bot later answered the same question: the knowledge base was updated, the row is not needed
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
    """Summary of questions and leads. `questions` are oldest first, as the repository returns them."""
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
        handoffs=sum(question.outcome is AuditOutcome.HANDOFF for question in questions),
    )


async def collect(repo: Repository, kind: BusinessProfile, days: int, now: datetime) -> Analytics:
    """Analytics for `days` days up to `now`, which is UTC without a time zone, like times in the DB."""
    since = now - timedelta(days=days)
    questions = await repo.questions_since(since, QUESTION_OUTCOMES)
    return build_analytics(questions, await repo.leads_since(since), kind)


def parse_period(value: str | None) -> int:
    """Period from the page URL: 7, 30 or 90 days; anything else gives 30."""
    return next((days for days in PERIODS if value == str(days)), DEFAULT_PERIOD)
