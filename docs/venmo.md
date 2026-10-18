# Connecting Venmo

sure-itemizer reads your Venmo payments through Venmo's **unofficial** mobile API, the one the [venmo-api](https://github.com/mmohades/Venmo) Python library uses. It only ever reads; there is no code in this project that can move money. The Connect page (**Integrations → Venmo → Connect**) accepts either:

- **a Venmo access token** ([how to get one](#getting-an-access-token)), or
- **your email or phone, your password, and a trusted device ID** ([how to get the device ID](#getting-a-trusted-device-id-v_id)). If Venmo then texts you a code, the page asks for it.

Only the resulting token is stored (`/data/venmo/auth.json`, readable only by the container). Your password is used once and thrown away.

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
5. Paste it into **Trusted device ID** on the Connect page, along with your email or phone and password.

If you can't find `v_id`, open the *Network* tab, reload the page or sign in again, and look in the response headers of the sign-in request for `Set-Cookie: v_id=…`.

## Getting an access token

You only need a token if you'd rather not type your password into the Connect page, or you already have one from another Venmo tool. **Treat a token like your password:** it doesn't expire, and it can do anything the Venmo app can, including sending money. Don't share it, and don't commit it anywhere.

### Option 1: let sure-itemizer get one (recommended)

Use the email, password and device ID fields on the Connect page. The token it gets stays inside the container.

### Option 2: with the venmo-api library

On your own computer, with Python 3:

```sh
pip install venmo-api==0.3.1
python -c "
from venmo_api import Client
token = Client.get_access_token(
    username='you@example.com',        # email, phone (no +1) or username
    password='your password',
    device_id='YOUR_V_ID',             # the trusted device ID from above
)
"
```

It sends the sign-in. If Venmo texts you a code, it asks for it in the terminal, then prints `access_token: …`. Paste that value into **access token** on the Connect page. The `device-id` it also prints is the one you gave it.

Avoid running this repeatedly if it fails: each failed attempt counts against the account. Check the device ID first.

### Option 3: from a browser session (less reliable)

People in [mmohades/Venmo#86](https://github.com/mmohades/Venmo/issues/86) report finding a token in the browser: with the developer tools' *Network* tab open, sign in at account.venmo.com, then look in the response of the first request to `https://account.venmo.com/` for `graphqlHeaderConfig`, which contains an access token. Tokens taken this way have been reported to expire, and some people got errors using them with the mobile API, so prefer options 1 or 2.

### Reusing a token from another tool

If another Venmo tool already has a token for your account (for example an auto-cashout script), you can paste the same token here. Both keep working. **Disconnect** in sure-itemizer only deletes its own copy and never revokes the token.

## Revoking a token

A token stays valid until it's revoked. To revoke one, for example if it leaked:

```sh
pip install venmo-api==0.3.1
python -c "from venmo_api import Client; Client.log_out('Bearer YOUR_TOKEN')"
```

This also breaks any other tool using the same token.
