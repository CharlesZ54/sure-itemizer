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
- **An account with at least one supported merchant** (Amazon, Target, eBay, Venmo), connected from the web UI (below).
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

## Connecting Amazon

Open the status page, go to **Integrations → Amazon → Connect**, and sign in. If Amazon asks where to send a code, or for the 6-digit code itself (or, rarely, a captcha), the page prompts you. The sign-in happens on Amazon's real sign-in page, in a headless Chromium inside the container, because Amazon rejects password submissions that don't come from a real browser, even when the password is right. Chromium runs only while you sign in, which is why the image carries a browser and the container should be allowed about 1 GB of memory.

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

## Connecting eBay

eBay charges are split into the items in the order, like Amazon's. The bank line quotes eBay's order number (`EBAY O*12-34567-89012`), so each charge goes to its own order.

**Step-by-step: [docs/ebay.md](docs/ebay.md).** eBay only opens its API to registered developers, so this needs a free eBay developer account and a production keyset of your own: you paste its **App ID**, **Cert ID** and **RuName** into **Integrations → eBay → Connect**, approve access on eBay's page, and paste back the address eBay sends you to. The keys and eBay's refresh token (about 18 months) are kept in `/data/ebay/auth.json`, mode 600.

Orders come from the Trading API's `GetOrders` as a buyer, which covers the last 90 days. Shipping and tax are spread over the items by price. A refund of a whole order, or on a single-item order, is split the same way; a partial refund on a multi-item order is only renamed, since eBay doesn't say which item it was for. Orders paid with PayPal show up in the bank as PayPal and aren't matched.

## Connecting Venmo

Payments are renamed, with the bank's original text kept in the notes. Cash-outs are split into the payments behind them.

- **A payment you made from your bank** (`VENMO PAYMENT`) becomes *"Venmo to Alex Doe: pizza"*. That includes requests from others that you paid.
- **A cash-out** (`VENMO CASHOUT`) is split into the payments received since the previous cash-out (*"Venmo from Sam Roe: dinner"*), less the payments you made from your Venmo balance in that time (*"Venmo to Alex Doe: pizza"*), so Sure's AI categorizes each one. If money was already in the balance, or some was left there, a *"Venmo balance from before"* or *"Left in the Venmo balance"* line makes the parts add up; an instant transfer's fee gets a line too. Like Amazon splits, this needs Sure's split API (v0.7.6). A payment counts as paid from the balance when the bank has no Venmo pull of the same amount around its date.

**Step-by-step: [docs/venmo.md](docs/venmo.md)**, covering the trusted device ID (`v_id`) and revoking access.

**Integrations → Venmo → Connect** uses Venmo's unofficial mobile API, the same one the [venmo-api](https://github.com/mmohades/Venmo) library uses. Sign in with your email or phone, your password and a **trusted device ID**. Venmo refuses sign-ins from devices it doesn't know, and repeated attempts can lock the account ([mmohades/Venmo#86](https://github.com/mmohades/Venmo/issues/86)), so the form won't try without one. The ID is the `v_id` cookie of a browser you've signed in to Venmo with: venmo.com → your browser's developer tools → Application/Storage → Cookies. If Venmo then texts a code, you enter it on the page. Only the token is kept (`/data/venmo/auth.json`, mode 600); it doesn't expire until revoked in Venmo. The service only ever reads with it. **Disconnect** forgets the token but doesn't revoke it; docs/venmo.md shows how.

Optionally, drop Venmo statement CSVs (venmo.com → Statements → Download CSV) into `/data/venmo/statements/`. They say how each payment was funded, so the bank-or-balance guess isn't needed for those months.

The API is unofficial: Venmo can change it at any time, and unusual access could draw attention to the account.

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
| `RUN_TIMES` | `07:30` | Comma-separated local times (`TZ`) |
| `RUN_ON_START` | `true` | |
| `WEB_PORT` | `8686` | Status page |
| `TZ` | `UTC` | Time zone for `RUN_TIMES`, e.g. `America/New_York` |
| `APPRISE_URL` | — | Where to send notifications (Apprise URLs, comma-separated) |
| `PUBLIC_URL` | — | How you reach the status page; linked from notifications |
| `AMZN_DOMAIN` | `amazon.com` | Other Amazon marketplaces are untested |
| `TARGET_SURE_ACCOUNT` | — | Only look at this Sure account for Target |
| `VENMO_SURE_ACCOUNT` | — | Only look at this Sure account for Venmo |
| `EBAY_SURE_ACCOUNT` | — | Only look at this Sure account for eBay |

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
