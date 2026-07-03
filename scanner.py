import os
import json
import logging
from datetime import datetime, time, timedelta

import pandas as pd
import yfinance as yf
import requests
import holidays

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")

# --- CONFIGURATION ---
MIN_PRICE = 0.205
MAX_PRICE = 7.05
DEDUP_FILE = "alerted_today.json"

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

# Match the names in your scanner.yml file
TELEGRAM_BOT_TOKEN = os.getenv("BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("CHAT_ID")

# Instantiate Malaysian holidays
MY_HOLIDAYS = holidays.MY()

def should_run():
    """Check if it's a weekday, not a holiday, and within trading hours."""
    now = datetime.utcnow() + timedelta(hours=8)
    
    # Check if weekend (Saturday=5, Sunday=6)
    if now.weekday() >= 5:
        logging.info("📆 Today is a weekend. Skipping scan.")
        return False
        
    # Check if public holiday in Malaysia
    if now.date() in MY_HOLIDAYS:
        logging.info("🎉 Today is a Malaysian Public Holiday. Skipping scan.")
        return False

    # Check if within market hours (9:00 AM to 5:30 PM to allow the 5:15 PM scan)
    return time(9, 0) <= now.time() <= time(17, 30)

# --- DEDUP LOGIC ---
def get_today_str():
    # Get today's date in Malaysia Time
    return (datetime.utcnow() + timedelta(hours=8)).strftime("%Y-%m-%d")

def load_alerted_today():
    """Loads the list of already alerted stocks for today."""
    today = get_today_str()
    try:
        with open(DEDUP_FILE, "r") as f:
            data = json.load(f)
            # If the file is from a previous day, reset the list
            if data.get("date") == today:
                return set(data.get("alerted", []))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return set()

def save_alerted_today(alerted_set):
    """Saves the updated list back to the JSON file."""
    today = get_today_str()
    data = {
        "date": today,
        "alerted": list(alerted_set)
    }
    with open(DEDUP_FILE, "w") as f:
        json.dump(data, f, indent=4)

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

def format_message(ticker, name, signals, price):
    lines = [
        f"*{name} ({ticker})*",
        f"Current: {price:.2f}",
        "",
        "Signals detected:",
    ]
    for s in signals:
        lines.append(f" - {s}")
    return "\n".join(lines)

def scan_ticker(ticker, name, alerted_set):
    # Check if already alerted today
    if ticker in alerted_set:
        logging.info(f"⏳ {name} ({ticker}): Already alerted today, skip.")
        return

    try:
        df = get_history(ticker)
        if df.empty:
            logging.info(f"📊 {name} ({ticker}): No data found, skip.")
            return

        # --- PRICE FILTER ---
        current_price = float(df.iloc[-1]["Close"])
        if not (MIN_PRICE <= current_price <= MAX_PRICE):
            logging.info(f"💰 {name} ({ticker}): Price {current_price:.3f} out of range ({MIN_PRICE} - {MAX_PRICE}), skip.")
            return

        signals = compute_signals(df)
        if not signals:
            logging.info(f"🚫 {name} ({ticker}): No signal triggered.")
            return

        latest = df.iloc[-1]
        msg = format_message(
            ticker,
            name,
            signals,
            float(latest["Close"])
        )
        
        # Send message
        if send_telegram(msg):
            logging.info(f"🚀 {name} ({ticker}): Sent to Telegram! Signals: {signals}")
            # Add to dedup list and save immediately
            alerted_set.add(ticker)
            save_alerted_today(alerted_set)
        else:
            logging.error(f"❌ {name} ({ticker}): Failed to send to Telegram.")
        
    except Exception as e:
        logging.error(f"❌ {name} ({ticker}): Error occurred - {e}")

def main():
    logging.info("🤖 Starting Bursa Malaysia Scanner...")
    
    # Check if we should run today
    if not should_run():
        logging.info("⏹️ Script finished early due to weekend, holiday, or outside trading hours.")
        return

    if not STOCKS:
        logging.error("❌ No stocks loaded. Exiting.")
        return

    # Load today's already alerted stocks
    alerted_set = load_alerted_today()
    logging.info(f"📋 {len(alerted_set)} stocks have already been alerted today.")

    for stock in STOCKS:
        ticker = stock.get("code")
        name = stock.get("name", ticker)
        if ticker:
            scan_ticker(ticker, name, alerted_set)
        
    logging.info("✅ Scan finished.")

if __name__ == "__main__":
    main()
