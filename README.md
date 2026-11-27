# MedLLMBot: a Telegram assistant bot for a dental clinic, with a web admin panel

[![CI](https://github.com/epythonic/MedLLMBot/actions/workflows/ci.yml/badge.svg)](https://github.com/epythonic/MedLLMBot/actions/workflows/ci.yml)

**English** · [Русский](README_ru.md) · [Română](README_ro.md) · [Changelog](CHANGELOG.md)

A mini project: a Telegram bot built on **aiogram 3** and **Claude (Anthropic API)**. It answers a dental clinic's customers' questions about services, prices, the address, opening hours and booking, in Russian and Romanian. All data is stored in **SQLite**, and the clinic's administrator manages it through a **web admin panel** (FastAPI + SQLAdmin) in Russian, Romanian or English: the price list, FAQ, leads, customers, bot settings. Changes are picked up without a restart.

> “Demo Dental Clinic” is fictional; all demo data is made up ([medllmbot/db/seed.py](medllmbot/db/seed.py)).

**Two demo profiles.** One command — `seed --reset --profile shop` — switches the same bot to a fictional electronics
store, “Demo Electro” (64 products, delivery, warranty, instalments). The profile sets the bot's role and domain
rules: the dental clinic never diagnoses and books appointments; the store helps pick a device for a budget, never
promises stock and places orders through a manager. The security rules are shared by both profiles.
The store's catalog is larger than the “Catalog in prompt” threshold, so there Claude searches for products with a tool.
The store also answers “where is my order?”: the `order_status` tool shows the status, items, total and delivery
date only if the customer gives the order number and the last 4 digits of the phone number on the order.

## Features

### Bot

- Answers only from the clinic's knowledge base: the “About the business” sections, services with prices, FAQ.
- Remembers the latest messages of the dialog; the history is stored in SQLite.
- Checks the prices in the model's answer against the price list. If the model names an invented price, the customer never sees it.
- **Searches a large catalog by itself** (tool use): if there are more active services than the threshold in the settings, the full price list stays out of the prompt — Claude calls the `search_catalog` tool backed by an SQLite FTS5 full-text index, gets up to 10 matching items and answers from them. The search understands different word forms (the accusative «чистку» finds «Чистка», “cleaning”), filters by price and never executes FTS syntax from the query. A small catalog still goes into the prompt in full — cheaper and faster.
- Saves a message with a phone number (Moldovan numbers in any notation, international ones with “+”) as a lead with the status “New” and forwards it to the administrator. The lead is not lost even if the LLM is unavailable, the message is over the length limit, or Telegram rejected the reply to the customer.
- **Calls a live person:** a customer asks for a human in Russian or Romanian — «позовите менеджера» (“call a manager”), «хочу поговорить с человеком» (“I want to talk to a person”) — or sends /operator, and the administrator gets a notification with the latest messages and answers right from Telegram by replying to the bot's message. While the operator has the dialog, the bot stays silent and forwards the customer's messages, photos included, to the administrator without calling Claude. The “Back to bot” button or a timeout from the settings hands the dialog back to the bot.
- Canned replies that bypass the model («Уточню у администратора», “I'll check with the administrator”; «Сообщение слишком длинное», “The message is too long”; an apology for a failure) are sent in the customer's language: Russian or Romanian. The language is detected from the message, or, when the message gives no clue (a command, an order number, a photo without a caption), from the customer's Telegram language. The /start greeting is chosen by the Telegram language, and customers whose Telegram is set to another language get both.
- While the model is thinking, the customer sees “typing…”. A long answer arrives as several messages within Telegram's limit.
- Protected against long messages, spam and prompt injection. Ignores customers blocked in the admin panel.
- Keeps an **audit log**: every incoming message, including rejected ones, with the model's answer, tokens, latency and signs of an attempt to take over the bot. Notifies the administrator about suspicious messages.
- Calculates the **cost of every answer** in tokens and dollars from the model price list and accumulates daily totals for the monthly report.

### Admin panel

- **Leads:** a list with a status filter (New / In progress / Done / Rejected) and administrator notes.
- **Orders** (store): number, customer, status, items, total, delivery date — the bot answers questions about an order from these. In the demo this is a database table; for a real store it is the store's own system (a CMS, 1C) behind the `OrderBackend` interface.
- **Clients:** who wrote and when, blocking with a single checkbox, who is currently handled by the live operator.
- **Analytics:** what customers ask about (topics by Russian and Romanian keywords), what share of dialogs ends with a lead, and which questions the bot found no answer to — with the reason and a link to the log entry. This is a hint at what to add to the knowledge base. It is computed from the log for 7, 30 or 90 days without calling Claude: it costs nothing, and customer messages are not sent anywhere else.
- **Chat history:** a read-only dialog log with search and a filter by chat.
- **Knowledge base:** services and prices, FAQ, sections about the clinic. Rows are reordered by drag and drop (SortableJS + HTMX), and the bot sees the new order immediately; entries can be disabled without deleting them. Search is case-insensitive for Cyrillic too.
- **Log:** all incoming messages with the processing outcome, the raw and the sent answer, tokens, latency and what the model searched for in the catalog. “Suspicious” and “Outcome” filters, read-only.
- **Costs:** a monthly report broken down by model with a forecast for the current month, daily spend, and the model price list (editable when Anthropic changes its prices).
- **Bot settings:** Claude model, limits, history depth, rate limit, the “Catalog in prompt” threshold, greetings in Russian and Romanian, the language of messages to the administrator, log retention. Values are validated by the form and by CHECK constraints in the database.
- **Administrators:** extra accounts with their own email and password — for example, for a partner who shows the demo to clients. The password is stored as a salted scrypt hash; access is turned off with a checkbox, and that person's open sessions end at once. The owner logs in with the username and password from `.env` and cannot be turned off from the admin panel.
- **Forms:** input errors are shown in the admin panel's language (WTForms catalogs, with corrections to inaccurate translations in Russian); decimal prices are accepted with either a dot or a comma.
- **Quick actions with HTMX:** a lead's status is changed right in the list; “Leads” and “Log” show a banner about new records without a reload (suspicious events are highlighted); a customer can be blocked from a log entry page. HTMX is vendored in the project rather than loaded from a CDN, works without `eval`, and state-changing requests are accepted only with the `HX-Request` header.
- **Help “?”:** a “?” icon sits next to every section, form field and table column. Hovering shows a short hint; clicking opens a side panel with detailed instructions. The panel does not cover the page; the F1 and “?” keys open it too. The home page has a diagram of how the bot works and a map of sections; the “Help” page has the full documentation with a table of contents.
- **Three languages:** the interface, the “?” help and the bot's Telegram messages to the administrator are in Russian, Romanian and English. The admin panel opens in the browser's language (English otherwise), and the switcher in the menu remembers the choice; the language of Telegram notifications is set in “Bot settings”. Translations are gettext catalogs (`medllmbot/locales`); statuses, log outcomes, analytics topics and attack indicators are translated too. A test fails if an interface string lacks a Russian or Romanian translation.

## Admin panel screenshots

All demo data is fictional.

| | |
| --- | --- |
| ![Home: how the bot works and a map of sections](docs/screenshots/en/home.png) | ![Leads: the status is changed right in the list](docs/screenshots/en/leads.png) |
| **Home** — how the bot works and a map of sections | **Leads** — the status is changed right in the list |
| ![Log: processing outcomes and suspicious messages](docs/screenshots/en/audit-log.png) | ![Analytics: question topics, leads, unanswered questions](docs/screenshots/en/analytics.png) |
| **Log** — the processing outcome of every message, attack attempts, live operator | **Analytics** — what customers ask about, share of dialogs with a lead, unanswered questions |
| ![Claude API costs by month](docs/screenshots/en/costs.png) | ![Store orders](docs/screenshots/en/orders.png) |
| **Costs** — cost of Claude requests by month, with a forecast | **Orders** (store) — the statuses the bot reports to the customer |
| ![Catalog: order changed by drag and drop](docs/screenshots/en/services.png) | ![Help “?” in the side panel](docs/screenshots/en/help-panel.png) |
| **Catalog and prices** — order changed by drag and drop | **Help “?”** — section instructions in a side panel |

## Architecture

```mermaid
flowchart LR
    TG[Customer in Telegram] <--> Bot
    subgraph Bot [Bot: aiogram 3]
        CM[ClientMiddleware<br/>customer tracking, blocking, settings] --> TM[ThrottlingMiddleware]
        TM --> H[handlers] --> A[ClinicAssistant]
    end
    A <--> Claude[Claude API]
    Bot <--> DB[(SQLite<br/>WAL)]
    Admin[Admin panel<br/>FastAPI + SQLAdmin] <--> DB
    Staff[Clinic administrator] --> Admin
    Bot -- new lead --> Staff
```

How the bot processes a message:

1. `ClientMiddleware` creates or updates the customer. It drops blocked customers and writes them to the log, and passes the settings from `bot_config` to the handler.
2. `ThrottlingMiddleware` lets through at most N messages per M seconds and writes the rejected ones to the log. Limits come from the settings; counters are kept in memory.
3. `ClinicAssistant` checks the message for attack indicators and length, then reads the knowledge base and history from the database, builds the system prompt and asks Claude. If the catalog is larger than the threshold, the model gets the `search_catalog` tool and may search the catalog several times before answering.
4. The model's answer is checked for system-prompt leaks and prices not in the price list, and Markdown is stripped from it. A normal exchange is saved to the history; a suspicious one is not.
5. Every outcome is written to the audit log.
6. The handler sends the answer (a long one in parts). If the message is suspicious, the administrator gets a notification (at most once per 15 minutes per customer). If there is a phone number, a lead is created — even when Telegram did not accept the reply to the customer.
7. An unexpected error at any step is caught: the customer gets an apology, and the error goes to the log.

`/start` and non-text messages (photos, voice messages) are logged too, with the outcomes “Greeting” and “Not text”.

### Database schema

```mermaid
erDiagram
    clients ||--o{ messages : "writes"
    clients ||--o{ leads : "leaves"
    clients ||--o{ audit_log : "log"
    clients {
        bigint chat_id PK
        string username
        string full_name
        bool is_blocked
    }
    messages {
        int id PK
        bigint chat_id FK
        string role
        text content
    }
    leads {
        int id PK
        bigint chat_id FK
        string phone
        enum status
        text admin_note
    }
    audit_log {
        int id PK
        bigint chat_id FK
        text user_text
        enum outcome
        bool is_suspicious
        string flags
        text llm_answer
        text final_answer
        int latency_ms
    }
    model_prices {
        string model
        int from_prompt_tokens
        float input_per_mtok
        float output_per_mtok
    }
    llm_usage_daily {
        date day
        string model
        int requests
        int cost_micro_usd
    }
    services {
        int id PK
        string name
        int price
        bool is_active
        int sort_order
    }
    faq {
        int id PK
        string question
        text answer
    }
    info_sections {
        int id PK
        string title
        text content
    }
    bot_config {
        int id PK "always 1"
        string model
        int max_len
        int rate_limit
        text greeting
        int audit_retention_days
    }
```

The schema is managed by **Alembic**. The bot and the admin panel apply migrations themselves on startup. In Docker, the `migrate` service applies them beforehand, so two processes never upgrade the schema at the same time. SQLite runs in **WAL** mode with `busy_timeout`, so the bot and the admin panel work with the same database concurrently without trouble. Foreign keys are enabled (`PRAGMA foreign_keys`).

## Security

| Threat | Mitigation |
| --- | --- |
| Prompt injection (“you are now a different bot”) | The system prompt declares its rules more important than customer messages; the knowledge base is isolated in `<info>` |
| Attack detection | Heuristics in RU / RO / EN catch requests to forget the instructions, role changes, “I'm your boss / the clinic owner”, attempts to extract the prompt, service markup and known jailbreaks. Such messages are flagged in the log, and the administrator gets a notification. Attack attempts never enter the history the model sees, so a multi-step attack does not accumulate |
| System-prompt leak | The model's answer is compared with the prompt rules. If it contains verbatim fragments of them, the customer gets a placeholder reply, and the log keeps both answers. This check does not catch a paraphrase or a translation of the rules — the prompt itself protects against that |
| Invented prices and discounts | `PriceChecker` compares the prices in the answer with the current price list in the database (it understands «1 200 лей», «1.200 lei», «900 леев»), including when the catalog is not in the prompt and the model searches it with a tool. The customer's budget passes only if the customer named that amount and the answer uses it as a limit («до 10 000 лей», “up to 10 000 lei”), not as an item price or a discount |
| Spam and API spend | A rate limit per chat, a message length limit before the API request, customer blocking in the admin panel. A daily spend limit for all customers combined: once it is reached, the bot answers without Claude until midnight UTC, leads are still saved, and the administrator gets a notification |
| Medical advice | The bot does not diagnose and refers acute pain to an appointment |
| API failure | A 30 s timeout and one SDK retry — the customer never waits longer than a minute. After that they get a polite fallback reply, and the lead is saved anyway. Any unexpected error (for example, a locked database) also ends with a reply to the customer rather than silence |
| Truncated or empty answer, model refusal | `stop_reason` is checked: an answer that hit `max_tokens` or the context window, an empty answer and a refusal (`refusal`) are not sent to the customer — they get a neutral phrase, and the log keeps the original answer and the outcome (“Answer cut off”, “Empty answer”, “Model refusal”) |
| Admin panel access | The owner's username and password from `.env` (constant-time comparison) and accounts from the “Administrators” section: the password is stored as a salted scrypt hash, and an unknown email takes as long to check as a wrong password. After 5 failed attempts from one address, login is locked for 15 minutes. The session is a signed cookie (HttpOnly, SameSite=Lax, 8 hours, `Secure` with `ADMIN_HTTPS_ONLY=true`); changing the password or turning the account off ends its sessions. CSP, `X-Frame-Options: DENY` and `nosniff` headers; pages with data are not cached. In Docker the port is published on `127.0.0.1` only |
| Personal data in the log | The retention period is configurable in the admin panel (90 days by default); the bot deletes old records every 6 hours |
| Someone else's orders | Order status only for the order number plus the last 4 digits of the phone number on the order; a wrong number and a wrong phone get the same reply. After 5 failed checks within an hour from one chat, the bot stops looking up orders. The phone number is never passed to the model |
| Secret leaks | Tokens live only in `.env` and are used as `SecretStr` in the code. `.env` is excluded from git and from the Docker image |

## Tech stack

Python 3.14, aiogram 3, Anthropic Python SDK, SQLAlchemy 2 (async) + aiosqlite, Alembic, FastAPI + SQLAdmin, HTMX, pydantic-settings, pytest, ruff, basedpyright (recommended mode, zero warnings), uv, Docker Compose, GitHub Actions.

## Project structure

```text
medllmbot/
├── __main__.py        # bot entry point: migrations → polling
├── main.py            # wiring of the bot's dependencies
├── config.py          # secrets and paths from the environment (pydantic-settings)
├── handlers.py        # Telegram handlers
├── middlewares.py     # ClientMiddleware, ThrottlingMiddleware
├── assistant.py       # knowledge base + history → LLM → price check → save
├── llm.py             # Anthropic API wrapper: tool use loop, error handling
├── tools.py           # search_catalog and order_status tools: schemas, parameter validation, result for the model
├── orders.py          # OrderBackend — interface to the store's order system, brute-force protection
├── operator.py        # live operator: request for a human, timeouts, texts for the administrator
├── operator_handlers.py # operator handlers: forwarding both ways, the “Back to bot” button
├── text.py            # text normalization, customer language, stripping Markdown from answers
├── i18n.py            # gettext translation catalogs (RU/RO/EN) and the N_ marker for labels in domain code
├── locales/           # ru/, ro/: messages.po — translations of the admin panel, of messages to the administrator and of canned replies to customers; ro/sqladmin.po — SQLAdmin's built-in strings
├── guards.py          # RateLimiter, PriceChecker, phone number detection
├── security.py        # prompt injection detector, prompt leak check
├── maintenance.py     # background log cleanup by retention period
├── costs.py           # price list, request cost, monthly report
├── analytics.py       # question analytics: topics, dialogs with a lead, unanswered questions
├── prompts.py         # system prompt and knowledge base rendering
├── profiles.py        # business profiles: bot role and domain rules (dental clinic, store)
├── db/
│   ├── models.py      # SQLAlchemy models
│   ├── repository.py  # all of the bot's database queries
│   ├── fts.py         # catalog full-text index (SQLite FTS5) and the query against it
│   ├── engine.py      # SQLite engine: WAL, foreign keys, busy_timeout, Cyrillic-aware lower()
│   ├── migrate.py     # python -m medllmbot.db.migrate
│   ├── seed.py        # python -m medllmbot.db.seed — demo data, --profile shop — the store
│   ├── demo_shop.py   # demo data for the fictional electronics store
│   └── migrations/    # Alembic
└── admin/
    ├── __main__.py    # python -m medllmbot.admin
    ├── app.py         # FastAPI + SQLAdmin
    ├── auth.py        # username and password login, brute-force protection, session
    ├── reports.py     # “Analytics” and “Monthly report” pages
    ├── help.py        # “?” help: field hints, processing outcomes, attack indicators
    ├── live.py        # HTMX quick actions: fragment routes
    ├── fragments.py   # HTML fragments for HTMX
    ├── forms.py       # base form for sections: errors in the admin panel's language, numbers with a comma
    ├── i18n.py        # admin languages on top of SQLAdmin i18n: language selection, Romanian catalog of SQLAdmin strings
    ├── static/        # htmx-2.0.11.min.js, sortable-1.15.7.min.js (+ licenses)
    ├── templates/     # reports, help texts per language (help/topics/{ru,ro,en}/), SQLAdmin template overrides
    └── views.py       # admin sections
evals/shop/             # store answer evaluation: questions, checks, judge, run (paid, manual)
tests/                 # 1209 tests, no network and no real keys
```

## Quick start

1. Install [uv](https://docs.astral.sh/uv/) and the dependencies:

   ```bash
   uv sync
   ```

2. Create `.env` from the template and fill in your values:

   ```bash
   cp .env.example .env
   ```

   You need a bot token from @BotFather, your chat id (ask @userinfobot), a key from the Anthropic Console, an admin
   password (at least 8 characters) and a secret key (at least 32 characters). You can generate the key like this:
   `python -c "import secrets; print(secrets.token_urlsafe(32))"`. If something is missing, the bot and the admin panel
   list the unset variables on startup instead of printing a traceback.

3. Create the database and fill it with demo data:

   ```bash
   uv run python -m medllmbot.db.migrate
   uv run python -m medllmbot.db.seed
   ```

4. Start the bot and the admin panel in two terminals:

   ```bash
   uv run python -m medllmbot          # bot
   uv run python -m medllmbot.admin    # admin panel: http://127.0.0.1:8000/admin
   ```

The administrator has to press /start in the bot once; otherwise Telegram won't let the bot message them first.

## Running in Docker

```bash
docker compose up -d --build                                  # migrate → bot + admin
docker compose run --rm migrate python -m medllmbot.db.seed   # demo data (once)
docker compose logs -f bot
```

The admin panel is at <http://127.0.0.1:8000/admin>. The database lives in the `bot-storage` volume and survives container rebuilds.

## Demo server

A small VPS (1 vCPU, 1 GB RAM) with Docker is enough for a demo. The bot uses long polling, so only the admin panel
needs incoming connections. A Docker Compose profile in `COMPOSE_PROFILES` chooses how it is published:

- `tunnel` — Cloudflare Tunnel (the demo uses it). The `cloudflared` container connects out to Cloudflare: the server
  opens no ports but SSH, needs no certificates, and its IP is not exposed. Needs a domain on Cloudflare
  and the tunnel token `TUNNEL_TOKEN`.
- `https` — Caddy on ports 80 and 443 obtains and renews the Let's Encrypt certificate itself. Needs a domain
  pointed at the server with an A record (`DOMAIN`).

`deploy/deploy.sh` starts the stack with `docker compose up -d --build` and takes the compose profile
from `COMPOSE_PROFILES` in `.env`. With either profile, set `ADMIN_HTTPS_ONLY=true`. Without a profile the admin panel
is reachable only on the server itself (`127.0.0.1:8000`).

### How changes reach the server

```mermaid
flowchart LR
    PR[Pull request] --> C1[CI: test, docker]
    C1 -->|green| M[Merge into main]
    M --> C2[CI: test, docker] --> K[config:<br/>keys from Environment demo]
    K --> D[deploy: SSH → deploy/deploy.sh]
    D --> S[Server: git pull, .env,<br/>docker compose up --build,<br/>health check]
```

- You can't push to `main` directly — only merge a PR with green `test` and `docker` checks (branch protection).
- After a merge, CI validates the keys from the GitHub Environment `demo`, builds `.env` from them and runs
  [deploy/deploy.sh](deploy/deploy.sh) on the server over SSH: the latest `main` (fast-forward only), a new `.env`,
  a container rebuild, then waiting until all services are up and the admin panel and the tunnel pass their health
  checks. If something did not come up, the deploy turns red,
  and the log shows the container's last lines.
- The CI key on the server is bound to this script (`command=` in `authorized_keys`): no other command can be run with it.
  The server's host key fingerprint is pinned in a secret, so SSH will not accept a spoofed host.
- Application secrets are stored only in the GitHub Environment `demo`; `.env` on the server is overwritten
  on every deploy, so keys are changed in GitHub. To get a new key to the server without a new PR, run the deploy
  by hand: Actions → CI → Run workflow on the `main` branch, or `gh workflow run ci.yml --ref main`.

### Initial setup (one time)

**On the server** (a user in the `docker` group):

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"          # log out and back in after this command
sudo git clone https://github.com/epythonic/MedLLMBot.git /opt/MedLLMBot
sudo chown -R "$USER" /opt/MedLLMBot
```

**Deploy key** — on your machine:

```bash
ssh-keygen -t ed25519 -N "" -C "github-actions-deploy" -f medllmbot-deploy
ssh-keyscan -t ed25519 YOUR_SERVER   # check the fingerprint: on the server, ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
```

Add the public key `medllmbot-deploy.pub` to `~/.ssh/authorized_keys` on the server as a single line:

```text
command="/opt/MedLLMBot/deploy/deploy.sh",no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-pty ssh-ed25519 AAAA… github-actions-deploy
```

**Cloudflare Tunnel** (the `tunnel` profile) — in the Cloudflare dashboard of the account with the domain:

1. Networking → Tunnels → **Create a tunnel**, with a name such as `medllmbot`.
2. Copy the tunnel token: Cloudflare shows it when the tunnel is created, after `--token` in the connector install
   command. This is the `TUNNEL_TOKEN` secret. Do not install the connector on the server — the `cloudflared`
   container runs it; until the first deploy the tunnel is not connected, which is expected.
3. The **Routes** tab → **Add route** → **Published application**: the demo's subdomain and domain, **Service URL** —
   `http://admin:8000`. This is the Docker Compose service name, not `localhost`: `cloudflared` runs in its own
   container. Cloudflare creates the DNS record for the subdomain itself.

With the tunnel, the server only needs SSH from outside: a firewall can close every other incoming port.

**In GitHub** — Settings → Environments → New environment `demo`, with these secrets:

| Secret | Value |
| --- | --- |
| `TG_TOKEN`, `ADMIN_ID`, `ANTHROPIC_API_KEY` | bot keys, as in `.env.example` |
| `ADMIN_PASSWORD`, `ADMIN_SECRET_KEY` | admin password (at least 8 characters) and session key (at least 32) |
| `DEPLOY_HOST`, `DEPLOY_USER` | server address and the user from `authorized_keys` |
| `DEPLOY_SSH_KEY` | contents of the private key `medllmbot-deploy` |
| `DEPLOY_KNOWN_HOSTS` | the `ssh-keyscan` output from the step above |
| `TUNNEL_TOKEN` | the Cloudflare Tunnel token (only for the `tunnel` profile) |

And variables (Environment variables): `COMPOSE_PROFILES` — `tunnel` or `https` (`https` by default),
`DOMAIN` — the demo domain for Caddy (only for `https`), `ADMIN_USERNAME` — the admin login (`admin` by default).
The key check in CI stops a profile without its key and an unknown profile name.

**Branch protection** for `main` — Settings → Branches, or with one command:

```bash
gh api -X PUT repos/epythonic/MedLLMBot/branches/main/protection --input - <<'JSON'
{
  "required_status_checks": {"strict": true, "contexts": ["test", "docker"]},
  "enforce_admins": true,
  "required_pull_request_reviews": {"required_approving_review_count": 0},
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false
}
JSON
```

**Enable deployment**: Settings → Secrets and variables → Actions → Variables → `DEPLOY_ENABLED` = `true`.
Until the variable exists, CI only checks the code, and `config` and `deploy` are skipped.

The first deploy is any PR merged into `main`, or a manual CI run. Demo data after the first start:

```bash
cd /opt/MedLLMBot && docker compose run --rm migrate python -m medllmbot.db.seed
```

### Costs and demo reset

- In the admin panel, “Bot settings” → **Daily spend limit, $** (1–2 $ is enough for a demo); in the
  [Anthropic Console](https://console.anthropic.com), set a monthly limit on the key as a second line of defence.
- After a showing, return the demo to its initial state. The command deletes customers, chat history, leads, orders and
  the log, and restores the knowledge base, the model price list and the settings; cost totals and the daily limit are kept:

  ```bash
  docker compose run --rm migrate python -m medllmbot.db.seed --reset
  ```

  To reset the demo every night, add this to the server's crontab:

  ```text
  0 3 * * * cd /opt/MedLLMBot && docker compose run --rm migrate python -m medllmbot.db.seed --reset
  ```

- To show the electronics store to a prospective client, run the same command with the profile. Back to the dental
  clinic — `--profile clinic`. If the demo is reset by crontab, add the profile there too, or the demo will turn back
  into the dental clinic overnight:

  ```bash
  docker compose run --rm migrate python -m medllmbot.db.seed --reset --profile shop
  ```

## Environment variables

| Variable | Default | Description |
| --- | --- | --- |
| `TG_TOKEN` | — | Bot token from @BotFather |
| `ADMIN_ID` | — | The administrator's personal chat id: lead notifications, replies to customers as the live operator (the operator does not work with a group id) |
| `ANTHROPIC_API_KEY` | — | Anthropic API key |
| `ADMIN_USERNAME` | `admin` | Admin panel login |
| `ADMIN_PASSWORD` | — | Admin panel password, at least 8 characters |
| `ADMIN_SECRET_KEY` | — | Session cookie signing key, at least 32 characters |
| `ADMIN_HOST` / `ADMIN_PORT` | `127.0.0.1` / `8000` | Admin panel address |
| `COMPOSE_PROFILES` | — | How the admin panel is published on the server: `tunnel` (Cloudflare Tunnel) or `https` (Caddy); empty — only the bot and the admin panel on `127.0.0.1` |
| `TUNNEL_TOKEN` | — | Cloudflare Tunnel token (the `tunnel` profile) |
| `DOMAIN` | — | Demo server domain for HTTPS via Caddy (the `https` profile) |
| `ADMIN_HTTPS_ONLY` | `false` | `true` if the admin panel is served over HTTPS through a reverse proxy: the session cookie gets the `Secure` flag |
| `DB_PATH` | `storage/bot.db` | Path to the SQLite database |

The Claude model, limits and greeting are not set here but in the admin panel, in “Bot settings”. Defaults: `claude-haiku-5-5`, answers up to 1024 tokens, messages up to 1024 characters, a history of 8 messages, 10 messages per 60 seconds, the log is kept for 90 days, no daily spend limit.

## Development

```bash
uv run pytest                                          # tests
uv run ruff check . && uv run ruff format --check .    # linter
uv run basedpyright                                     # type checking
uv run alembic revision --autogenerate -m "description"   # migration after changing the models
# new interface string: extract, update the catalogs, then translate msgstr into Russian and Romanian
uv run pybabel extract -F babel.cfg -k lazy_gettext -k N_ --no-wrap -o /tmp/messages.pot .
uv run pybabel update -i /tmp/messages.pot -d medllmbot/locales -D messages --no-wrap
```

Code, comments and commits are in English; everything customers and the administrator see is translated. Tests never touch the network: the Anthropic client and Telegram objects are replaced with fakes. Dedicated tests check that the migrations match the models and that the bot sees edits from the admin panel immediately. CI in GitHub Actions runs on every push on Python 3.14: the linter, type checking (basedpyright), tests, validation of the demo server keys from the GitHub Environment `demo`, and the Docker image build.

### Translations

| What | Where |
|---|---|
| Admin interface, the bot's messages to the administrator, its canned replies to customers | `medllmbot/locales/{ru,ro}/LC_MESSAGES/messages.po`; the English text is the `msgid` in the code |
| Romanian for SQLAdmin's built-in buttons (Russian ships with SQLAdmin) | `medllmbot/locales/ro/LC_MESSAGES/sqladmin.po` |
| Text of the «?» help sections | `medllmbot/admin/templates/help/topics/{ru,ro,en}/*.html` |
| Greeting | the admin panel, “Bot settings” (stored in the database) |
| This README | `README.md`, `README_ru.md`, `README_ro.md` |

To fix a translation, edit only the `msgstr` lines, by hand or in Poedit, and keep placeholders such as `%(name)s`. If you rename a button or a column, rename it in that language's help too: the help names interface elements exactly. The English text is the `msgid` in the code: after changing it, run the two `pybabel` commands above and translate the new entry. Check with `uv run pytest tests/test_i18n.py tests/test_help.py`: the first catches untranslated strings and broken placeholders, the second help sections missing in a language or with different anchors and links.

### Answer-quality evaluation

Tests check the code, while the quality of the model's answers is measured by a separate evaluation set, `evals/shop/`. It has 38 store customer questions: prices, picking within a budget, products the store does not carry, store terms, order status, bot rules and questions in Romanian. The questions go to the real bot with the store's prompt and tools, on a temporary copy of the demo database. The answers are checked by code (required prices and terms, no leaks about someone else's order, the expected tool was called, language, no Markdown) and by a Claude judge against a rubric: nothing invented, rules followed, the answer is helpful.

```bash
uv run python -m evals.shop.run --cases price-01,order-02   # pilot on a few questions
uv run python -m evals.shop.run --reps 2                    # full set: 76 answers, about $0.80 and 2.5 minutes
uv run python -m evals.shop.run --regrade                   # free: re-run the code checks on stored answers
```

A run costs money (it uses the key from `.env`), so it is started by hand, not in CI. Results, transcripts and the judge's explanations are written to `.claude/hillclimb/shop/` (not tracked in git). The `report.html` report is built by the generator from the `claude-api` skill for Claude Code.

First run (claude-haiku-5-5, November 2026): **80% ± 13%** of answers passed every check. Store terms, order status, rules and products missing from the catalog — 100%. Weak spots: picking within a budget and Romanian — analysis in the CHANGELOG.

After fixing what it found — **97% ± 4%**. Every code check passes; the judge found a rules violation in 2 answers out of 76.

## Notes

- **Prompt caching.** The system prompt is marked with `cache_control`. The prompt bytes change only when the admin edits the knowledge base. But the cache works only when the prefix is at least the model's minimum length. For Haiku 5.5 that is 512 tokens, and the demo prompt (about 2600 characters) should clear the threshold. For Haiku 4.5 the minimum is 4096 tokens, and the demo knowledge base does not reach it. Cache hits show up in the logs (`cache_read=`) and in the request log.
- **Cost is an estimate, not an invoice.** It is calculated from the token counts in the API response and the prices in the price list (the initial prices come from the [official page](https://platform.claude.com/docs/en/about-claude/pricing) as of 10 November 2026). Input, output (including the model's thinking), cache writes and cache reads are counted, as well as the Haiku 5.5 rate for prompts longer than 100,000 tokens. Taxes and contract discounts are not taken into account. Months are counted in UTC, as in Anthropic's billing. Money is stored in integer micro-dollars, with no rounding errors. Changing a price in the admin panel does not recalculate past requests. Daily totals contain no personal data and are not deleted together with the old log.
- **The attack detector is a signal, not a defence.** Heuristics can be bypassed by rephrasing. The protection comes from the system prompt rules and the output checks (prices, leaks); the log and the notifications help notice attempts and block the customer.
- **Limitations of the demo version:**
  - long polling only (no webhook);
  - the rate limit and the login attempt counter are kept in the memory of a single process (for several replicas they would have to move to Redis);
  - all admin panel accounts have the same rights, there are no roles; logging out clears the cookie in the browser, but a stolen cookie stays valid until the session ends (8 hours), the password is changed or the account is turned off;
  - the bot does not re-read an edited message, and it processes two quick messages from the same customer in parallel.

## License

[MIT](LICENSE). The vendored libraries in `medllmbot/admin/static/` come with their own licenses next to them (htmx — 0BSD, SortableJS — MIT).
