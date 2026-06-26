# Saham Bursa Alert

Scans all 900+ Bursa Malaysia stocks every 30 minutes during trading hours and fires Telegram alerts when any signal condition is met.

## Signals detected

| Signal | Condition |
|---|---|
| 📗 GC Alert | MA50 just crossed above MA200 (Golden Cross) |
| 📗 Bullish Zone Alert | Price is currently above MA200 |
| 📗 ATH Alert | Price within 0.5% of all-time high |
| 📗 52WH Alert | Price within 0.5% of 52-week high |
| 🔥 Pending Breakout | Price within 15% of 52-week high |
| 📈 Volume Surge | Today's volume ≥ 2× the 20-day average |

## Sample Telegram output

```
Saham Alert
KOBAY : 2.430

📗 GC Alert
📗 Bullish Zone Alert
🔥 Pending Breakout (12.9% to 52WH)
────────────────────────────────────

Chart Link :
https://my.tradingview.com/chart/?symbol=MYX:KOBAY

Saham Alert
```

## Setup (5 minutes)

### Step 1 — Create a Telegram bot

1. Open Telegram → search **@BotFather** → send `/newbot`
2. Follow prompts → copy the **bot token** (looks like `123456789:ABCdef...`)
3. Add the bot to your channel/group as an **admin**
4. Get your **chat ID**:
   - For a channel: forward a message from the channel to **@getmyid_bot**
   - For a group: add **@getmyid_bot** to the group → it will show the chat ID
   - Chat IDs for channels/groups are negative numbers like `-1001234567890`

### Step 2 — Fork or clone this repo

```bash
git clone https://github.com/YOUR_USERNAME/bursa-scanner.git
cd bursa-scanner
```

Or click **Fork** on GitHub to add it to your own account.

### Step 3 — Add GitHub Secrets

In your repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

| Secret name | Value |
|---|---|
| `BOT_TOKEN` | Your Telegram bot token |
| `CHAT_ID` | Your channel/group chat ID |

### Step 4 — Enable GitHub Actions

Go to the **Actions** tab in your repo → click **"I understand my workflows, go ahead and enable them"** if prompted.

The scanner will now run automatically every 30 minutes during Bursa trading hours (Mon–Fri, 9am–5pm MYT).

### Step 5 — Test it manually

Actions tab → **Bursa Market Scanner** → **Run workflow** → **Run workflow**

Check your Telegram — you should receive a scan summary and any triggered alerts within ~5 minutes.

## Running locally

```bash
pip install -r requirements.txt

export BOT_TOKEN="your_bot_token"
export CHAT_ID="your_chat_id"

python scanner.py
```

If `BOT_TOKEN` and `CHAT_ID` are not set, alerts are printed to the console instead.

## Customising signals

Edit the tuning constants at the top of `scanner.py`:

```python
PENDING_BREAKOUT_PCT  = 15.0   # % below 52WH to flag as Pending Breakout
VOLUME_SURGE_MULT     = 2.0    # multiplier above 20-day avg volume
ATH_TOLERANCE         = 0.5    # % below ATH still counts as ATH alert
WH52_TOLERANCE        = 0.5    # % below 52WH still counts as 52WH alert
MIN_PRICE             = 0.05   # skip stocks below this price (RM)
MIN_VOLUME            = 50_000 # skip stocks with avg daily volume below this
```

## Customising the schedule

Edit `.github/workflows/scan.yml` — the `cron` line:

```yaml
- cron: '0,30 1-9 * * 1-5'   # every 30 min, 9am–5:30pm MYT, Mon–Fri
```

Bursa Malaysia trading hours are **9:00am–5:00pm MYT** (UTC+8).
GitHub Actions cron uses UTC, so MYT = UTC+8 (subtract 8 hours).

| Schedule | Cron |
|---|---|
| Every 30 min during market hours | `0,30 1-9 * * 1-5` |
| Once at market open (9am MYT) | `0 1 * * 1-5` |
| Three times daily | `0 1,4,8 * * 1-5` |

## File structure

```
bursa-scanner/
├── scanner.py                    # Main scanner + signal engine
├── requirements.txt              # Python dependencies
├── README.md                     # This file
└── .github/
    └── workflows/
        └── scan.yml              # GitHub Actions cron schedule
```

## GitHub Actions free tier

GitHub gives **2,000 free minutes/month** (public repo) or **500 minutes/month** (private repo).

Each scan of ~900 stocks takes roughly **4–6 minutes**.  
Running every 30 min during market hours = ~16 runs/day × ~5 min = ~80 min/day × ~22 trading days = **~1,760 min/month**.

This fits within the free tier on a **public repo**. For a private repo, consider scanning once per hour instead:

```yaml
- cron: '0 1-9 * * 1-5'   # every hour
```

## Data source

Price data is fetched from Yahoo Finance via `yfinance`. Data is end-of-day with a ~15-minute delay. This is suitable for daily/swing trading signals, not for intraday scalping.

## Disclaimer

This tool is for informational purposes only and does not constitute financial advice. Always do your own research before making investment decisions.
