# EOD Bursa Scanner

Duplicated from the original **Bursa Market Scanner — Saham Alert** project.

Scans all Bursa Malaysia stocks **once daily at 8:00 AM MYT** (End-Of-Day, pre-market) and fires Telegram alerts when a stock meets both the signal condition and the entry rules below. Does **not** scan on Saturday, Sunday, or Malaysian public holidays.

## Signals detected

| Signal | Condition |
|---|---|
| 🚀 52-Week High (52WH) | Price at or near 52-week high |
| 📈 2-Year High (2YH) | Price at or near 2-year high |
| ⚡ Volume Surge | Volume >= 1.5x 20-day average volume |

## Rules (must pass before any signal can fire)

- Price closed **above** yesterday's / the previous daily close
- Volume **above 500,000** shares
- Price between RM0.205 and RM7.05

## Telegram output

All matching stocks for the day are sent as a consolidated list with stock name, code, price, and triggered signals:

```html
<b>📊 EOD Bursa Scanner — 2026-09-04</b>

MAYBANK (1155): RM 9.85 🚀 52-Week High (52WH)
PCHEM (5183): RM 7.20 ⚡ Volume Surge
TOPGLOVE (7113): RM 1.23 🔥 52-Week High (52WH) | 2-Year High (2YH)
```


If the day's matches don't fit in one Telegram message (4096-character limit), the table is split across multiple messages, each a complete, independently-readable table. If no stock matches that day, no Telegram message is sent.

## Schedule

| Item | Value |
|---|---|
| Scan time | 8:00 AM GMT+8 / MYT (00:05 UTC), once daily |
| Days | Monday–Friday |
| Skipped | Saturday, Sunday, Malaysian public holidays |

Cron (`.github/workflows/scanner.yml`):

```yaml
schedule:
  - cron: '5 0 * * 1-5'   # 8:05 AM GMT+8 (00:05 UTC), Mon-Fri
```


The weekend/public-holiday skip is enforced twice — once by the cron (`1-5` = Mon–Fri) and again inside `scanner.py`'s `should_run()`, which also checks the `holidays` Malaysia calendar.

## Setup (5 minutes)

### Step 1 — Create a Telegram bot

1. Open Telegram → search **@BotFather** → send `/newbot`
2. Follow prompts → copy the **bot token** (looks like `123456789:ABCdef...`)
3. Add the bot to your channel/group as an **admin**
4. Get your **chat ID**:
   - For a channel: forward a message from the channel to **@getmyid_bot**
   - For a group: add **@getmyid_bot** to the group → it will show the chat ID
   - Chat IDs for channels/groups are negative numbers like `-1001234567890`

### Step 2 — Push this project to its own GitHub repo

```bash
cd EOD-Bursa-Scanner
git init
git add .
git commit -m "Initial commit: EOD Bursa Scanner"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/eod-bursa-scanner.git
git push -u origin main
```

### Step 3 — Add GitHub Secrets

In your repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

| Secret name | Value |
|---|---|
| `BOT_TOKEN` | Your Telegram bot token |
| `CHAT_ID` | Your channel/group chat ID |

### Step 4 — Enable GitHub Actions

Go to the **Actions** tab in your repo → click **"I understand my workflows, go ahead and enable them"** if prompted.

The scanner will now run automatically once a day at 8:00 AM MYT (Mon–Fri, skipping public holidays).

### Step 5 — Test it manually

Actions tab → **EOD Bursa Scanner** → **Run workflow** → **Run workflow**

This bypasses the schedule and runs immediately (it does **not** bypass the weekend/holiday check — for a full-bypass test run locally with `FORCE_RUN=true`, see below). Check your Telegram — you should receive alerts for any stock meeting the Bullish Zone or Pending Breakout signal.

## Running locally

```bash
pip install -r requirements.txt

export BOT_TOKEN="your_bot_token"
export CHAT_ID="your_chat_id"

python scanner.py

# To bypass the weekday/holiday check for a test run:
FORCE_RUN=true python scanner.py
```

If `BOT_TOKEN` and `CHAT_ID` are not set, alerts are logged as a warning instead of being sent.

## Customising

Edit the tuning constants at the top of `scanner.py`:

```python
MIN_PRICE             = 0.205   # skip stocks below this price (RM)
MAX_PRICE             = 7.05    # skip stocks above this price (RM)
MIN_VOLUME            = 500_000 # skip stocks with volume at or below this
PENDING_BREAKOUT_PCT  = 7.0     # % below 52WH to flag as Pending Breakout
```

## File structure

```
EOD-Bursa-Scanner/
├── scanner.py                    # EOD scanner + signal engine (Bullish Zone, Pending Breakout)
├── requirements.txt              # Python dependencies
├── stocks.json                   # Ticker list
├── README.md                     # This file
└── .github/
    └── workflows/
        └── scanner.yml           # GitHub Actions cron schedule (8:00 AM MYT, Mon-Fri)
```

## Data source

Price data is fetched from Yahoo Finance via `yfinance`. Data is end-of-day with a ~15-minute delay, which is appropriate for this EOD (End-Of-Day) scanner.

## Disclaimer

This tool is for informational purposes only and does not constitute financial advice. Always do your own research before making investment decisions.
