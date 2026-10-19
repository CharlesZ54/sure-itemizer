# Amazon

Each Amazon card charge is split into the items it paid for. A $30.75 `AMAZON MKTPL*EXAMPLE01` charge becomes:

| Split | Amount |
|---|---|
| USB-C cable, 6 ft | $21.21 |
| Toothpaste (x3) | $9.54 |

Add `amazon` to `SOURCES` (it's the default).

## Connecting

Open the status page, go to **Integrations → Amazon → Connect**, choose your **Amazon site** (amazon.com, amazon.co.uk, amazon.de, …), and sign in. The site is remembered for later runs and reconnects; only amazon.com has been tested. If Amazon asks where to send a code, or for the 6-digit code itself (or, rarely, a captcha), the page prompts you. The sign-in happens on Amazon's real sign-in page, in a headless Chromium inside the container, because Amazon rejects password submissions that don't come from a real browser, even when the password is right. Chromium runs only while you sign in, which is why the image carries a browser and the container should be allowed about 1 GB of memory. If Amazon shows a page the script doesn't recognize, you can finish signing in yourself through a live view of that browser (*Or sign in yourself…* on the Connect page).

- **Your email and password are used once and thrown away.** They aren't configuration, and they aren't written anywhere. The only thing kept is the session Amazon issues (`/data/amazon/cookies.json`, mode 600), the same thing your browser keeps when you tick "Keep me signed in".
- **No authenticator secret is ever needed.** Two-step verification keeps working as intended.
- **Scheduled runs never sign in on their own.** When Amazon ends the session, the run stops, the page shows *Sign-in needed*, and you get **one** notification. Click **Reconnect** to sign in again, and it catches up straight away.

The status page is plain HTTP, so your Amazon password crosses your network unencrypted when you connect. If that matters, put the page behind HTTPS (a reverse proxy, or for example `tailscale serve`).

You can also sign in from a terminal: `docker exec -it sure-itemizer itemizer login amazon`.

## How charges are worked out

1. **Find what Amazon charged.** It reads *Your Account → Transactions*, which lists every card charge with its order number, and opens only the orders whose charge amounts match a waiting Sure transaction.
2. **Work out which items each charge paid for.** Amazon charges per *shipment*. Each shipment's subtotal is grossed up by the order's tax, shipping and discount factor, and the service looks for the one way of assigning shipments to charges that fits every charge. If two different assignments fit, it reports the charge as ambiguous and doesn't guess.
3. **Match and split.** A match needs the exact amount, with the bank date between 2 days before and 5 days after Amazon's date. The charge is apportioned by item price, so tax and shipping land proportionally.

- **One item:** still a one-row split, so Sure's AI categorizes it from the item's name rather than from "Amazon".
- **Refunds:** handled only when the order had a single product, because Amazon doesn't say which item came back.

## Caveats

- Amazon has no API for your own orders. This logs in as you and reads the same pages your browser does, using [amazon-orders](https://github.com/alexdlaird/amazon-orders), so it can break when Amazon changes its site. It's polite about it: one run a day by default, and only the orders it needs.
- Digital orders, Whole Foods and Fresh orders may lack item prices; those are reported as skipped rather than guessed.
