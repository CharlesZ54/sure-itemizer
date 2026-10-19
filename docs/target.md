# Target

> **In progress:** signing in to Target works, but reading Target's receipts isn't built yet, so Target transactions show as *unmatched* for now.

The goal: purchases made with a Target Circle Card (debit or credit) are split into the items on the receipt. Target links those purchases to your account, in-store ones included. In the bank they show up as `TARGET DEBIT CRD` pulls a day or few after the purchase.

Add `target` to `SOURCES`.

## Connecting

**Integrations → Target → Connect** works like Amazon's: type your Target email and password, and the service signs in on Target's real page in the container's browser, preferring the password when Target also offers a code. If Target sends a one-time code, you're asked where to send it and then for the code.

Two things are never scripted, and come to you instead through a **live view** of that same browser:

- **Target's "Press & hold" check** (bot protection). You hold the button in the live view (mouse or finger) until the checkmark appears, then press **Continue**. Automating that check is not something this does.
- **Any step the script doesn't recognize.** You finish it in the live view and press **Continue**, and the script carries on.

The live view is also available on its own from the Connect page (*Or sign in yourself…*).

Only the browser's session is kept (`/data/target/state.json`, mode 600). Your email and password are used once and thrown away.

If Target keeps answering the check with "Please try again", stop and wait a day or two before trying again: repeated attempts make its bot protection stricter.

## Settings

| Variable | Default | |
|---|---|---|
| `TARGET_SURE_ACCOUNT` | — | Only look at this Sure account (its id, or part of its name) for Target |
