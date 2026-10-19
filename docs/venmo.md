# Connecting Venmo

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
