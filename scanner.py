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

PENDING_BREAKOUT_PCT  =  7.0
VOLUME_SURGE_MULT     =  2.0
ATH_TOLERANCE         =  0.5
WH52_TOLERANCE        =  0.5
MIN_PRICE             =  0.205
MAX_PRICE             =  6.90
MIN_VOLUME            = 50_000
MAX_WORKERS           = 20
DELAY_BETWEEN_MSGS    =  1.0
ALERTED_FILE          = "alerted_today.json"

MALAYSIA_PUBLIC_HOLIDAYS = {
    date(2025,  1,  1), date(2025,  1, 29), date(2025,  1, 30),
    date(2025,  2,  1), date(2025,  3, 31), date(2025,  4,  1),
    date(2025,  5,  1), date(2025,  5, 12), date(2025,  6,  2),
    date(2025,  6,  7), date(2025,  6, 27), date(2025,  8, 31),
    date(2025,  9, 16), date(2025,  9, 26), date(2025, 10, 20),
    date(2025, 12, 25),
    date(2026,  1,  1), date(2026,  1, 19), date(2026,  2,  1),
    date(2026,  2, 17), date(2026,  2, 18), date(2026,  3, 20),
    date(2026,  3, 21), date(2026,  5,  1), date(2026,  5,  2),
    date(2026,  5, 27), date(2026,  6,  1), date(2026,  6, 17),
    date(2026,  8, 31), date(2026,  9, 15), date(2026,  9, 16),
    date(2026, 11,  9), date(2026, 12, 25),
}

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

def main():
    if not BOT_TOKEN or not CHAT_ID:
        raise SystemExit("BOT_TOKEN or CHAT_ID missing")

    msg = "Scanner test: workflow reached scanner.py"
    send_telegram(msg)

if __name__ == "__main__":
    main()
