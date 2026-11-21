# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Before 1.0 a minor release may change the database schema. Migrations run automatically when the bot or the admin
panel starts, and every schema change ships as an Alembic migration.

## [Unreleased]

### Added

- Answer-quality eval for the store profile (`evals/shop/`). It contains 38 customer questions with code checks and
  a Claude judge, and runs the real bot on a temporary copy of the demo database. It is run by hand because each run
  calls the paid API.
- First baseline on claude-haiku-5-5: 80% ± 13% of 76 answers pass every check. The issues it found are fixed below,
  and the run after the fixes scores 97% ± 4%. Every code check passes there, and the judge flags 2 of 76 answers on
  its rules criterion.

### Changed

- The bot's fixed replies are in the customer's language. Before, a Romanian customer got them in Russian: the
  technical error, "Уточню стоимость у менеджера", "message too long", "type your question as text" and the
  live-operator messages. The language comes from the message text. When the text gives no clue, the bot uses the
  customer's Telegram language, as for a command, an order number or a photo without a caption. When the operator
  returns the dialog, the bot uses the customer's latest messages.
- The bot settings have a separate Romanian greeting. `/start` carries no text to detect the language from, so the
  bot goes by the customer's Telegram language:
  - Russian: the Russian greeting;
  - Romanian: the Romanian greeting;
  - any other language: both.

  If the Romanian greeting is empty, everyone gets the main greeting. Existing settings keep working this way after
  the migration.

### Fixed

- The price check no longer replaces a correct pick with "Уточню стоимость у менеджера" when the answer repeats the
  customer's own budget («до 10 000 лей»). This happened in 8 of 76 eval answers. The amount passes only if the
  customer named it and the answer uses it as a limit. Used as an item price or a discount, it is still replaced.
- The store bot answers a Romanian question in Romanian, although the catalog and the search results are in Russian.
- The store bot searches the catalog for services such as washing-machine installation, instead of saying it has no
  information. The catalog section in the prompt is now called «Товары, услуги и цены».
- Markdown in the model's answer is stripped before the customer sees it, so Telegram no longer shows asterisks.
  The audit log keeps the model's answer unchanged.

## [0.1.0] - 2026-11-21

The first release: a Claude-powered Telegram assistant for a small business, with a web admin panel. It ships with
two fictional demo businesses, a dental clinic and an electronics store.

### Bot

- Answers customers in Russian and Romanian from a knowledge base kept in SQLite: sections about the business,
  a catalog with prices, FAQ. The conversation history is kept per customer.
- Checks every price in the model's answer against the catalog, so the customer never sees an invented price.
- Turns any message with a phone number into a lead and notifies the administrator in Telegram. The lead is saved
  even if Claude is down, the message is too long, or Telegram rejects the reply.
- Searches a large catalog with a `search_catalog` tool backed by an SQLite FTS5 index, instead of putting the whole
  price list into the prompt (#3).
- Business profiles: the same bot works as a dental clinic or an electronics store, each with its own role, domain
  rules, replies and demo data, while the security rules stay shared (#4).
- Answers "where is my order?" with an `order_status` tool. It needs the order number and the last 4 digits of the
  order's phone. Orders come through an `OrderBackend` interface: a database table in the demo, the store's own
  system in a real project (#5).
- Hands the conversation to a live operator when a customer asks for a human. The administrator answers from
  Telegram by replying to the bot's message, and the bot stays silent until the operator returns the dialog or a
  timeout passes (#8).

### Admin panel

- FastAPI + SQLAdmin with a Russian interface:
  - leads with statuses;
  - customers with blocking;
  - read-only chat history;
  - catalog, FAQ and business info with drag-and-drop ordering;
  - bot settings.
- Changes reach the bot without a restart.
- Audit log of every incoming message: the outcome, the raw and the delivered answer, tokens, cost, latency,
  attack indicators and the tool calls the model made.
- Cost tracking from a price list kept in the database, with a monthly report and a month-end forecast.
- A daily spend limit, after which the bot answers without Claude.
- Contextual help: a "?" next to every section, form field and column.
- HTMX quick actions: change a lead's status in the list, see a banner for new records, block a customer from a
  log entry. HTMX is served from the project, not a CDN, and runs without `eval`.
- Orders section for the store profile (#5).
- Question analytics: frequent topics, the share of dialogs that end with a lead, and the questions the bot could
  not answer, each with a reason and a link to the log entry. It is computed from the audit log without calling
  Claude (#7).
- Live-operator status for each customer, and the operator timeout in the bot settings (#8).

### Security

- Prompt-injection heuristics in Russian, Romanian and English. Flagged messages never enter the model's history,
  and the administrator gets an alert.
- Answers that quote the system prompt are replaced with a neutral reply.
- Per-chat rate limit, message length limit, customer blocking.
- Order status is shown only to someone who knows the order's phone. A wrong number and a wrong phone get the same
  reply, failed checks are limited per chat and per order, and the phone never reaches the model.
- Admin login:
  - lock-out after repeated failed logins;
  - signed HttpOnly session cookie;
  - CSP and `X-Frame-Options`;
  - no caching of pages with personal data.
- Audit log retention is configurable, 90 days by default.

### Operations

- Docker Compose setup, with an optional Caddy profile for automatic HTTPS on a demo server.
- GitHub Actions run linting, type checking, tests and a Docker build on every pull request. After a merge to `main`,
  CI deploys the demo server over SSH.
- `seed --reset [--profile shop]` returns the demo to its initial state after a presentation.

### Known limitations

- Long polling only, no webhook. Rate-limit and login counters live in one process's memory.
- A single admin account without roles.
- Edited messages are not re-read.
- The tool-use paths (catalog search, order status) are covered by tests with a fake Anthropic client and have not yet
  been checked against the live API in this release.

[Unreleased]: https://github.com/epythonic/MedLLMBot/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/epythonic/MedLLMBot/releases/tag/v0.1.0
