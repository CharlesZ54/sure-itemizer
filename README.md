# sure-itemizer

Splits and renames merchant transactions in [Sure](https://github.com/we-promise/sure) using the merchant's own order data, so Sure's AI categorizer sees what was actually bought instead of guessing from the store name.

A $30.75 `AMAZON MKTPL*EXAMPLE01` charge becomes:

| Split | Amount |
|---|---|
| USB-C cable, 6 ft | $21.21 |
| Toothpaste (x3) | $9.54 |

The new rows are left uncategorized, so Sure's **Auto categorize** rule picks them up like any other transaction.

## Merchants

| Merchant | What happens in Sure | Connects with | Guide |
|---|---|---|---|
| Amazon | Charges split into items | Your Amazon sign-in | [docs/amazon.md](docs/amazon.md) |
| eBay | Charges split into items | eBay's official API, with your own free developer keys | [docs/ebay.md](docs/ebay.md) |
| Venmo | Payments renamed; cash-outs split into the payments behind them | Venmo's unofficial API, with your sign-in | [docs/venmo.md](docs/venmo.md) |
| Target | *In progress:* sign-in works, receipt reading isn't built yet | Your Target sign-in | [docs/target.md](docs/target.md) |

Choose merchants with `SOURCES`, then connect each one from the status page (**Integrations → Connect**). Passwords are used once to sign in and never stored; only the session or token the merchant issues is kept, in `/data`.

## How it works

1. **Find candidates in Sure.** It lists recent transactions whose name matches a merchant (e.g. `amazon|amzn`). If there are none, it never contacts the merchant.
2. **Fetch what the merchant charged,** with what each charge paid for. Each guide explains how for that merchant.
3. **Match charges to Sure transactions.** A match needs the exact amount and a bank date within a few days of the merchant's. Each charge matches at most one transaction, and the closest date wins; a bank line that quotes the merchant's order number goes to that order first.
4. **Split or rename it in Sure** (`POST /api/v1/transactions/:id/split`, or an update of the name). A split is apportioned by item price, so tax and shipping land proportionally, and the rows always add up to the cent. A renamed transaction keeps the bank's original text in its notes.
   - **One item:** still a one-row split. By the time the service sees a charge, Sure's AI rule has usually already categorized and locked it from the bare merchant name. A fresh, uncategorized row named after the item is what gets it categorized again with real information.

Everything it decides is recorded in `/data/itemizer.db` and shown on a small status page. Once a transaction is finished, it's never touched again, and neither are the split rows it created.

## Requirements

- **Sure v0.7.6 or newer** for splits; the split API arrived in [PR #4117](https://github.com/we-promise/sure/pull/4117). On older versions splits stay *deferred* (or *planned* in a dry run); renames work on any version.
- **Something in Sure that categorizes the new rows**, such as a rule with the *Auto categorize* action.
- **A Sure API key** with `read_write` scope (*Settings → API keys*).
- **An account with at least one supported merchant** (Amazon, Target, eBay, Venmo), connected from the web UI (see its guide).
- **Docker** (or any container runtime that runs Docker images).

## Running it

The image is `ghcr.io/charlesz54/sure-itemizer:latest`. It keeps its state and sign-ins in `/data`, serves the status page on port 8686, and runs as UID 99 / GID 100, so the data directory must be writable by that user (or pass your own with `user:`). See [Configuration](#configuration) for every variable.

**Docker Compose:**

```yaml
services:
  sure-itemizer:
    image: ghcr.io/charlesz54/sure-itemizer:latest
    container_name: sure-itemizer
    restart: unless-stopped
    ports:
      - "8686:8686"
    volumes:
      - ./data:/data
    environment:
      SURE_URL: http://sure.example.lan:3000
      SURE_API_KEY: your-sure-api-key
      DRY_RUN: "true"
      SOURCES: amazon
      SCHEDULE: "30 7 * * *"
      TZ: America/New_York
      # APPRISE_URL: discord://webhook_id/webhook_token
      # PUBLIC_URL: http://nas.example.lan:8686
    mem_limit: 1g
```

**docker run:**

```sh
mkdir -p data && sudo chown 99:100 data
docker run -d --name sure-itemizer --restart unless-stopped --memory=1g \
  -p 8686:8686 -v "$PWD/data:/data" \
  -e SURE_URL=http://sure.example.lan:3000 -e SURE_API_KEY=your-sure-api-key \
  -e DRY_RUN=true -e SOURCES=amazon -e TZ=America/New_York \
  ghcr.io/charlesz54/sure-itemizer:latest
```

**Unraid:** a ready-made template is in [`unraid/sure-itemizer.xml`](unraid/sure-itemizer.xml).

Then open `http://<host>:8686`, connect your merchants, and check the planned changes before setting `DRY_RUN=false`.

## Notifications

Notifications go through [Apprise](https://github.com/caronc/apprise), so they can land in Discord, ntfy, Pushover, Telegram, Home Assistant, email, a plain JSON webhook and [many more](https://github.com/caronc/apprise/wiki). Set `APPRISE_URL` to one or more Apprise URLs, separated by commas:

```
APPRISE_URL=discord://webhook_id/webhook_token
APPRISE_URL=ntfys://my-topic, mailtos://user:pass@example.com
```

You'll hear about it when a merchant needs you to sign in again, and when a run fails, once per problem until it clears. Messages carry the status page link (`PUBLIC_URL`). **Integrations → Notifications → Send test** checks the setup. If a notification doesn't arrive, the container log has Apprise's own message about it. Those messages can include the URL, token included, so keep the log private.

## Configuration

| Variable | Default | |
|---|---|---|
| `SURE_URL` | — | e.g. `http://192.168.1.10:3000` |
| `SURE_API_KEY` | — | `read_write` scope |
| `DRY_RUN` | `true` | Plan and show what *would* change; write nothing |
| `SOURCES` | `amazon` | Comma-separated: `amazon`, `target`, `venmo`, `ebay` |
| `LOOKBACK_DAYS` | `30` | How far back to look for unprocessed transactions |
| `SCHEDULE` | `30 7 * * *` | When to run, as a cron expression (minute hour day-of-month month day-of-week) in `TZ`; e.g. `0 7,19 * * *` for 07:00 and 19:00 |
| `RUN_ON_START` | `true` | |
| `WEB_PORT` | `8686` | Status page |
| `TZ` | `UTC` | Time zone for `SCHEDULE`, e.g. `America/New_York` |
| `APPRISE_URL` | — | Where to send notifications (Apprise URLs, comma-separated) |
| `PUBLIC_URL` | — | How you reach the status page; linked from notifications |

Merchant choices (such as which Amazon site) are made on each merchant's Connect page, not in variables.

## Commands

```sh
docker exec -it sure-itemizer itemizer login amazon   # sign in from a console instead of the web UI
docker exec sure-itemizer itemizer run                # one run now
docker exec sure-itemizer itemizer status             # what was planned/applied
docker exec sure-itemizer itemizer probe target       # record Target's own order data (development)
```

## Adding a merchant

A source is a subclass of `itemizer.sources.base.Source` that sets:

- `name`
- `descriptor`: a regex for the bank description
- `sure_search`
- `date_window`

and implements `fetch(since, amounts) -> list[Purchase]`. Each `Purchase` is one charge, with `Line`s for what it paid for. Matching, apportioning, planning and writing to Sure are shared. Register the source in `itemizer/sources/__init__.py`.

If the merchant needs a sign-in, also set `login_fields` and implement `login(fields, io)`, `logout()` and `connected()`. The web UI then gives the source its own Connect page: `io.prompt(...)` inside `login` asks the user (for a code, a choice, a captcha) and returns their answer. `fetch` should raise `LoginRequired` rather than sign in unattended. The runner turns that into the *Sign-in needed* badge and a single notification.

Add a guide for it in `docs/<merchant>.md`, link it from the [Merchants](#merchants) table, and point the source's `help_url` at it so its Connect page links there too.

## Status page statuses

| Status | Meaning |
|---|---|
| planned | Dry run: this is what would happen |
| applied | Done |
| deferred | Waiting on something: Sure's split API, or a pending card transaction |
| already split | You or Sure split it already; left alone |
| skipped | Matched, but the items couldn't be determined; the reason is shown |
| unmatched | No merchant charge with this amount near this date |
| error | Sure rejected the write; the reason is shown, and it's retried next run |

## License

MIT
