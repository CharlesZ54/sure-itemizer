# sure-itemizer

Splits merchant charges in [Sure](https://github.com/we-promise/sure) into the items they actually paid for, so Sure's AI categorizer can categorize each item instead of guessing at "Amazon".

A $30.75 `AMAZON MKTPL*EXAMPLE01` charge becomes:

| Split | Amount |
|---|---|
| USB-C cable, 6 ft | $21.21 |
| Toothpaste (x3) | $9.54 |

The split rows are left uncategorized, so Sure's **Auto categorize** rule picks them up like any other transaction.

## How it works

1. **Find candidates in Sure.** It lists recent transactions whose name matches a merchant (`amazon|amzn`). If there are none, it never contacts the merchant.
2. **Fetch what the merchant charged.** For Amazon, it reads *Your Account → Transactions*, which lists every card charge with its order number, and opens only the orders whose charge amounts match a waiting Sure transaction.
3. **Work out which items each charge paid for.** Amazon charges per *shipment*. Each shipment's subtotal is grossed up by the order's tax, shipping and discount factor, and the service looks for the one way of assigning shipments to charges that fits every charge. If two different assignments fit, it reports the charge as ambiguous and doesn't guess.
4. **Match charges to Sure transactions.** A match needs the exact amount, with the bank date between 2 days before and 5 days after Amazon's date. Each charge matches at most one transaction, and the closest date wins.
5. **Split it in Sure** (`POST /api/v1/transactions/:id/split`), one row per item. The charge is apportioned by item price, so tax and shipping land proportionally, and the rows always add up to the cent.
   - **One item:** still a one-row split. By the time the service sees a charge, Sure's AI rule has usually already categorized and locked it from the bare merchant name. A fresh, uncategorized row named after the item is what gets it categorized again with real information.
   - **Refunds:** handled only when the order had a single product, because Amazon doesn't say which item came back.

Everything it decides is recorded in `/data/itemizer.db` and shown on a small status page. Once a transaction is finished, it's never touched again, and neither are the split rows it created.

## Requirements

- **Sure v0.7.6 or newer**; the split API arrived in [PR #4117](https://github.com/we-promise/sure/pull/4117). On older versions everything stays *deferred* (or *planned* in a dry run) and nothing is written.
- **Something in Sure that categorizes the new rows**, such as a rule with the *Auto categorize* action.
- **A Sure API key** with `read_write` scope (*Settings → API keys*).
- **An Amazon account,** connected from the web UI (below).

## Connecting Amazon

Open the status page, go to **Integrations → Amazon → Connect**, and sign in. If Amazon asks where to send a code, or for the 6-digit code itself (or, rarely, a captcha), the page prompts you. The sign-in happens on Amazon's real sign-in page, in a headless Chromium inside the container, because Amazon rejects password submissions that don't come from a real browser, even when the password is right. Chromium runs only while you sign in, which is why the image carries a browser and the template allows 1 GB of memory.

- **Your email and password are used once and thrown away.** They aren't configuration, and they aren't written anywhere. The only thing kept is the session Amazon issues (`/data/amazon/cookies.json`, mode 600), the same thing your browser keeps when you tick "Keep me signed in".
- **No authenticator secret is ever needed.** Two-step verification keeps working as intended.
- **Scheduled runs never sign in on their own.** When Amazon ends the session, the run stops, the page shows *Sign-in needed*, and you get **one** notification (`APPRISE_URL`). Click **Reconnect** to sign in again, and it catches up straight away.

The page is plain HTTP, so your Amazon password crosses your LAN unencrypted when you connect. If that matters, reach it over HTTPS, for example with `tailscale serve`.

## Connecting Target

**Integrations → Target → Connect** works like Amazon's: type your Target email and password, and the service signs in on Target's real page in the container's browser, preferring the password when Target also offers a code. If Target sends a one-time code, you're asked where to send it and then for the code.

Two things are never scripted, and come to you instead through a **live view** of that same browser:

- **Target's "Press & hold" check** (bot protection). You hold the button in the live view (mouse or finger), then press **Continue**. Automating that check is not something this does.
- **Any step the script doesn't recognize.** You finish it in the live view and press **Continue**, and the script carries on.

Only the browser's session is kept (`/data/target/state.json`, mode 600). Target links purchases made with a Target Circle Card to your account, in-store ones included. In the bank they show up as `TARGET DEBIT CRD` pulls a day or few after the purchase.

The live view is also available on its own from either Connect page (*Or sign in yourself…*).

## Notifications

Notifications go through [Apprise](https://github.com/caronc/apprise), so they can land in Discord, ntfy, Pushover, Telegram, Home Assistant, email, a plain JSON webhook and [many more](https://github.com/caronc/apprise/wiki). Set `APPRISE_URL` to one or more Apprise URLs, separated by commas:

```
APPRISE_URL=discord://webhook_id/webhook_token
APPRISE_URL=ntfys://my-topic, mailtos://user:pass@example.com
```

You'll hear about it when a merchant needs you to sign in again, and when a run fails, once per problem until it clears. Messages carry the status page link (`PUBLIC_URL`). **Integrations → Notifications → Send test** checks the setup. Apprise's own logging is silenced, because it would print the URLs, tokens included. If a URL can't be parsed, the log says so without showing it.

## Configuration

| Variable | Default | |
|---|---|---|
| `SURE_URL` | — | e.g. `http://192.168.1.10:3000` |
| `SURE_API_KEY` | — | `read_write` scope |
| `DRY_RUN` | `true` | Plan and show what *would* change; write nothing |
| `SOURCES` | `amazon` | Comma-separated |
| `LOOKBACK_DAYS` | `30` | How far back to look for unprocessed transactions |
| `RUN_TIMES` | `07:30` | Comma-separated local times (`TZ`) |
| `RUN_ON_START` | `true` | |
| `WEB_PORT` | `8686` | Status page |
| `APPRISE_URL` | — | Where to send notifications (Apprise URLs, comma-separated) |
| `PUBLIC_URL` | — | How you reach the status page; linked from notifications |
| `AMZN_CARD_LAST4` | — | Only use Amazon charges paid with this card |
| `AMZN_SURE_ACCOUNT` | — | Only look at this Sure account (id or part of its name) |
| `AMZN_DOMAIN` | `amazon.com` | Other Amazon marketplaces are untested |
| `TARGET_SURE_ACCOUNT` | — | Only look at this Sure account for Target |

The Amazon variables are `AMZN_*`, not `AMAZON_*`, because the `amazon-orders` library reads every `AMAZON_*` variable as its own setting.

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

## Caveats

- Amazon has no API for your own orders. This logs in as you and reads the same pages your browser does, using [amazon-orders](https://github.com/alexdlaird/amazon-orders), so it can break when Amazon changes its site. It's polite about it: one run a day by default, and only the orders it needs.
- Digital orders, Whole Foods and Fresh orders may lack item prices; those are reported as skipped rather than guessed.

## License

MIT
