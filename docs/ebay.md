# Connecting eBay

sure-itemizer reads your eBay purchases through eBay's **official** API, which splits each eBay charge into the items in the order. eBay only gives API access to registered developers, so you'll set up a free developer account and an "application" of your own once (about 15 minutes, plus eBay's approval). Nobody else's keys are involved: your keys only ever reach your own eBay account.

What you'll end up with, and paste into the Connect page:

| Field on the Connect page | Where it comes from |
|---|---|
| App ID (Client ID) | Your production keyset |
| Cert ID (Client Secret) | Your production keyset |
| RuName (eBay Redirect URL name) | The redirect you create in step 4 |

eBay's developer portal changes now and then. If a label below doesn't match exactly, look for the nearest equivalent.

## 1. Join the eBay Developers Program

1. Go to **https://developer.ebay.com/signin**. Sign in if you already have a developer account; otherwise choose the option there to register for one.
2. Fill in the form. A developer account is separate from your eBay buying account; using the same email is fine.
3. Confirm your email. eBay reviews new developer accounts, which can take up to a business day. You'll get an email when it's approved.

## 2. Create a production keyset

1. Once approved, open **https://developer.ebay.com/my/keys** (or your name in the top right → **Application Keysets**).
2. Enter an **Application Title**, for example `sure-itemizer`.
3. In the **Production** column, choose **Create a keyset**. (Sandbox keys won't work: the sandbox is a separate, fake eBay with none of your orders.)

## 3. Clear the account-deletion requirement

eBay keeps a new production keyset disabled until you deal with **Marketplace Account Deletion notifications**. eBay asks every app to either receive a message when an eBay user closes their account (so the app can delete that user's data), or opt out. The portal shows this as an alert on the keys page; follow it to **Alerts & Notifications**.

- **Opt out:** eBay intends this for apps that don't persist eBay user data. Be aware that sure-itemizer keeps your order item names in its own database and writes them into Sure. It only ever handles *your own* account, so these notifications could only ever concern you closing your own eBay account, but whether opting out fits eBay's terms is your call.
- **Subscribe:** requires a public HTTPS endpoint that answers eBay's challenge. sure-itemizer doesn't provide one.

When this step is done, the production keyset turns active.

## 4. Create the redirect (RuName)

The RuName tells eBay where to send your browser after you approve access. You'll copy the address of that page back into sure-itemizer, so eBay's default page is all you need.

1. On the keys page, next to your **Production** keyset, choose **User Tokens**.
2. At the top, make sure **OAuth** is selected (not *Auth'n'Auth*).
3. Under **Get a Token from eBay via Your Application**, choose **Add eBay Redirect URL**.
4. Fill in:
   - **Display Title:** shown on eBay's approval page, e.g. `sure-itemizer`.
   - **Privacy Policy URL:** required. Any page you control works, e.g. `https://github.com/CharlesZ54/sure-itemizer`.
   - **Your auth accepted URL / Your auth declined URL:** leave eBay's defaults.
5. **Save**. Copy the value labelled **RuName (eBay Redirect URL name)**. It looks like `Pat_Example-PatExamp-sureit-abcdefgh`, not like a web address.

## 5. Copy the keys

Back on **Application Keysets**, from the **Production** keyset copy:

- **App ID (Client ID)**, e.g. `PatExamp-sureitem-PRD-1a2b3c4d5-6e7f8a9b`
- **Cert ID (Client Secret)**, e.g. `PRD-1a2b3c4d5e6f-7a8b-9c0d-1e2f-3a4b`

The **Dev ID** isn't needed. Treat the Cert ID like a password.

## 6. Connect in sure-itemizer

1. Add `ebay` to the container's **SOURCES** variable (e.g. `amazon,target,venmo,ebay`) and apply.
2. Open the status page → **Integrations → eBay → Connect**. The **How to get these →** link there leads back to this page.
3. Paste the App ID, Cert ID and RuName, then **Sign in**.
4. Choose **Open the sign-in page ↗**. In the new tab, sign in with **the eBay account you buy with** and choose **Agree**.
5. eBay sends you to a page saying the authorization succeeded (it may say you can close the window). **Copy that page's entire address** from the address bar. It contains `code=`.
6. Back on the Connect page, paste the address and **Continue** within about 5 minutes (eBay's code expires quickly).

The status page should show **eBay · Connected**, and the next run reads your orders.

## What gets stored

`/data/ebay/auth.json`, readable only by the container, holds your App ID, Cert ID, RuName and the **refresh token** eBay issued. The refresh token lasts about 18 months; the short-lived access tokens made from it are never written to disk. When it expires, you get one notification and the card shows **Sign-in needed**: choose **Reconnect**, leave the three fields blank to reuse the saved keys, and repeat steps 6.4–6.6.

**Disconnect** deletes the file. To also cut off the app on eBay's side, remove it in your eBay account settings under the permissions for third-party apps, or delete the keyset in the developer portal.

## Limits

- **90 days.** eBay's API only returns orders from the last 90 days, plenty for the default 30-day lookback.
- **PayPal-paid orders** show up in the bank as PayPal, not eBay, so they aren't matched.
- **Refunds:** a refund on a single-item order, or of the whole order, is split like the charge. A partial refund on a multi-item order is only renamed (*"eBay refund: USB cable, Phone case"*), because eBay doesn't say which item it was for.
- **Shipping and tax** are spread over an order's items by price.

## Troubleshooting

| Message | Likely cause |
|---|---|
| `client authentication failed` | Wrong Cert ID, or Sandbox keys instead of Production |
| `the provided authorization grant is invalid` | The pasted address is older than ~5 minutes, was already used, or came from a different RuName. Start again |
| An error saying the client or keyset isn't authorized or is disabled | Step 3 isn't finished |
| `that doesn't look like the address eBay sent you to` | Paste the whole address of the page after **Agree**, including `code=` |
| `eBay says the sign-in was declined` | You chose **Disagree** on eBay's page |
