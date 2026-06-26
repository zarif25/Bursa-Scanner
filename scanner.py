"""
Bursa Malaysia Market Scanner
Nota Saham Alert V4

Signals detected:
  - Golden Cross (GC)        : MA50 crosses above MA200
  - Bullish Zone             : Price above MA200
  - 52-Week High (52WH)      : Price at or near 52-week high
  - All-Time High (ATH)      : Price at or near all-time high
  - Pending Breakout         : Price within 15% of 52-week high
  - Volume Surge             : Volume 2x above 20-day average

Runs via GitHub Actions cron during Bursa trading hours.
Fires alerts to a Telegram channel/group.
"""

import os
import time
import logging
import requests
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bursa-scanner")

# ── Config (set as GitHub Actions secrets or local env vars) ───────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")   # Telegram bot token
CHAT_ID   = os.environ.get("CHAT_ID",   "")   # Telegram chat/channel ID

# Scanner tuning
PENDING_BREAKOUT_PCT  = 15.0   # % below 52WH to flag as Pending Breakout
VOLUME_SURGE_MULT     = 2.0    # multiplier above 20-day avg vol
ATH_TOLERANCE         = 0.5    # % below ATH still counts as ATH alert
WH52_TOLERANCE        = 0.5    # % below 52WH still counts as 52WH alert
MIN_PRICE             = 0.05   # skip penny stocks below this price (RM)
MIN_VOLUME            = 50_000 # skip stocks with avg daily volume below this
MAX_WORKERS           = 20     # parallel threads for downloading data
DELAY_BETWEEN_MSGS    = 1.0    # seconds between Telegram messages (rate limit)


# ── Telegram ───────────────────────────────────────────────────────────────────
def send_telegram(message: str) -> bool:
    """Send a formatted HTML message to Telegram. Returns True on success."""
    if not BOT_TOKEN or not CHAT_ID:
        log.warning("BOT_TOKEN or CHAT_ID not set — skipping Telegram send")
        print(message)   # print to console when running locally
        return False

    url  = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {
        "chat_id":               CHAT_ID,
        "text":                  message,
        "parse_mode":            "HTML",
        "disable_web_page_preview": False,
    }
    try:
        resp = requests.post(url, json=data, timeout=10)
        resp.raise_for_status()
        return True
    except Exception as e:
        log.error(f"Telegram error: {e}")
        return False


def format_alert(ticker: str, price: float, signals: list[str]) -> str:
    """Build the Telegram message in Nota Saham Alert V4 style."""
    sym      = ticker.replace(".KL", "")
    tv_sym   = f"MYX:{sym}"
    chart    = f"https://my.tradingview.com/chart/?symbol={tv_sym}"
    sigs_txt = "\n".join(signals)
    divider  = "─" * 36

    return (
        f"<b>Nota Saham Alert V4</b>\n"
        f"{sym} : {price:.3f}\n\n"
        f"{sigs_txt}\n"
        f"{divider}\n\n"
        f"Chart Link :\n"
        f'<a href="{chart}">{chart}</a>\n\n'
        f"<b>Nota Saham Alert V4</b>"
    )


# ── Stock list ──────────────────────────────────────────────────────────────────
def get_bursa_tickers() -> list[str]:
    """
    Fetch all Bursa Malaysia tickers from KLSEScreener.
    Falls back to a hardcoded starter list if the request fails.
    """
    try:
        url     = "https://www.klsescreener.com/v2/screener/quote_results"
        headers = {"User-Agent": "Mozilla/5.0 (compatible; BursaScanner/1.0)"}
        params  = {
            "board":      "",
            "sector":     "",
            "sortby":     "code",
            "sortorder":  "asc",
            "page":       1,
            "per_page":   9999,
        }
        resp = requests.get(url, params=params, headers=headers, timeout=30)
        resp.raise_for_status()
        data    = resp.json()
        tickers = [f"{item['code']}.KL" for item in data.get("data", []) if item.get("code")]
        if tickers:
            log.info(f"Fetched {len(tickers)} tickers from KLSEScreener")
            return tickers
    except Exception as e:
        log.warning(f"KLSEScreener fetch failed: {e} — using fallback list")

    # Fallback: top Bursa stocks by market cap (edit as needed)
    fallback = [
        "1155.KL",  # Maybank
        "1295.KL",  # Public Bank
        "1023.KL",  # CIMB
        "5183.KL",  # Petronas Chemicals
        "6888.KL",  # Axiata
        "4863.KL",  # Telekom Malaysia
        "6947.KL",  # Maxis
        "5347.KL",  # Tenaga Nasional
        "3816.KL",  # MISC
        "2445.KL",  # Sime Darby
        "4197.KL",  # Sime Darby Plantation
        "5285.KL",  # IHH Healthcare
        "7277.KL",  # Dialog Group
        "5168.KL",  # Hartalega
        "7113.KL",  # Kossan Rubber
        "5110.KL",  # Supercomnet
        "0023.KL",  # KOBAY
        "0007.KL",  # LACMED
        "5285.KL",  # MCLEAN (use actual code)
        "0166.KL",  # TOPGLOV
    ]
    log.info(f"Using fallback list of {len(fallback)} tickers")
    return fallback


# ── Signal detection ───────────────────────────────────────────────────────────
def analyze(ticker: str) -> Optional[dict]:
    """
    Download 2 years of daily OHLCV data and check all signal conditions.
    Returns a result dict if any signals triggered, else None.
    """
    try:
        df = yf.download(
            ticker,
            period="2y",
            interval="1d",
            progress=False,
            auto_adjust=True,
        )

        if df is None or df.empty or len(df) < 210:
            return None

        # Flatten MultiIndex columns if present
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        close  = df["Close"].dropna()
        high   = df["High"].dropna()
        volume = df["Volume"].dropna()

        if len(close) < 60:
            return None

        current_price  = float(close.iloc[-1])
        current_volume = float(volume.iloc[-1])

        # Skip penny stocks and illiquid counters
        if current_price < MIN_PRICE:
            return None
        avg_vol = float(volume.rolling(20).mean().iloc[-1])
        if avg_vol < MIN_VOLUME:
            return None

        # Moving averages
        ma50  = close.rolling(50).mean()
        ma200 = close.rolling(200).mean()

        ma50_now  = float(ma50.iloc[-1])
        ma50_prev = float(ma50.iloc[-2])
        ma200_now  = float(ma200.iloc[-1])
        ma200_prev = float(ma200.iloc[-2])

        # 52-week metrics
        high_252 = float(high.rolling(252).max().iloc[-1])
        pct_to_52wh = (high_252 - current_price) / current_price * 100

        # All-time high (full history)
        ath = float(high.max())

        signals = []

        # ── Golden Cross ──────────────────────────────────────────────────────
        if ma50_now > ma200_now and ma50_prev <= ma200_prev:
            signals.append("📗 GC Alert")

        # ── Bullish Zone ──────────────────────────────────────────────────────
        if ma50_now > ma200_now:
            signals.append("📗 Bullish Zone Alert")

        # ── ATH ───────────────────────────────────────────────────────────────
        if current_price >= ath * (1 - ATH_TOLERANCE / 100):
            signals.append("📗 ATH Alert")

        # ── 52-Week High ──────────────────────────────────────────────────────
        if current_price >= high_252 * (1 - WH52_TOLERANCE / 100):
            signals.append("📗 52WH Alert")

        # ── Pending Breakout ──────────────────────────────────────────────────
        elif 0 < pct_to_52wh <= PENDING_BREAKOUT_PCT:
            signals.append(f"🔥 Pending Breakout ({pct_to_52wh:.1f}% to 52WH)")

        # ── Volume Surge ──────────────────────────────────────────────────────
        if avg_vol > 0 and current_volume >= avg_vol * VOLUME_SURGE_MULT:
            signals.append("📈 Volume Surge")

        if not signals:
            return None

        return {
            "ticker":  ticker,
            "price":   current_price,
            "signals": signals,
        }

    except Exception as e:
        log.debug(f"{ticker}: {e}")
        return None


# ── Main scan ──────────────────────────────────────────────────────────────────
def run_scan():
    start = datetime.now()
    log.info("=" * 60)
    log.info(f"Bursa Scanner starting at {start.strftime('%Y-%m-%d %H:%M MYT')}")
    log.info("=" * 60)

    tickers = get_bursa_tickers()
    log.info(f"Scanning {len(tickers)} stocks with {MAX_WORKERS} threads…")

    results = []
    done = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(analyze, t): t for t in tickers}
        for future in as_completed(futures):
            done += 1
            if done % 100 == 0:
                log.info(f"  Progress: {done}/{len(tickers)}")
            result = future.result()
            if result:
                results.append(result)

    elapsed = (datetime.now() - start).seconds
    log.info(f"Scan complete in {elapsed}s — {len(results)} signals found")

    if not results:
        log.info("No signals this run.")
        return

    # Send summary header
    summary = (
        f"<b>🔍 Nota Saham Alert V4 — Market Scan</b>\n"
        f"{datetime.now().strftime('%d %b %Y  %H:%M MYT')}\n"
        f"Scanned: {len(tickers)} stocks\n"
        f"Signals found: {len(results)}\n"
        f"{'─' * 36}"
    )
    send_telegram(summary)
    time.sleep(DELAY_BETWEEN_MSGS)

    # Send individual alerts
    for r in results:
        msg = format_alert(r["ticker"], r["price"], r["signals"])
        log.info(f"  ALERT: {r['ticker']} — {', '.join(r['signals'])}")
        send_telegram(msg)
        time.sleep(DELAY_BETWEEN_MSGS)

    log.info("All alerts sent.")


if __name__ == "__main__":
    run_scan()
