"""Hermes Singapore bag-drop tracker.

Loads the combined Hermes SG "Bags and clutches" category page with a
headless browser (the site sits behind DataDome bot protection, so a plain
HTTP request gets CAPTCHA'd - a real browser context is needed to get past
it), extracts every currently-listed product, and sends a WhatsApp alert via
CallMeBot whenever a configured item (e.g. "Picotin 18", "Garden Party 30")
newly appears in the listing. Sold-out items simply aren't listed at all, so
"newly appears" == "just became available to buy".
"""

import argparse
import json
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests
from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "state.json"
LOG_PATH = BASE_DIR / "tracker.log"

HOME_URL = "https://www.hermes.com/sg/en/"
CATEGORY_URL = "https://www.hermes.com/sg/en/category/leather-goods/bags-and-clutches/"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Match rules: a product's link text must contain every term in a group
# (case-insensitive) to count as that item. Add more entries here to watch
# for other charms/bags without touching the rest of the script.
KEYWORD_GROUPS = [
    {"label": "Picotin 18", "must_contain": ["picotin", "18"]},
    {"label": "Garden Party 30", "must_contain": ["garden party", "30"]},
]

# If the listing can't be fetched this many checks in a row (DataDome block,
# network error, etc.), send one heads-up so it doesn't fail silently for days.
BLOCK_ALERT_THRESHOLD = 4

# Items observed so far flicker in and out of the listing every few hours
# (stock likely gets reserved by someone's cart, then released back), not a
# one-time restock. Don't re-alert for the same item within this many hours
# of the last alert, so a temporary blip doesn't spam a fresh WhatsApp
# message every time it happens to reappear.
ALERT_COOLDOWN_HOURS = 24

DEFAULT_CONFIG = {
    "poll_interval_seconds": 3600,
}


def log(message: str) -> None:
    line = f"{datetime.now().isoformat(timespec='seconds')} {message}"
    print(line)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_config() -> dict:
    config = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        config.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))

    # Environment variables take precedence (used for GitHub Actions secrets).
    if os.environ.get("CALLMEBOT_PHONE"):
        config["callmebot_phone"] = os.environ["CALLMEBOT_PHONE"]
    if os.environ.get("CALLMEBOT_APIKEY"):
        config["callmebot_apikey"] = os.environ["CALLMEBOT_APIKEY"]

    if "callmebot_phone" not in config or "callmebot_apikey" not in config:
        log(
            "Missing CallMeBot credentials. Set callmebot_phone/callmebot_apikey in "
            f"{CONFIG_PATH}, or CALLMEBOT_PHONE/CALLMEBOT_APIKEY env vars."
        )
        sys.exit(1)

    return config


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def send_whatsapp(config: dict, message: str) -> None:
    phone = config["callmebot_phone"]
    apikey = config["callmebot_apikey"]
    url = (
        "https://api.callmebot.com/whatsapp.php"
        f"?phone={quote(phone)}&text={quote(message)}&apikey={quote(str(apikey))}"
    )
    try:
        resp = requests.get(url, timeout=15)
        log(f"WhatsApp send status={resp.status_code} body={resp.text[:200]!r}")
    except Exception as exc:
        log(f"WhatsApp send failed: {exc}")


def match_label(text: str) -> str | None:
    lowered = text.lower()
    for group in KEYWORD_GROUPS:
        if all(term in lowered for term in group["must_contain"]):
            return group["label"]
    return None


def collect_all_products(page) -> dict[str, str]:
    """Harvest every product card's link text, keyed by product URL.

    The grid lazily paginates behind a "Load more" button and virtualizes
    off-screen rows, so cards only exist in the DOM once loaded and scrolled
    into view. We click "load more" until it's gone, then scroll the page in
    steps - re-harvesting after each step - until no new items show up for a
    few consecutive rounds.
    """
    seen: dict[str, str] = {}

    def harvest() -> None:
        items = page.eval_on_selector_all(
            'a[href*="/sg/en/product/"]',
            "els => els.map(e => ({href: e.href, text: (e.innerText || e.textContent || '')}))",
        )
        for item in items:
            href = item["href"]
            text = " ".join(item["text"].split())
            if not text:
                continue
            if href not in seen or len(text) > len(seen[href]):
                seen[href] = text

    harvest()

    for _ in range(15):
        load_more = page.get_by_text("LOAD MORE ITEMS", exact=False)
        if load_more.count() == 0:
            break
        try:
            load_more.first.scroll_into_view_if_needed(timeout=5000)
            load_more.first.click(timeout=5000)
        except Exception as exc:
            log(f"Load-more click stopped: {exc}")
            break
        page.wait_for_timeout(1500)
        harvest()

    prev_count = -1
    stable_rounds = 0
    for _ in range(60):
        page.mouse.wheel(0, 1200)
        page.wait_for_timeout(250)
        harvest()
        if len(seen) == prev_count:
            stable_rounds += 1
            if stable_rounds >= 3:
                break
        else:
            stable_rounds = 0
        prev_count = len(seen)

    return seen


def fetch_bag_listing() -> dict[str, str] | None:
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 900},
            locale="en-SG",
            timezone_id="Asia/Singapore",
        )
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = context.new_page()
        try:
            # Hit the homepage first to pick up session cookies - a cold direct
            # request straight to a category URL is more likely to get CAPTCHA'd.
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000 + random.randint(0, 1500))
            page.goto(CATEGORY_URL, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000 + random.randint(0, 1500))

            listing = collect_all_products(page)
        except Exception as exc:
            log(f"Fetch failed: {exc}")
            return None
        finally:
            browser.close()

    if len(listing) < 10:
        log(f"Suspiciously few products found ({len(listing)}) - treating as a failed fetch.")
        return None

    return listing


def run_once(config: dict, state: dict) -> None:
    state["last_checked_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    listing = fetch_bag_listing()

    if listing is None:
        state["consecutive_blocks"] = state.get("consecutive_blocks", 0) + 1
        log(f"Fetch unsuccessful (consecutive_blocks={state['consecutive_blocks']}).")
        if state["consecutive_blocks"] >= BLOCK_ALERT_THRESHOLD and not state.get("block_alert_sent"):
            send_whatsapp(
                config,
                "Hermes tracker: fetch has failed/been blocked "
                f"{state['consecutive_blocks']} checks in a row (likely DataDome "
                "blocking the GitHub Actions IP). May need to run this locally instead.",
            )
            state["block_alert_sent"] = True
        save_state(state)
        return

    state["consecutive_blocks"] = 0
    state["block_alert_sent"] = False

    matches = {}
    for href, text in listing.items():
        label = match_label(text)
        if label:
            matches[href] = {"label": label, "text": text}

    prev_seen = state.get("seen")
    if prev_seen is None:
        state["seen"] = matches
        state["alert_history"] = {}
        log(f"Baseline captured. {len(matches)} matching item(s) currently listed. No alert on first run.")
        save_state(state)
        return

    now = datetime.now(timezone.utc)
    alert_history = state.get("alert_history", {})
    cooldown = timedelta(hours=ALERT_COOLDOWN_HOURS)

    alert_worthy = []
    for href in matches:
        last_alerted = alert_history.get(href)
        if last_alerted is None or now - datetime.fromisoformat(last_alerted) >= cooldown:
            alert_worthy.append(href)

    if alert_worthy:
        lines = [f"{matches[h]['label']}: {matches[h]['text']} - {h}" for h in alert_worthy]
        message = "Hermes SG alert! New listing(s) just appeared:\n- " + "\n- ".join(lines)
        log(f"ALERT SENT for: {alert_worthy}")
        send_whatsapp(config, message)
        for href in alert_worthy:
            alert_history[href] = now.isoformat(timespec="seconds")
    else:
        log(f"No alert-worthy matches ({len(matches)} matching item(s) currently listed, all within cooldown).")

    state["seen"] = matches
    state["alert_history"] = alert_history
    save_state(state)


def main() -> None:
    parser = argparse.ArgumentParser(description="Hermes SG bag-drop tracker")
    parser.add_argument("--once", action="store_true", help="Run a single check pass and exit")
    args = parser.parse_args()

    config = load_config()
    state = load_state()

    if args.once:
        run_once(config, state)
        return

    log("Hermes tracker started. Ctrl+C to stop.")
    while True:
        run_once(config, state)
        interval = config.get("poll_interval_seconds", 3600) + random.uniform(0, 30)
        time.sleep(interval)


if __name__ == "__main__":
    main()
