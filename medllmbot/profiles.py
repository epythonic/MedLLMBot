"""Профили бизнеса: роль бота и предметные правила для стоматологии и магазина электроники.

Тексты профилей — в коде, а не в админке: правила безопасности в них нельзя случайно ослабить
из веб-формы. Какой профиль работает, хранится в `bot_config.profile`; данные демо для него
заливает `python -m medllmbot.db.seed --reset --profile shop`.
"""

from dataclasses import dataclass

from medllmbot.db.models import BusinessProfile
from medllmbot.text import Language


@dataclass(frozen=True)
class Replies:
    """Готовые ответы бота без модели: в них сотрудник называется так же, как в промпте профиля.

    У профиля два набора — русский и румынский: клиент получает заготовку на языке своего сообщения.
    """

    tech_error: str  # API недоступен или непредвиденная ошибка
    fallback: str  # отказ модели, обрезанный или пустой ответ, утечка промпта
    price_fallback: str  # модель назвала цену, которой нет в каталоге
    budget_exceeded: str  # дневной лимит расходов исчерпан
    lead_saved: str  # к ответу, когда телефон сохранён как заявка
    operator_started: str  # клиент попросил человека — диалог передан администратору
    operator_ended: str  # администратор вернул клиента боту


@dataclass(frozen=True)
class Profile:
    # Строки правил — с отступом продолжения «  », как в итоговом промпте
    role: str  # первая строка промпта: кто такой бот
    scope: str  # правило «отвечай только о …»
    staff: str  # кто отвечает клиенту вместо бота
    price_note: str  # что добавлять, называя цену
    domain_rules: tuple[str, ...]  # правила предметной области
    lead_rule: str  # когда просить телефон
    off_topic: str  # ответ на посторонние темы, с переносом строки
    catalog_title: str  # заголовок каталога в базе знаний
    search_examples: str  # примеры запросов для search_catalog
    replies: Replies  # по-русски
    replies_ro: Replies  # те же ответы по-румынски
    orders: bool = False  # есть ли заказы, статус которых бот узнаёт инструментом order_status

    def replies_in(self, language: Language) -> Replies:
        """Готовые ответы на языке клиента."""
        return self.replies_ro if language is Language.RO else self.replies


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
            tech_error="Извините, произошла техническая ошибка. Администратор свяжется с вами.",
            fallback="Уточню у администратора и вернусь к вам.",
            price_fallback="Уточню стоимость у администратора и вернусь к вам.",
            budget_exceeded="Сейчас я не могу ответить подробно. Администратор ответит вам в рабочее время.",
            lead_saved="Номер телефона мы сохранили — администратор перезвонит вам в рабочее время.",
            operator_started="Передал ваш вопрос администратору — он ответит здесь же, в этом чате, в рабочее время. "
            "Пишите ему прямо сюда.",
            operator_ended="Администратор завершил разговор. Если появятся вопросы — пишите, я отвечу.",
        ),
        replies_ro=Replies(
            tech_error="Ne pare rău, a apărut o eroare tehnică. Administratorul vă va contacta.",
            fallback="Voi verifica cu administratorul și revin cu un răspuns.",
            price_fallback="Voi verifica prețul cu administratorul și revin cu un răspuns.",
            budget_exceeded="Acum nu pot răspunde detaliat. Administratorul vă va răspunde în timpul programului de lucru.",
            lead_saved="Am salvat numărul de telefon — administratorul vă va suna în timpul programului de lucru.",
            operator_started="Am transmis întrebarea dvs. administratorului — vă va răspunde aici, în acest chat, "
            "în timpul programului de lucru. Scrieți-i direct aici.",
            operator_ended="Administratorul a încheiat conversația. Dacă mai aveți întrebări, scrieți-mi — vă răspund.",
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
            # Результаты поиска на русском, и без этого правила модель отвечала по-русски на румынский вопрос
            "- Каталог и результаты search_catalog — на русском. Клиенту, который пишет\n"
            "  по-румынски, отвечай по-румынски: описание переводи, название модели не меняй.",
            # Сумму, которой нет в каталоге, проверка цен заменит на «Уточню стоимость…»
            "- Не считай суммы и ежемесячные платежи: назови цены позиций,\n  итог и рассрочку посчитает менеджер.",
            "- Статус заказа узнавай инструментом order_status: попроси номер заказа\n"
            "  и последние 4 цифры телефона из заказа. Полный номер телефона не проси.\n"
            "  Статус, сумму и дату бери только из order_status в этом ответе: вызывай его заново,\n"
            "  а не повторяй прошлые ответы.",
        ),
        lead_rule="- Если клиент хочет купить, оформить заказ или поговорить с менеджером,\n  попроси имя и номер телефона.",
        off_topic="«Я могу помочь только\n  с вопросами о магазине»",
        # В каталоге и услуги: без них в заголовке модель не искала установку и отвечала «информации нет»
        catalog_title="Товары, услуги и цены",
        search_examples="«ноутбук 16 ГБ», «беспроводные наушники», «робот-пылесос», «установка стиральной машины»",
        replies=Replies(
            tech_error="Извините, произошла техническая ошибка. Менеджер свяжется с вами.",
            fallback="Уточню у менеджера и вернусь к вам.",
            price_fallback="Уточню стоимость у менеджера и вернусь к вам.",
            budget_exceeded="Сейчас я не могу ответить подробно. Менеджер ответит вам в рабочее время.",
            lead_saved="Номер телефона мы сохранили — менеджер перезвонит вам в рабочее время.",
            operator_started="Передал ваш вопрос менеджеру — он ответит здесь же, в этом чате, в рабочее время. "
            "Пишите ему прямо сюда.",
            operator_ended="Менеджер завершил разговор. Если появятся вопросы — пишите, я отвечу.",
        ),
        replies_ro=Replies(
            tech_error="Ne pare rău, a apărut o eroare tehnică. Managerul vă va contacta.",
            fallback="Voi verifica cu managerul și revin cu un răspuns.",
            price_fallback="Voi verifica prețul cu managerul și revin cu un răspuns.",
            budget_exceeded="Acum nu pot răspunde detaliat. Managerul vă va răspunde în timpul programului de lucru.",
            lead_saved="Am salvat numărul de telefon — managerul vă va suna în timpul programului de lucru.",
            operator_started="Am transmis întrebarea dvs. managerului — vă va răspunde aici, în acest chat, "
            "în timpul programului de lucru. Scrieți-i direct aici.",
            operator_ended="Managerul a încheiat conversația. Dacă mai aveți întrebări, scrieți-mi — vă răspund.",
        ),
        orders=True,
    ),
}
