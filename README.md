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
- **An Amazon login.** If you use two-step verification, give the service the authenticator secret (`AMZN_OTP_SECRET`) so it can log in unattended. To get one, add a new authenticator app in Amazon's *Login & security → Two-step verification* and copy the key behind "Can't scan the barcode?". The session is saved in `/data/amazon/cookies.json`, so logins are rare.

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
| `AMZN_USERNAME` / `AMZN_PASSWORD` | — | |
| `AMZN_OTP_SECRET` | — | Authenticator secret, spaces allowed |
| `AMZN_CARD_LAST4` | — | Only use Amazon charges paid with this card |
| `AMZN_SURE_ACCOUNT` | — | Only look at this Sure account (id or part of its name) |
| `AMZN_DOMAIN` | `amazon.com` | Other Amazon marketplaces are untested |

The Amazon variables are `AMZN_*`, not `AMAZON_*`, because the `amazon-orders` library reads every `AMAZON_*` variable as its own setting.

## Commands

```sh
docker exec sure-itemizer itemizer login    # check the Amazon login and save the session
docker exec sure-itemizer itemizer run      # one run now
docker exec sure-itemizer itemizer status   # what was planned/applied
```

## Adding a merchant

A source is a subclass of `itemizer.sources.base.Source` that sets:

- `name`
- `descriptor`: a regex for the bank description
- `sure_search`
- `date_window`

and implements `fetch(since, amounts) -> list[Purchase]`. Each `Purchase` is one charge, with `Line`s for what it paid for. Matching, apportioning, planning and writing to Sure are shared. Register the source in `itemizer/sources/__init__.py`.

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
