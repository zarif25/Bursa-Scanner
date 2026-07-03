import os
import json
import logging
from datetime import datetime, time, timedelta

import pandas as pd
import yfinance as yf
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")

def load_tickers():
    """Reads stocks.json and extracts the ticker codes and names."""
    try:
        with open("stocks.json", "r") as f:
            data = json.load(f)
            logging.info(f"✅ Successfully loaded {len(data)} tickers from stocks.json")
            return data
    except FileNotFoundError:
        logging.error("❌ stocks.json file not found! Make sure it is in the same directory.")
        return []
    except Exception as e:
        logging.error(f"❌ Error reading stocks.json: {e}")
        return []

STOCKS = load_tickers()

# Updated to match your scanner.yml file exactly
TELEGRAM_BOT_TOKEN = os.getenv("BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("CHAT_ID")

MARKET_OPEN = time(9, 0)
MARKET_CLOSE = time(17, 0)

def in_trading_hours(now=None):
    now = now or datetime.utcnow() + timedelta(hours=8)
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE

def get_history(ticker):
    df = yf.download(
        ticker,
        period="max",
        interval="1d",
        progress=False,
        auto_adjust=False
    )
    if df is None or df.empty:
        return pd.DataFrame()
    
    # Fix for newer yfinance versions returning MultiIndex columns
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
        
    df = df.dropna().copy()
    df.columns = [str(c).capitalize() for c in df.columns]
    return df

def crossed_above(prev_a, prev_b, curr_a, curr_b):
    return prev_a <= prev_b and curr_a > curr_b

def compute_signals(df):
    if len(df) < 250:
        return []

    df = df.copy()
    df["MA5"] = df["Close"].rolling(5).mean()
    df["MA20"] = df["Close"].rolling(20).mean()
    df["EMA5"] = df["Close"].ewm(span=5, adjust=False).mean()
    df["EMA20"] = df["Close"].ewm(span=20, adjust=False).mean()
    df["EMA200"] = df["Close"].ewm(span=200, adjust=False).mean()
    df["Vol20"] = df["Volume"].rolling(20).mean()

    latest = df.iloc[-1]
    prev = df.iloc[-2]
    yesterday = df.iloc[-2]
    current_price = float(latest["Close"])
    yesterday_open = float(yesterday["Open"])

    if current_price <= yesterday_open:
        return []

    signals = []

    prev2_close = float(df.iloc[-3]["Close"])
    if current_price >= prev2_close * 1.07:
        signals.append("Price Up")

    if not pd.isna(prev["MA5"]) and not pd.isna(prev["MA20"]) and not pd.isna(latest["MA5"]) and not pd.isna(latest["MA20"]):
        if crossed_above(prev["MA5"], prev["MA20"], latest["MA5"], latest["MA20"]):
            signals.append("Golden Cross (GC)")

    if not pd.isna(latest["EMA5"]) and not pd.isna(latest["EMA20"]) and not pd.isna(latest["EMA200"]):
        if (
            current_price > latest["EMA5"]
            and current_price > latest["EMA20"]
            and current_price > latest["EMA200"]
            and latest["EMA5"] > latest["EMA20"]
        ):
            signals.append("Bullish Zone")

    high_52w = float(df.tail(252)["High"].max())
    if current_price >= high_52w * 0.995:
        signals.append("52-Week High (52WH)")

    all_time_high = float(df["High"].max())
    if current_price >= all_time_high * 0.995:
        signals.append("All-Time High (ATH)")

    if current_price >= high_52w * 0.93:
        signals.append("Pending Breakout")

    if not pd.isna(latest["Vol20"]) and float(latest["Volume"]) >= float(latest["Vol20"]) * 2:
        signals.append("Volume Surge")

    return signals

def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("⚠️ Telegram credentials missing in GitHub Secrets.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    r = requests.post(url, data=payload, timeout=20)
    r.raise_for_status()
    return True

# Updated to accept 'name' and format it as "NAME (CODE)"
def format_message(ticker, name, signals, price, open_price):
    lines = [
        f"*{name} ({ticker})*",
        f"Current: {price:.2f}",
        f"Yesterday Open: {open_price:.2f}",
        "",
        "Signals detected:",
    ]
    for s in signals:
        lines.append(f" - {s}")
    return "\n".join(lines)

def scan_ticker(ticker, name):
    if not in_trading_hours():
        logging.info(f"⏰ {name} ({ticker}): Outside trading hours, skip.")
        return

    try:
        df = get_history(ticker)
        if df.empty:
            logging.info(f"📊 {name} ({ticker}): No data found, skip.")
            return

        signals = compute_signals(df)
        if not signals:
            logging.info(f"🚫 {name} ({ticker}): No signal triggered.")
            return

        latest = df.iloc[-1]
        yesterday = df.iloc[-2]
        msg = format_message(
            ticker,
            name,
            signals,
            float(latest["Close"]),
            float(yesterday["Open"])
        )
        
        send_telegram(msg)
        logging.info(f"🚀 {name} ({ticker}): Sent to Telegram! Signals: {signals}")
        
    except Exception as e:
        logging.error(f"❌ {name} ({ticker}): Error occurred - {e}")

def main():
    logging.info("🤖 Starting Bursa Malaysia Scanner...")
    
    if not STOCKS:
        logging.error("❌ No stocks loaded. Exiting.")
        return

    # Loop through the list of dictionaries
    for stock in STOCKS:
        ticker = stock.get("code")
        name = stock.get("name", ticker) # Fallback to ticker if name is missing
        if ticker:
            scan_ticker(ticker, name)
        
    logging.info("✅ Scan finished.")

if __name__ == "__main__":
    main()
