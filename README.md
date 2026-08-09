# Hermes SG bag watch

Watches the Hermes Singapore "Bags and clutches" category page
(`hermes.com/sg/en/category/leather-goods/bags-and-clutches/`) and sends a
WhatsApp alert (via [CallMeBot](https://www.callmebot.com/)) whenever a
configured item newly appears in the listing. Hermes only lists items that
are currently orderable, so a new appearance means it just became available
to buy.

Currently watching (see `KEYWORD_GROUPS` in `tracker.py`):

- Picotin 18
- Garden Party 30

Add more items by adding entries to `KEYWORD_GROUPS` - no other code changes
needed.

## Why this isn't a plain HTTP scraper

Unlike the sibling `ovenbird-tracker` project (a simple JSON API poll),
hermes.com sits behind **DataDome** bot protection. A cold, direct request to
a category URL gets served a CAPTCHA page. This tracker uses a real headless
browser (Playwright/Chromium) that first visits the homepage to pick up
session cookies before loading the category page, which gets through in
testing - but there's no guarantee it keeps working, especially from a
datacenter IP.

**Known risk:** this runs on GitHub Actions (a datacenter IP), which DataDome
is more likely to flag than a residential IP. If the tracker starts failing
consistently, `tracker.py` will send one WhatsApp heads-up after 4
consecutive failed checks (see `BLOCK_ALERT_THRESHOLD`) rather than failing
silently. If that happens, the most likely fix is switching this to run
locally (e.g. Windows Task Scheduler) instead of on GitHub Actions, the same
tradeoff discussed when this was set up.

## Setup

1. `pip install -r requirements.txt && playwright install --with-deps chromium`
2. Copy `config.example.json` to `config.json` and fill in your CallMeBot
   phone/API key (get the API key by WhatsApping `I allow callmebot to send
   me messages` to `+34 644 59 71 67`, per CallMeBot's setup instructions).
3. Test a single pass: `python tracker.py --once`, then check `tracker.log`.
4. For GitHub Actions: add `CALLMEBOT_PHONE` and `CALLMEBOT_APIKEY` as repo
   secrets (Settings -> Secrets and variables -> Actions), push, and the
   `.github/workflows/tracker.yml` workflow will run hourly.

The very first run only captures a baseline (no alert), so nothing fires
just because the tracker started up.

## State

`state.json` tracks which matching items were seen on the last successful
check, plus a consecutive-failure counter. It's committed back to the repo
by the GitHub Actions workflow after each run (the runner is stateless
otherwise), and also keeps the repo "active" so the scheduled workflow
doesn't get auto-disabled after 60 days of inactivity.
