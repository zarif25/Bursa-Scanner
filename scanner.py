import os
import time
import logging
import requests
import pandas as pd
import yfinance as yf
from pathlib import Path
from datetime import date, datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bursa-scanner")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

PENDING_BREAKOUT_PCT = 7.0
VOLUME_SURGE_MULT = 2.0
ATH_TOLERANCE = 0.5
WH52_TOLERANCE = 0.5
MIN_PRICE = 0.205
MAX_PRICE = 6.90
MIN_VOLUME = 50_000
MAX_WORKERS = 20
DELAY_BETWEEN_MSGS = 1.0
ALERTED_FILE = "alerted_today.json"

MALAYSIA_PUBLIC_HOLIDAYS = {
    date(2025, 1, 1), date(2025, 1, 29), date(2025, 1, 30),
    date(2025, 2, 1), date(2025, 3, 31), date(2025, 4, 1),
    date(2025, 5, 1), date(2025, 5, 12), date(2025, 6, 2),
    date(2025, 6, 7), date(2025, 6, 27), date(2025, 8, 31),
    date(2025, 9, 16), date(2025, 9, 26), date(2025, 10, 20),
    date(2025, 12, 25),
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 1),
    date(2026, 2, 17), date(2026, 2, 18), date(2026, 3, 20),
    date(2026, 3, 21), date(2026, 5, 1), date(2026, 5, 2),
    date(2026, 5, 27), date(2026, 6, 1), date(2026, 6, 17),
    date(2026, 8, 31), date(2026, 9, 15), date(2026, 9, 16),
    date(2026, 11, 9), date(2026, 12, 25),
}

BURSA_HARDCODED: list[tuple[str, str]] = [
    ("1155.KL", "MAYBANK"), ("1295.KL", "PBBANK"), ("1023.KL", "CIMB"),
    ("5183.KL", "PCHEM"), ("6888.KL", "AXIATA"), ("4863.KL", "TM"),
    ("6947.KL", "MAXIS"), ("5347.KL", "TENAGA"), ("3816.KL", "MISC"),
    ("2445.KL", "SIME"), ("4197.KL", "SIMEPLT"), ("5285.KL", "IHH"),
    ("7277.KL", "DIALOG"), ("5168.KL", "HARTA"), ("7113.KL", "KOSSAN"),
    ("5110.KL", "SUPERCOMNET"), ("0023.KL", "KOBAY"), ("0007.KL", "LACMED"),
    ("0166.KL", "TOPGLOV"), ("4065.KL", "PPB"), ("2194.KL", "IOICORP"),
]

def is_market_open() -> bool:
    myt = timezone(timedelta(hours=8))
    now = datetime.now(myt)
    if now.weekday() > 4:
        log.info("Market closed: weekend")
        return False
    today = now.date()
    if today in MALAYSIA_PUBLIC_HOLIDAYS:
        log.info(f"Market closed: public holiday ({today})")
        return False
    market_open = now.replace(hour=9, minute=0, second=0, microsecond=0)
    market_close = now.replace(hour=17, minute=0, second=0, microsecond=0)
    if not (market_open <= now <= market_close):
        log.info(f"Market closed: outside trading hours ({now.strftime('%H:%M')} MYT)")
        return False
    return True

def send_telegram(message: str) -> bool:
    if not BOT_TOKEN or not CHAT_ID:
        log.warning("BOT_TOKEN or CHAT_ID not set")
        print(message)
        return False
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        resp = requests.post(url, json=payload, timeout=20)
        log.info(f"Telegram response: {resp.status_code} {resp.text[:300]}")
        resp.raise_for_status()
        return True
    except Exception as e:
        log.error(f"Telegram error: {e}")
        return False

def format_alert(ticker: str, price: float, signals: list[str], name: str = "") -> str:
    code = ticker.replace(".KL", "")
    tv_label = name if name else code
    chart = f"https://my.tradingview.com/chart/?symbol=MYX:{tv_label}"
    sigs_txt = "\n".join(signals)
    divider = "─" * 36
    return (
        f"<b>Saham Alert</b>\n"
        f"{tv_label} : {price:.3f}\n"
        f"{sigs_txt}\n"
        f"{divider}\n"
        f"Chart Link :\n"
        f'<a href="{chart}">{chart}</a>'
    )

def _fetch_klsescreener() -> list[tuple[str, str]]:
    url = "https://www.klsescreener.com/v2/screener/quote_results"
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.klsescreener.com/v2/screener",
        "Origin": "https://www.klsescreener.com",
    }
    params = {"board": "", "sector": "", "sortby": "code", "sortorder": "asc", "page": 1, "per_page": 9999}
    resp = requests.get(url, params=params, headers=headers, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    stocks = [
        (f"{item['code']}.KL", (item.get("stock_name") or item.get("symbol") or item.get("name") or "").strip().split()[0])
        for item in data.get("data", []) if item.get("code")
    ]
    if not stocks:
        raise ValueError("Empty stock list from KLSEScreener")
    return stocks

def get_bursa_tickers() -> list[tuple[str, str]]:
    try:
        stocks = _fetch_klsescreener()
        if len(stocks) >= 500:
            log.info(f"✅ Layer 1 (KLSEScreener): {len(stocks)} stocks")
            return stocks
        log.warning(f"Layer 1 only returned {len(stocks)} stocks — too few, skipping to Layer 2")
    except Exception as e:
        log.warning(f"Layer 1 failed: {e}")

    try:
        import json, pathlib
        json_path = pathlib.Path(__file__).parent / "stocks.json"
        data = json.loads(json_path.read_text())
        stocks = [(item["code"], item["name"].replace(".KL", "")) for item in data if item.get("code") and item.get("name")]
        if len(stocks) > 500:
            log.info(f"✅ Layer 2 (stocks.json): {len(stocks)} stocks")
            return stocks
    except Exception as e:
        log.warning(f"Layer 2 failed: {e}")

    try:
        import csv, pathlib
        csv_path = pathlib.Path(__file__).parent / "Bursa_Malaysia.csv"
        stocks = []
        with open(csv_path, "r") as f:
            reader = csv.reader(f)
            next(reader)
            for row in reader:
                if len(row) >= 2 and row[0].strip() and row[1].strip():
                    code = row[0].strip()
                    symbol = row[1].strip().replace(".KL", "")
                    stocks.append((code, symbol))
        if len(stocks) > 500:
            log.info(f"✅ Layer 3 (CSV): {len(stocks)} stocks")
            return stocks
    except Exception as e:
        log.warning(f"Layer 3 CSV failed: {e}")

    log.info(f"✅ Layer 4 (hardcoded): {len(BURSA_HARDCODED)} stocks")
    return BURSA_HARDCODED

def analyze(ticker: str, name: str = "") -> Optional[dict]:
    try:
        if not name:
            try:
                info = yf.Ticker(ticker).info
                raw = info.get("shortName", "") or info.get("symbol", "")
                name = raw.split()[0] if raw else ticker.replace(".KL", "")
            except Exception:
                name = ticker.replace(".KL", "")

        tk = yf.Ticker(ticker)
        try:
            fi = tk.fast_info
            current_price = float(fi.last_price)
        except Exception:
            return None

        if not current_price or current_price <= 0:
            return None
        if not (MIN_PRICE <= current_price <= MAX_PRICE):
            return None

        df = tk.history(period="2y", interval="1d", auto_adjust=True)
        if df is None or df.empty or len(df) < 210:
            return None

        if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
            df.columns = df.columns.get_level_values(0)

        open_ = df["Open"].dropna()
        close = df["Close"].dropna()
        high = df["High"].dropna()
        volume = df["Volume"].dropna()

        if len(close) < 60 or len(open_) < 3:
            return None

        current_volume = float(volume.iloc[-1])
        avg_vol = float(volume.rolling(20).mean().iloc[-1])
        if avg_vol < MIN_VOLUME:
            return None

        yesterday_open = float(open_.iloc[-2])
        if yesterday_open <= 0 or current_price <= yesterday_open:
            return None

        close_2d_ago = float(close.iloc[-3])
        pct_vs_2d = (current_price - close_2d_ago) / close_2d_ago * 100 if close_2d_ago > 0 else 0

        high_252 = float(high.rolling(252).max().iloc[-1])
        pct_to_52wh = (high_252 - current_price) / current_price * 100
        ath = float(high.max())

        ma5 = close.rolling(5).mean()
        ma20 = close.rolling(20).mean()
        ma5_now, ma5_prev = float(ma5.iloc[-1]), float(ma5.iloc[-2])
        ma20_now, ma20_prev = float(ma20.iloc[-1]), float(ma20.iloc[-2])

        ma200_now = float(close.rolling(200).mean().iloc[-1])

        signals = []
        if pct_vs_2d >= 7.0:
            signals.append(f"📈 Price Up (+{pct_vs_2d:.2f}% vs 2-day-ago close)")
        if ma5_now > ma20_now and ma5_prev <= ma20_prev:
            signals.append("📗 GC Alert")
        if current_price > ma200_now:
            signals.append("📗 Bullish Zone Alert")
        if current_price >= ath * (1 - ATH_TOLERANCE / 100):
            signals.append("📗 ATH Alert")
        if current_price >= high_252 * (1 - WH52_TOLERANCE / 100):
            signals.append("📗 52WH Alert")
        elif 0 < pct_to_52wh <= PENDING_BREAKOUT_PCT:
            signals.append(f"🔥 Pending Breakout ({pct_to_52wh:.1f}% to 52WH)")
        if avg_vol > 0 and current_volume >= avg_vol * VOLUME_SURGE_MULT:
            signals.append("📈 Volume Surge")

        if not signals:
            return None

        return {"ticker": ticker, "name": name, "price": current_price, "signals": signals}
    except Exception as e:
        log.debug(f"{ticker}: {e}")
        return None

def load_alerted_today() -> set:
    import json
    myt = timezone(timedelta(hours=8))
    today = datetime.now(myt).date().isoformat()
    path = Path(__file__).parent / ALERTED_FILE
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text())
        if data.get("date") != today:
            return set()
        return set(data.get("tickers", []))
    except Exception:
        return set()

def save_alerted_today(alerted: set) -> None:
    import json
    myt = timezone(timedelta(hours=8))
    today = datetime.now(myt).date().isoformat()
    path = Path(__file__).parent / ALERTED_FILE
    path.write_text(json.dumps({"date": today, "tickers": sorted(alerted)}, indent=2))

def run_scan():
    start = datetime.now()
    log.info("=" * 60)
    log.info(f"Saham Alert starting at {start.strftime('%Y-%m-%d %H:%M')} MYT")
    log.info("=" * 60)

    if not is_market_open():
        return

    stocks = get_bursa_tickers()
    log.info(f"Scanning {len(stocks)} stocks | Price RM{MIN_PRICE}–RM{MAX_PRICE} | Breakout within {PENDING_BREAKOUT_PCT}% of 52WH")

    results = []
    done = 0

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

    alerted_today = load_alerted_today()
    new_results = [r for r in results if r["ticker"] not in alerted_today]
    skipped = len(results) - len(new_results)

    if skipped:
        log.info(f"Skipped {skipped} already-alerted stock(s) today")

    if not new_results:
        log.info("All signals already alerted today — nothing new to send.")
        return

    myt = timezone(timedelta(hours=8))
    now_myt = datetime.now(myt)
    summary = (
        f"<b>🔍 Saham Alert — Market Scan</b>\n"
        f"{now_myt.strftime('%d %b %Y  %H:%M MYT')}\n"
        f"Scanned: {len(stocks)} stocks\n"
        f"New signals: {len(new_results)}"
        + (f"  (skipped {skipped} repeat)" if skipped else "")
        + f"\n{'─' * 36}"
    )
    send_telegram(summary)
    time.sleep(DELAY_BETWEEN_MSGS)

    for r in new_results:
        msg = format_alert(r["ticker"], r["price"], r["signals"], r.get("name", ""))
        log.info(f"  → {r['ticker']} ({r.get('name','')}) {r['signals']}")
        send_telegram(msg)
        time.sleep(DELAY_BETWEEN_MSGS)
        alerted_today.add(r["ticker"])

    save_alerted_today(alerted_today)
    log.info(f"All alerts sent. {len(alerted_today)} unique stocks alerted today.")

if __name__ == "__main__":
    run_scan()
