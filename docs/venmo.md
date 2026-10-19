# Venmo

Payments are renamed, with the bank's original text kept in the notes. Cash-outs are split into the payments behind them. Add `venmo` to `SOURCES`.

- **A payment you made from your bank** (`VENMO PAYMENT`) becomes *"Venmo to Alex Doe: pizza"*. That includes requests from others that you paid.
- **A cash-out** (`VENMO CASHOUT`) is split into the payments received since the previous cash-out (*"Venmo from Sam Roe: dinner"*), less the payments you made from your Venmo balance in that time (*"Venmo to Alex Doe: pizza"*), so Sure's AI categorizes each one. If money was already in the balance, or some was left there, a *"Venmo balance from before"* or *"Left in the Venmo balance"* line makes the parts add up; an instant transfer's fee gets a line too. Cash-out splits need Sure v0.7.6 or newer; renames work on any version.
- A payment counts as paid from the balance when the bank has no Venmo pull of the same amount around its date. For exact answers, drop Venmo statement CSVs (venmo.com → Statements → Download CSV) into `/data/venmo/statements/`: they say how each payment was funded.

The API is unofficial: Venmo can change it at any time, and unusual access could draw attention to the account.

## Connecting

sure-itemizer reads your Venmo payments through Venmo's **unofficial** mobile API, the one the [venmo-api](https://github.com/mmohades/Venmo) Python library uses. It only ever reads; there is no code in this project that can move money. The Connect page (**Integrations → Venmo → Connect**) asks for your email or phone, your password, and a **trusted device ID** ([how to get it](#getting-a-trusted-device-id-v_id)). If Venmo then texts you a code, the page asks for it.

Only the access token Venmo issues is stored (`/data/venmo/auth.json`, readable only by the container), with the device ID. Your password is used once and thrown away. When reconnecting, the device ID field can be left blank to reuse the saved one.

> **Why a device ID?** Since early 2024, Venmo refuses sign-ins from devices it doesn't recognize, answering *"OAuth2 Exception: Unable to complete your request"* (error 240). Repeated failed attempts have locked people out of Venmo entirely, including the official app ([mmohades/Venmo#86](https://github.com/mmohades/Venmo/issues/86)). Signing in with the ID of a device Venmo already trusts avoids this. So the Connect page won't send your password without one.

## Getting a trusted device ID (`v_id`)

Your browser has one: when you sign in at venmo.com, Venmo sets a cookie named **`v_id`**, and its value is a device ID Venmo knows. Use a browser where you've signed in to Venmo before. If Venmo asks whether to remember or trust the browser, say yes.

1. Sign in at **https://account.venmo.com** in a desktop browser.
2. Open the developer tools: **F12**, or **Cmd+Option+I** on a Mac.
3. Find the cookies:
   - **Chrome / Edge / Brave:** *Application* tab → *Storage* → *Cookies* → `https://account.venmo.com` (if it's not there, check `https://venmo.com`).
   - **Firefox:** *Storage* tab → *Cookies* → `https://account.venmo.com`.
   - **Safari:** enable *Settings → Advanced → Show features for web developers* first, then *Develop → Show Web Inspector* → *Storage* tab → *Cookies*.
4. Copy the **Value** of the cookie named **`v_id`**. It looks like a long string of letters and digits, sometimes with dashes.
5. Paste it into **Trusted device ID** on the Connect page, along with your email or phone and password, and choose **Sign in**.

If you can't find `v_id`, open the *Network* tab, reload the page or sign in again, and look in the response headers of the sign-in request for `Set-Cookie: v_id=…`.

## Revoking access

The token stays valid until it's revoked; **Disconnect** only deletes sure-itemizer's copy. To revoke it, for example if it leaked, run this on the machine running the container (it reads the saved token and asks Venmo to sign it out):

```sh
docker exec sure-itemizer python -c "
import json, requests
token = json.load(open('/data/venmo/auth.json'))['token']
r = requests.delete('https://api.venmo.com/v1/oauth/access_token', headers={'Authorization': 'Bearer ' + token})
print('revoked' if r.ok else f'Venmo said {r.status_code}')
"
```

Then choose **Disconnect** (or **Reconnect** to get a new token).

## Settings

| Variable | Default | |
|---|---|---|
| `VENMO_SURE_ACCOUNT` | — | Only look at this Sure account (its id, or part of its name) for Venmo |
