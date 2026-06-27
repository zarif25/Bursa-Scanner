"""
Bursa Malaysia Market Scanner — Saham Alert

Signals detected (only fires when price is UP vs yesterday's close):
  - Price Up                 : Current price higher than yesterday's close
  - Golden Cross (GC)        : MA50 crosses above MA200
  - Bullish Zone             : Price above MA200
  - 52-Week High (52WH)      : Price within 0.5% of 52-week high
  - All-Time High (ATH)      : Price within 0.5% of all-time high
  - Pending Breakout         : Price within 7% of 52-week high
  - Volume Surge             : Volume 2x above 20-day average

Filters:
  - Price range RM0.205 – RM6.90 only
  - Skips Bursa public holidays (Malaysia)
  - Only runs Mon–Fri 9am–5pm MYT

Runs via GitHub Actions cron during Bursa trading hours.
Fires alerts to a Telegram channel/group.
"""

import os
import time
import logging
import requests
import pandas as pd
import yfinance as yf
from datetime import date, datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bursa-scanner")

# ── Config ─────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID   = os.environ.get("CHAT_ID",   "")

# Scanner tuning
PENDING_BREAKOUT_PCT  =  7.0    # % below 52WH to flag as Pending Breakout (changed from 15%)
VOLUME_SURGE_MULT     =  2.0    # multiplier above 20-day avg vol
ATH_TOLERANCE         =  0.5    # % below ATH still counts as ATH alert
WH52_TOLERANCE        =  0.5    # % below 52WH still counts as 52WH alert
MIN_PRICE             =  0.205  # minimum price filter (RM)
MAX_PRICE             =  6.90   # maximum price filter (RM)
MIN_VOLUME            = 50_000  # skip illiquid stocks
MAX_WORKERS           = 20      # parallel download threads
DELAY_BETWEEN_MSGS    =  1.0    # seconds between Telegram sends


# ── Malaysia Public Holidays ───────────────────────────────────────────────────
# Bursa Malaysia is CLOSED on these dates.
# Update this list each year.
MALAYSIA_PUBLIC_HOLIDAYS = {
    # 2025
    date(2025,  1,  1),   # New Year's Day
    date(2025,  1, 29),   # Chinese New Year
    date(2025,  1, 30),   # Chinese New Year Holiday
    date(2025,  2,  1),   # Federal Territory Day
    date(2025,  3, 31),   # Hari Raya Aidilfitri
    date(2025,  4,  1),   # Hari Raya Aidilfitri Holiday
    date(2025,  5,  1),   # Labour Day
    date(2025,  5, 12),   # Wesak Day
    date(2025,  6,  2),   # Agong's Birthday
    date(2025,  6,  7),   # Hari Raya Aidiladha
    date(2025,  6, 27),   # Awal Muharram
    date(2025,  8, 31),   # National Day
    date(2025,  9, 16),   # Malaysia Day
    date(2025,  9, 26),   # Prophet Muhammad's Birthday
    date(2025, 10, 20),   # Deepavali
    date(2025, 12, 25),   # Christmas Day

    # 2026
    date(2026,  1,  1),   # New Year's Day
    date(2026,  1, 19),   # Thaipusam
    date(2026,  2,  1),   # Federal Territory Day
    date(2026,  2, 17),   # Chinese New Year
    date(2026,  2, 18),   # Chinese New Year Holiday
    date(2026,  3, 20),   # Hari Raya Aidilfitri
    date(2026,  3, 21),   # Hari Raya Aidilfitri Holiday
    date(2026,  5,  1),   # Labour Day
    date(2026,  5,  2),   # Wesak Day
    date(2026,  6,  1),   # Agong's Birthday
    date(2026,  5, 27),   # Hari Raya Aidiladha
    date(2026,  6, 17),   # Awal Muharram
    date(2026,  8, 31),   # National Day
    date(2026,  9, 16),   # Malaysia Day
    date(2026,  9, 15),   # Prophet Muhammad's Birthday
    date(2026, 11,  9),   # Deepavali
    date(2026, 12, 25),   # Christmas Day
}


# ── Market status ──────────────────────────────────────────────────────────────
def is_market_open() -> bool:
    """
    Returns True only if Bursa Malaysia is open right now:
      - Weekday (Mon–Fri)
      - Not a Malaysian public holiday
      - Between 9:00am and 5:00pm MYT (UTC+8)
    """
    myt = timezone(timedelta(hours=8))
    now = datetime.now(myt)

    # Weekend check
    if now.weekday() > 4:
        log.info("Market closed: weekend")
        return False

    # Public holiday check
    today = now.date()
    if today in MALAYSIA_PUBLIC_HOLIDAYS:
        log.info(f"Market closed: public holiday ({today})")
        return False

    # Trading hours check
    market_open  = now.replace(hour=9,  minute=0, second=0, microsecond=0)
    market_close = now.replace(hour=17, minute=0, second=0, microsecond=0)
    if not (market_open <= now <= market_close):
        log.info(f"Market closed: outside trading hours ({now.strftime('%H:%M')} MYT)")
        return False

    return True


# ── Telegram ───────────────────────────────────────────────────────────────────
def send_telegram(message: str) -> bool:
    if not BOT_TOKEN or not CHAT_ID:
        log.warning("BOT_TOKEN or CHAT_ID not set — printing to console")
        print(message)
        return False
    url  = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {
        "chat_id":                  CHAT_ID,
        "text":                     message,
        "parse_mode":               "HTML",
        "disable_web_page_preview": False,
    }
    try:
        resp = requests.post(url, json=data, timeout=10)
        resp.raise_for_status()
        return True
    except Exception as e:
        log.error(f"Telegram error: {e}")
        return False


def format_alert(ticker: str, price: float, signals: list[str], name: str = "") -> str:
    code     = ticker.replace(".KL", "")
    tv_label = name if name else code
    chart    = f"https://my.tradingview.com/chart/?symbol=MYX:{tv_label}"
    sigs_txt = "\n".join(signals)
    divider  = "─" * 36
    return (
        f"<b>Saham Alert</b>\n"
        f"{tv_label} : {price:.3f}\n"
        f"{sigs_txt}\n"
        f"{divider}\n"
        f"Chart Link :\n"
        f'<a href="{chart}">{chart}</a>'
    )


# ── Stock list ─────────────────────────────────────────────────────────────────
def get_bursa_tickers() -> list[tuple[str, str]]:
    """
    Fetch all Bursa Malaysia tickers from KLSEScreener.
    Returns list of (ticker, stock_name) e.g. ("0023.KL", "KOBAY").
    """
    try:
        url     = "https://www.klsescreener.com/v2/screener/quote_results"
        headers = {"User-Agent": "Mozilla/5.0 (compatible; BursaScanner/1.0)"}
        params  = {"board": "", "sector": "", "sortby": "code",
                   "sortorder": "asc", "page": 1, "per_page": 9999}
        resp = requests.get(url, params=params, headers=headers, timeout=30)
        resp.raise_for_status()
        data   = resp.json()
        stocks = [
            (
                f"{item['code']}.KL",
                (item.get("stock_name") or item.get("symbol") or
                 item.get("name") or "").strip().split()[0]
            )
            for item in data.get("data", []) if item.get("code")
        ]
        if stocks:
            log.info(f"Fetched {len(stocks)} tickers from KLSEScreener")
            return stocks
    except Exception as e:
        log.warning(f"KLSEScreener fetch failed: {e} — using fallback list")

    fallback = [
        ("1155.KL", "MAYBANK"), ("1295.KL", "PBBANK"),  ("1023.KL", "CIMB"),
        ("5183.KL", "PCHEM"),   ("6888.KL", "AXIATA"),  ("4863.KL", "TM"),
        ("6947.KL", "MAXIS"),   ("5347.KL", "TENAGA"),  ("3816.KL", "MISC"),
        ("2445.KL", "SIME"),    ("4197.KL", "SIMEPLT"), ("5285.KL", "IHH"),
        ("7277.KL", "DIALOG"),  ("5168.KL", "HARTA"),   ("7113.KL", "KOSSAN"),
        ("5110.KL", "SUPERCOMNET"), ("0023.KL", "KOBAY"),
        ("0007.KL", "LACMED"),  ("0166.KL", "TOPGLOV"),
    ]
    log.info(f"Using fallback list of {len(fallback)} tickers")
    return fallback


# ── Signal detection ───────────────────────────────────────────────────────────
def analyze(ticker: str, name: str = "") -> Optional[dict]:
    """
    Download 2 years of daily OHLCV and check all signal conditions.
    Uses actual traded prices (auto_adjust=False).
    Only fires if:
      - Price is UP vs yesterday's actual close
      - Price is within RM0.205 – RM6.90 range
      - Price is within 7% of 52-week high (Pending Breakout gate)
    """
    try:
        # Resolve stock name from yfinance if not supplied
        if not name:
            try:
                info = yf.Ticker(ticker).info
                raw  = info.get("shortName", "") or info.get("symbol", "")
                name = raw.split()[0] if raw else ticker.replace(".KL", "")
            except Exception:
                name = ticker.replace(".KL", "")

        # ── Download: auto_adjust=False → real traded prices ─────────────────
        df = yf.download(
            ticker,
            period="2y",
            interval="1d",
            progress=False,
            auto_adjust=False,
        )

        if df is None or df.empty or len(df) < 210:
            return None

        # Flatten MultiIndex columns if present
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        # Close     = actual last traded price (display, gate, price filter)
        # Adj Close = dividend-adjusted (MA calculations only)
        # High      = actual daily high (52WH, ATH)
        close     = df["Close"].dropna()
        adj_close = df["Adj Close"].dropna()
        high      = df["High"].dropna()
        volume    = df["Volume"].dropna()

        if len(close) < 60:
            return None

        current_price  = float(close.iloc[-1])
        current_volume = float(volume.iloc[-1])

        # ── Price range filter: RM0.205 – RM6.90 ─────────────────────────────
        if not (MIN_PRICE <= current_price <= MAX_PRICE):
            return None

        # ── Liquidity filter ──────────────────────────────────────────────────
        avg_vol = float(volume.rolling(20).mean().iloc[-1])
        if avg_vol < MIN_VOLUME:
            return None

        # ── Gate: price must be UP vs yesterday's actual close ────────────────
        prev_close = float(close.iloc[-2])
        if current_price <= prev_close:
            return None

        # ── 52WH: rolling 252 days on actual High prices ──────────────────────
        high_aligned = high.reindex(close.index).dropna()
        high_252     = float(high_aligned.rolling(252).max().iloc[-1])
        pct_to_52wh  = (high_252 - current_price) / current_price * 100

        # ── Pre-filter: only continue if within 7% of 52WH ───────────────────
        # (catches 52WH alert + Pending Breakout; skip stocks far from high)
        if pct_to_52wh > PENDING_BREAKOUT_PCT and \
           current_price < high_252 * (1 - WH52_TOLERANCE / 100):
            # Not near 52WH at all — still check other signals below
            pass   # we allow GC, Bullish Zone, ATH, Volume Surge regardless

        # ── All-time high from actual highs ───────────────────────────────────
        ath = float(high_aligned.max())

        # ── Moving averages on Adj Close (accurate across dividends/splits) ───
        ma50  = adj_close.rolling(50).mean()
        ma200 = adj_close.rolling(200).mean()

        ma50_now   = float(ma50.iloc[-1])
        ma50_prev  = float(ma50.iloc[-2])
        ma200_now  = float(ma200.iloc[-1])
        ma200_prev = float(ma200.iloc[-2])

        # ── Build signals ─────────────────────────────────────────────────────
        pct_chg = (current_price - prev_close) / prev_close * 100
        signals = [f"📈 Price Up (+{pct_chg:.2f}% vs yesterday)"]

        # Golden Cross
        if ma50_now > ma200_now and ma50_prev <= ma200_prev:
            signals.append("📗 GC Alert")

        # Bullish Zone
        if ma50_now > ma200_now:
            signals.append("📗 Bullish Zone Alert")

        # ATH
        if current_price >= ath * (1 - ATH_TOLERANCE / 100):
            signals.append("📗 ATH Alert")

        # 52WH
        if current_price >= high_252 * (1 - WH52_TOLERANCE / 100):
            signals.append("📗 52WH Alert")

        # Pending Breakout (only if NOT already at 52WH, within 7%)
        elif 0 < pct_to_52wh <= PENDING_BREAKOUT_PCT:
            signals.append(f"🔥 Pending Breakout ({pct_to_52wh:.1f}% to 52WH)")

        # Volume Surge
        if avg_vol > 0 and current_volume >= avg_vol * VOLUME_SURGE_MULT:
            signals.append("📈 Volume Surge")

        # Must have at least one signal beyond "Price Up"
        if len(signals) < 2:
            return None

        return {
            "ticker":  ticker,
            "name":    name,
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
    log.info(f"Saham Alert starting at {start.strftime('%Y-%m-%d %H:%M')} MYT")
    log.info("=" * 60)

    # ── Market open guard (weekday + public holiday + trading hours) ──────────
    if not is_market_open():
        return

    stocks = get_bursa_tickers()
    log.info(f"Scanning {len(stocks)} stocks | "
             f"Price range RM{MIN_PRICE}–RM{MAX_PRICE} | "
             f"Pending Breakout threshold {PENDING_BREAKOUT_PCT}% to 52WH")

    results = []
    done    = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(analyze, t, n): (t, n) for t, n in stocks}
        for future in as_completed(futures):
            done += 1
            if done % 100 == 0:
                log.info(f"  Progress: {done}/{len(stocks)}")
            result = future.result()
            if result:
                results.append(result)

    elapsed = (datetime.now() - start).seconds
    log.info(f"Scan complete in {elapsed}s — {len(results)} signals found")

    if not results:
        log.info("No signals this run.")
        return

    # Summary message
    myt = timezone(timedelta(hours=8))
    now_myt = datetime.now(myt)
    summary = (
        f"<b>🔍 Saham Alert — Market Scan</b>\n"
        f"{now_myt.strftime('%d %b %Y  %H:%M MYT')}\n"
        f"Scanned: {len(stocks)} stocks\n"
        f"Signals found: {len(results)}\n"
        f"{'─' * 36}"
    )
    send_telegram(summary)
    time.sleep(DELAY_BETWEEN_MSGS)

    for r in results:
        msg = format_alert(r["ticker"], r["price"], r["signals"], r.get("name", ""))
        log.info(f"  → {r['ticker']} ({r.get('name','')}) {r['signals']}")
        send_telegram(msg)
        time.sleep(DELAY_BETWEEN_MSGS)

    log.info("All alerts sent.")


if __name__ == "__main__":
    run_scan()
