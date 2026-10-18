"""Sign in to Target from the web UI's form, in a browser in the container.

The script handles the steps it recognizes: the email, the password, a
one-time code and where to send it, preferring the password when Target
offers both. Target's "Press & hold" human check, and any page the script
doesn't recognize, are handed to the user through the live view (`io.handoff`)
-- the check is never automated -- and the script carries on once they press
Continue.
"""
from __future__ import annotations

import logging
import re
import time

from playwright.sync_api import Error as PlaywrightError

log = logging.getLogger(__name__)

HOME = "https://www.target.com/"
SIGN_IN_TIMEOUT = 420  # seconds, including the user's answers and hand-offs
STUCK_AFTER = 15  # seconds on an unrecognized page before handing it to the user

HUMAN_CHECK = "#px-captcha-modal:visible, #px-captcha:visible"
ACCOUNT = "#account-sign-in"
EMAIL = ("input#username:visible, input[name='username']:visible, input[type='email']:visible, "
         "input[autocomplete='username']:visible")
PASSWORD = "input[type='password']:visible"
CODE = ("input[autocomplete='one-time-code']:visible, input[name*='code' i]:visible, "
        "input[id*='code' i]:visible, input[inputmode='numeric']:visible")
KEEP_SIGNED_IN = "input[name*='keepMeSignedIn' i], input[id*='keepMeSignedIn' i], input[id*='kmsi' i]"
ERROR = "[role='alert']:visible, [data-test*='error' i]:visible, [id*='error' i]:visible"
SIGN_IN_BUTTON = re.compile(r"^\s*sign in( or create account)?\s*$", re.I)
USE_PASSWORD = re.compile(r"(enter|use|sign in with)( your| a)? password|password instead", re.I)
CODE_CHANNEL = re.compile(r"\b(text|sms|email|call)\b", re.I)
EMAIL_LIKE = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
PHONE_LIKE = re.compile(r"\+?\d[\d\s().*-]{6,}\d")
SUBMIT = ("button[type='submit']:visible, input[type='submit']:visible, button#login:visible, "
          "button:has-text('Continue'):visible, button:has-text('Sign in'):visible")


class SignInError(RuntimeError):
    pass


def signed_in(page) -> bool:
    """Target's account link says "Account, sign in" until someone is signed in."""
    try:
        link = page.locator(ACCOUNT)
        if not link.count():
            return False
        label = (link.first.get_attribute("aria-label") or "").lower()
        return bool(label) and "sign in" not in label
    except Exception:
        return False


class TargetSignIn:
    def __init__(self, io) -> None:
        self.io = io

    def run(self, context, page, email: str, password: str, timeout: int = SIGN_IN_TIMEOUT,
            home: str = HOME) -> None:
        page.goto(home, wait_until="domcontentloaded", timeout=45000)
        deadline = time.monotonic() + timeout
        last_change = time.monotonic()
        password_tries = 0
        opened_sign_in = 0
        asked_channel = False
        pages = [page]

        def progressed() -> None:
            nonlocal last_change
            last_change = time.monotonic()

        while time.monotonic() < deadline:
            page.wait_for_timeout(700)

            if _seen(page, HUMAN_CHECK):
                log.info("target sign-in: human check at %s; handing over", page.url.split("?")[0])
                self.io.handoff(context, pages, "Target wants to check you're human: press and hold its button "
                                "until it finishes, then press Continue.",
                                until=lambda ctx: not _visible(page, HUMAN_CHECK))
                progressed()
                continue

            if signed_in(page):
                return

            error = _text(page, ERROR)
            if error and (_visible(page, EMAIL) or _visible(page, PASSWORD) or _visible(page, CODE)):
                raise SignInError(f"Target says: {_redact(error, email)}")

            if _visible(page, PASSWORD):
                password_tries += 1
                if password_tries > 2:
                    raise SignInError("Target keeps asking for the password; check it and try again")
                if _visible(page, EMAIL) and not page.locator(EMAIL).first.input_value():
                    page.locator(EMAIL).first.fill(email)
                page.locator(PASSWORD).first.fill(password)
                _check(page, KEEP_SIGNED_IN)
                _submit(page, PASSWORD)
                progressed()
                continue

            if _visible(page, CODE):
                # Target sometimes defaults to a code; a password needs no phone.
                if password_tries == 0 and _click(page, USE_PASSWORD):
                    progressed()
                    continue
                code = self.io.prompt("Enter the code Target sent you")
                page.locator(CODE).first.fill(re.sub(r"\s", "", code))
                _check(page, KEEP_SIGNED_IN)
                _submit(page, CODE)
                progressed()
                continue

            if _visible(page, EMAIL):
                field = page.locator(EMAIL).first
                if not field.input_value():
                    field.fill(email)
                _check(page, KEEP_SIGNED_IN)
                _submit(page, EMAIL)
                progressed()
                continue

            if password_tries == 0 and _click(page, USE_PASSWORD):
                progressed()
                continue

            choices = _channel_choices(page)
            if choices and not asked_channel:
                labels = [label for label, _ in choices]
                n = self.io.prompt("Where should Target send your sign-in code?", type=int,
                                   choices=[f"{i}: {_redact(label, email)}" for i, label in enumerate(labels, 1)])
                if not 1 <= n <= len(choices):
                    raise SignInError(f"{n} isn't one of the choices")
                choices[n - 1][1].click()
                asked_channel = True
                progressed()
                continue

            # Still on the shop: open the account panel and its Sign in button.
            if opened_sign_in < 3 and page.locator(ACCOUNT).count():
                try:
                    button = page.get_by_role("button", name=SIGN_IN_BUTTON)
                    if not (button.count() and button.first.is_visible()):
                        page.locator(ACCOUNT).first.click(timeout=5000)
                        page.wait_for_timeout(1500)
                        button = page.get_by_role("button", name=SIGN_IN_BUTTON)
                    if button.count() and button.first.is_visible():
                        opened_sign_in += 1
                        button.first.click(timeout=5000)
                        page.wait_for_timeout(2500)
                        progressed()
                        continue
                except PlaywrightError:
                    continue  # something covered it (a check?); look again

            if time.monotonic() - last_change > STUCK_AFTER:
                log.info("target sign-in: unrecognized page %r at %s; handing over",
                         _redact(page.title(), email), page.url.split("?")[0])
                self.io.handoff(context, pages, "Target is showing a step this doesn't handle. Finish signing "
                                "in here, then press Continue.", until=lambda ctx: signed_in(page))
                if signed_in(page):
                    return
                progressed()

        raise SignInError("timed out before the sign-in finished")


def _channel_choices(page) -> list:
    """Buttons or options like "Text me a code" / "Email me a code"."""
    body = (page.locator("body").inner_text(timeout=2000) or "").lower()
    if "code" not in body:
        return []
    found = []
    for role in ("radio", "button", "link"):
        loc = page.get_by_role(role, name=CODE_CHANNEL)
        for i in range(loc.count()):
            el = loc.nth(i)
            if el.is_visible():
                label = " ".join((el.inner_text() or el.get_attribute("aria-label") or "").split())
                if role == "radio" and not label:
                    label = el.evaluate("e => (e.labels && e.labels[0] && e.labels[0].innerText) || ''")
                if label and "password" not in label.lower():
                    found.append((label, el))
        if found:
            return found
    return []


def _seen(page, selector: str, window: float = 1.0) -> bool:
    """Visible at any point during `window` seconds: a check that blinks while
    loading is still there, even if one glance lands on a blink."""
    end = time.monotonic() + window
    while True:
        if _visible(page, selector):
            return True
        if time.monotonic() >= end:
            return False
        page.wait_for_timeout(150)


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
        pass  # optional


def _click(page, name: re.Pattern) -> bool:
    for role in ("link", "button"):
        loc = page.get_by_role(role, name=name)
        for i in range(loc.count()):
            el = loc.nth(i)
            if el.is_visible() and "forgot" not in (el.inner_text() or "").lower():
                el.click()
                page.wait_for_timeout(2000)
                return True
    return False


def _submit(page, near: str) -> None:
    form = page.locator(near).first.locator("xpath=ancestor::form[1]")
    button = form.locator(SUBMIT) if form.count() else page.locator(SUBMIT)
    if button.count():
        button.first.click()
    else:
        page.locator(near).first.press("Enter")
    page.wait_for_timeout(2500)


def _redact(text: str, email: str) -> str:
    if email:
        text = re.sub(re.escape(email), "<email>", text, flags=re.I)
    return PHONE_LIKE.sub("<phone>", EMAIL_LIKE.sub("<email>", text))
