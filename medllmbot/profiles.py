"""Business profiles: the bot role and domain rules for the dental clinic and the electronics shop.

Profile texts live in code, not in the admin panel, so their security rules cannot be weakened
by accident from a web form. The active profile is stored in `bot_config.profile`; its demo data
is loaded by `python -m medllmbot.db.seed --reset --profile shop`.
"""

from dataclasses import dataclass
from functools import cache

from medllmbot.db.models import BusinessProfile
from medllmbot.i18n import N_, gettext_for
from medllmbot.text import Language


@dataclass(frozen=True)
class Replies:
    """Canned bot replies without the model: they name the staff member the same way as the profile prompt.

    In a profile the fields are English msgids; the Russian and Romanian texts live in the translation catalogs
    (`medllmbot/locales`), and `Profile.replies_in()` returns them in the language of the client's message.
    """

    tech_error: str  # API unavailable or an unexpected error
    fallback: str  # model refusal, truncated or empty reply, prompt leak
    price_fallback: str  # the model named a price that is not in the catalog
    budget_exceeded: str  # daily spending limit exhausted
    lead_saved: str  # appended to the reply when the phone is saved as a lead
    operator_started: str  # the client asked for a human; the dialog is handed to the administrator
    operator_ended: str  # the administrator handed the client back to the bot


@dataclass(frozen=True)
class Profile:
    # Rule lines use the "  " continuation indent, as in the final prompt
    role: str  # first prompt line: who the bot is
    scope: str  # the "answer only about …" rule
    staff: str  # who answers the client instead of the bot
    price_note: str  # what to add when naming a price
    domain_rules: tuple[str, ...]  # domain rules
    lead_rule: str  # when to ask for a phone number
    off_topic: str  # reply to off-topic questions, with a line break
    catalog_title: str  # catalog heading in the knowledge base
    search_examples: str  # example queries for search_catalog
    replies: Replies  # msgids; the client gets them through replies_in()
    orders: bool = False  # whether there are orders whose status the bot looks up with the order_status tool

    def replies_in(self, language: Language) -> Replies:
        """Canned replies in the client's language."""
        return _translated(self.replies, language)


@cache
def _translated(replies: Replies, language: Language) -> Replies:
    _ = gettext_for(language)
    return Replies(
        tech_error=_(replies.tech_error),
        fallback=_(replies.fallback),
        price_fallback=_(replies.price_fallback),
        budget_exceeded=_(replies.budget_exceeded),
        lead_saved=_(replies.lead_saved),
        operator_started=_(replies.operator_started),
        operator_ended=_(replies.operator_ended),
    )


PROFILES: dict[BusinessProfile, Profile] = {
    BusinessProfile.CLINIC: Profile(
        role="Ты помощник стоматологии в Кишинёве.",
        scope="- Отвечай только о клинике: услуги, цены, адрес, часы работы, запись.",
        staff="администратор",
        price_note="  Добавляй, что точную стоимость подтвердит администратор.",
        domain_rules=(
            "- Не ставь диагнозы и не назначай лечение. При острой боли, отёке\n"
            "  или травме советуй как можно скорее прийти на приём.",
        ),
        lead_rule="- Если клиент хочет записаться, попроси имя и номер телефона.",
        off_topic="«Я могу помочь только\n  с вопросами о клинике»",
        catalog_title="Услуги и цены",
        search_examples="«чистка зубов», «имплант»",
        replies=Replies(
            tech_error=N_("Sorry, a technical error occurred. The administrator will contact you."),
            fallback=N_("I will check with the administrator and get back to you."),
            price_fallback=N_("I will check the price with the administrator and get back to you."),
            budget_exceeded=N_("I cannot answer in detail right now. The administrator will reply during business hours."),
            lead_saved=N_("We have saved your phone number — the administrator will call you back during business hours."),
            operator_started=N_(
                "I have passed your question to the administrator — they will reply right here, in this chat, "
                "during business hours. Write to them right here."
            ),
            operator_ended=N_(
                "The administrator has ended the conversation. If you have questions, write to me — I will answer."
            ),
        ),
    ),
    BusinessProfile.SHOP: Profile(
        role="Ты помощник магазина электроники в Кишинёве.",
        scope="- Отвечай только о магазине: товары, цены, доставка, оплата, гарантия,\n  адрес, часы работы.",
        staff="менеджер",
        price_note="  Добавляй, что наличие и итоговую цену подтвердит менеджер.",
        domain_rules=(
            "- Помогай выбрать: уточни бюджет и задачи, предложи 2–3 подходящих\n  товара из каталога и объясни разницу.",
            "- Называй только характеристики из <info> и результатов search_catalog.\n"
            "  Не обещай наличие и сроки доставки, которых там нет.",
            "- Не сравнивай с другими магазинами и не советуй покупать в другом месте.",
            # Search results are in Russian, and without this rule the model answered a Romanian question in Russian
            "- Каталог и результаты search_catalog — на русском. Клиенту, который пишет\n"
            "  по-румынски, отвечай по-румынски: описание переводи, название модели не меняй.",
            # The price check replaces an amount that is not in the catalog with «Уточню стоимость…»
            "- Не считай суммы и ежемесячные платежи: назови цены позиций,\n  итог и рассрочку посчитает менеджер.",
            "- Статус заказа узнавай инструментом order_status: попроси номер заказа\n"
            "  и последние 4 цифры телефона из заказа. Полный номер телефона не проси.\n"
            "  Статус, сумму и дату бери только из order_status в этом ответе: вызывай его заново,\n"
            "  а не повторяй прошлые ответы.",
        ),
        lead_rule="- Если клиент хочет купить, оформить заказ или поговорить с менеджером,\n  попроси имя и номер телефона.",
        off_topic="«Я могу помочь только\n  с вопросами о магазине»",
        # The catalog includes services too: without them in the heading the model did not search for installation
        # and answered «информации нет»
        catalog_title="Товары, услуги и цены",
        search_examples="«ноутбук 16 ГБ», «беспроводные наушники», «робот-пылесос», «установка стиральной машины»",
        replies=Replies(
            tech_error=N_("Sorry, a technical error occurred. The manager will contact you."),
            fallback=N_("I will check with the manager and get back to you."),
            price_fallback=N_("I will check the price with the manager and get back to you."),
            budget_exceeded=N_("I cannot answer in detail right now. The manager will reply during business hours."),
            lead_saved=N_("We have saved your phone number — the manager will call you back during business hours."),
            operator_started=N_(
                "I have passed your question to the manager — they will reply right here, in this chat, "
                "during business hours. Write to them right here."
            ),
            operator_ended=N_("The manager has ended the conversation. If you have questions, write to me — I will answer."),
        ),
        orders=True,
    ),
}
