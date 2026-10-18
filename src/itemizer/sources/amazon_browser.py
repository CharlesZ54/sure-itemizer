"""Sign in to Amazon entirely inside headless Chromium.

Submitting the sign-in form over plain HTTP gets "Your email or password was
incorrect" even with the right password: Amazon's page adds a fingerprint
with JavaScript, and a submission without it is treated as a bot. So the
whole sign-in runs on Amazon's real page in a real (headless) browser. Every
question Amazon asks along the way -- which device gets the code, the code
itself, a captcha, "approve on your phone" -- is relayed to the user through
`io`, and the cookies the browser ends up with become the session.

It reuses amazon-orders' own browser plumbing (cookie handover, session
check) from PlaywrightBrowserLogin, which does the same with a visible
window and a person typing.
"""
from __future__ import annotations

import base64
import logging
import time
from urllib.parse import urlencode

from amazonorders.contrib.browser.playwright import PlaywrightBrowserLogin

log = logging.getLogger(__name__)

SIGN_IN_TIMEOUT = 300  # seconds for the whole sign-in, including the user's answers
STUCK_AFTER = 25  # seconds on an unrecognized page before giving up

EMAIL = "input[name='email']:visible"
PASSWORD = "input[name='password']:visible"
ERROR = "#auth-error-message-box:visible, .a-alert-error:visible .a-alert-content"
DEVICE_CHOICE = "input[name='otpDeviceContext']"
OTP = ("input[name='otpCode']:visible, input#auth-mfa-otpcode:visible, input#cvf-input-code:visible, "
       "form#verification-code-form input[name='code']:visible")
REMEMBER_DEVICE = "input#auth-mfa-remember-device, input[name='rememberDevice']"
CAPTCHA_IMAGE = "#auth-captcha-image:visible, img[src*='captcha']:visible"
CAPTCHA_INPUT = ("input#auth-captcha-guess:visible, input[name='cvf_captcha_input']:visible, "
                 "input[id^='captchacharacters']:visible, input[name='guess']:visible")
# "We sent a notification, approve it" pages poll on their own until approved.
APPROVAL_TEXT = ("approve the notification", "approve this sign-in", "approve the sign-in", "check your phone",
                 "sent you a notification", "we sent an email")
# Interstitials that only want to sell us a passkey or a phone number.
SKIP = ("#ap-account-fixup-phone-skip-link, a#ap-account-fixup-phone-skip-link, a:has-text('Not now'), "
        "button:has-text('Not now'), a:has-text('Skip'), button:has-text('Skip'), "
        "input[type='submit'][value*='Not now' i]")
SUBMIT = ("input#signInSubmit, input#continue, #auth-send-code, #auth-signin-button, "
          "input[type='submit']:visible, button[type='submit']:visible")


class SignInError(RuntimeError):
    pass


class HeadlessSignIn(PlaywrightBrowserLogin):
    def __init__(self, amazon_session, io) -> None:
        super().__init__(amazon_session)
        self.headless = True
        self.io = io

    def sign_in(self, email: str, password: str, timeout: int = SIGN_IN_TIMEOUT) -> None:
        from playwright.sync_api import Error as PlaywrightError, sync_playwright

        constants = self.config.constants
        url = f"{constants.SIGN_IN_URL}?{urlencode(constants.SIGN_IN_QUERY_PARAMS)}"
        with sync_playwright() as pw:
            # Full Chromium in its new headless mode, not the stripped-down
            # headless shell, and without the automation flag sites look for.
            browser = pw.chromium.launch(headless=True, channel="chromium",
                                         args=["--disable-blink-features=AutomationControlled"])
            try:
                # Same browser identity the session will use afterwards, so
                # Amazon accepts the cookies outside the browser too.
                context = browser.new_context(user_agent=constants.BASE_HEADERS.get("User-Agent"),
                                              locale="en-US", viewport={"width": 1280, "height": 900})
                page = context.new_page()
                page.goto(url, wait_until="domcontentloaded")
                self._drive(context, page, email, password, timeout)
                page.wait_for_timeout(2000)  # let the last redirects set their cookies
                self._harvest_cookies(context)
            except PlaywrightError as e:
                raise SignInError(f"the browser stopped: {e}") from e
            finally:
                browser.close()
        self._verify_session()

    def _drive(self, context, page, email: str, password: str, timeout: int) -> None:
        deadline = time.monotonic() + timeout
        password_tries = 0
        told_to_approve = False
        last_change = time.monotonic()
        last_url = None

        while time.monotonic() < deadline:
            if self._is_signed_in(context.cookies()):
                return
            page.wait_for_timeout(700)
            if page.url != last_url:
                last_url, last_change = page.url, time.monotonic()

            error = _text(page, ERROR)
            if error:
                raise SignInError(f"Amazon says: {error}")

            if _visible(page, CAPTCHA_IMAGE) and _visible(page, CAPTCHA_INPUT):
                image = "data:image/png;base64," + base64.b64encode(
                    page.locator(CAPTCHA_IMAGE).first.screenshot()).decode()
                answer = self.io.prompt("Enter the characters shown in the image", img_url=image)
                if _visible(page, PASSWORD):  # Amazon asks for the password again with its captcha
                    page.locator(PASSWORD).first.fill(password)
                page.locator(CAPTCHA_INPUT).first.fill(answer)
                _submit(page, CAPTCHA_INPUT)
                last_change = time.monotonic()
                continue

            if _visible(page, PASSWORD):
                password_tries += 1
                if password_tries > 2:
                    raise SignInError("Amazon keeps asking for the password; check it and try again")
                if _visible(page, EMAIL) and not page.locator(EMAIL).first.input_value():
                    page.locator(EMAIL).first.fill(email)
                page.locator(PASSWORD).first.fill(password)
                _check(page, "input[name='rememberMe']")
                _submit(page, PASSWORD)
                last_change = time.monotonic()
                continue

            if _visible(page, EMAIL):
                page.locator(EMAIL).first.fill(email)
                _submit(page, EMAIL)
                last_change = time.monotonic()
                continue

            if page.locator(DEVICE_CHOICE).count():
                radios = page.locator(DEVICE_CHOICE)
                labels = [_label(page, radios.nth(i)) for i in range(radios.count())]
                choice = self.io.prompt("Choose where you would like your one-time passcode sent", type=int,
                                        choices=[f"{n}: {label}" for n, label in enumerate(labels, start=1)])
                if not 1 <= choice <= len(labels):
                    raise SignInError(f"{choice} isn't one of the choices")
                radios.nth(choice - 1).check(force=True)
                _submit(page, DEVICE_CHOICE)
                last_change = time.monotonic()
                continue

            if _visible(page, OTP):
                code = self.io.prompt("Enter the one-time passcode from your preferred 2FA method")
                page.locator(OTP).first.fill(code.replace(" ", ""))
                _check(page, REMEMBER_DEVICE)  # fewer codes on later sign-ins
                _submit(page, OTP)
                last_change = time.monotonic()
                continue

            body = (page.locator("body").inner_text(timeout=2000) or "").lower()
            if any(t in body for t in APPROVAL_TEXT):
                if not told_to_approve:
                    self.io.echo("Amazon sent you a sign-in approval. Approve it on your phone or in your "
                                 "email, and this will continue on its own.")
                    told_to_approve = True
                last_change = time.monotonic()  # waiting on the user, not stuck
                continue

            skip = page.locator(SKIP)
            if skip.count() and skip.first.is_visible():
                skip.first.click()
                last_change = time.monotonic()
                continue

            if time.monotonic() - last_change > STUCK_AFTER:
                title = page.title()
                snippet = " ".join(body.split())[:200]
                raise SignInError(f"stuck on an Amazon page this doesn't recognize ({title!r}: {snippet})")

        raise SignInError("timed out before the sign-in finished")


def _visible(page, selector: str) -> bool:
    loc = page.locator(selector)
    try:
        return loc.count() > 0 and loc.first.is_visible()
    except Exception:
        return False


def _text(page, selector: str) -> str:
    if not _visible(page, selector):
        return ""
    return " ".join(page.locator(selector).first.inner_text().split())


def _check(page, selector: str) -> None:
    box = page.locator(selector)
    try:
        if box.count() and not box.first.is_checked():
            box.first.check(force=True)
    except Exception:
        pass  # optional checkbox; never worth failing the sign-in over


def _label(page, radio) -> str:
    rid = radio.get_attribute("id")
    if rid:
        label = page.locator(f"label[for='{rid}']")
        if label.count():
            return " ".join(label.first.inner_text().split())
    container = radio.locator("xpath=ancestor::label[1]")
    if container.count():
        return " ".join(container.first.inner_text().split())
    return radio.get_attribute("value") or "option"


def _submit(page, near: str) -> None:
    """Submit the form the field `near` belongs to, and wait for what comes next."""
    form = page.locator(near).first.locator("xpath=ancestor::form[1]")
    button = form.locator(SUBMIT) if form.count() else page.locator(SUBMIT)
    try:
        with page.expect_navigation(wait_until="domcontentloaded", timeout=15000):
            if button.count():
                button.first.click()
            else:
                page.locator(near).first.press("Enter")
    except Exception:
        # Some steps update the page in place rather than navigating.
        page.wait_for_timeout(1500)
