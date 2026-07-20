import os
import json
import logging
import time as time_module
from datetime import datetime, time, timedelta, timezone

import pandas as pd
import yfinance as yf
import requests
import holidays


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")

# --- CONFIGURATION ---
MIN_PRICE = 0.205
MAX_PRICE = 7.05
MIN_VOLUME = 50_000
PENDING_BREAKOUT_PCT = 7.0
VOLUME_SURGE_MULT = 2.0
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

def get_bursa_tickers():
    """Returns a list of ticker codes for test_setup.py compatibility."""
    return [stock.get("code") for stock in STOCKS if stock.get("code")]

def analyze(ticker):
    """Runs signal analysis for a single ticker for test_setup.py compatibility."""
    try:
        df = get_history(ticker)
        if df.empty:
            return None
        signals = compute_signals(df)
        if signals:
            return {"signals": signals}
    except Exception as e:
        logging.error(f"Error analyzing {ticker}: {e}")
    return None

# Match the names in your scanner.yml file
TELEGRAM_BOT_TOKEN = os.getenv("BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("CHAT_ID")


# Instantiate Malaysian holidays
MY_HOLIDAYS = holidays.MY()

def should_run():
    """Check if it's a weekday, not a holiday, and within trading hours."""
    import sys
    if os.getenv("FORCE_RUN") == "true" or "--force" in sys.argv:
        logging.info("💪 Force run enabled. Bypassing schedule/holiday checks.")
        return True

    now = datetime.now(timezone(timedelta(hours=8)))
    
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
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")


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
        period="2y",
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
    df["MA50"] = df["Close"].rolling(50).mean()
    df["MA200"] = df["Close"].rolling(200).mean()
    df["EMA5"] = df["Close"].ewm(span=5, adjust=False).mean()
    df["EMA20"] = df["Close"].ewm(span=20, adjust=False).mean()
    df["EMA50"] = df["Close"].ewm(span=50, adjust=False).mean()
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

    # Golden Cross (MA50 crossing above MA200)
    if not pd.isna(prev["MA50"]) and not pd.isna(prev["MA200"]) and not pd.isna(latest["MA50"]) and not pd.isna(latest["MA200"]):
        if crossed_above(prev["MA50"], prev["MA200"], latest["MA50"], latest["MA200"]):
            signals.append("Golden Cross (GC)")

    # Bullish Zone (Price > EMA20 > EMA50 > EMA200)
    if not pd.isna(latest["EMA20"]) and not pd.isna(latest["EMA50"]) and not pd.isna(latest["EMA200"]):
        if current_price > latest["EMA20"] > latest["EMA50"] > latest["EMA200"]:
            signals.append("Bullish Zone")

    high_52w = float(df.tail(252)["High"].max())
    if current_price >= high_52w * 0.995:
        signals.append("52-Week High (52WH)")

    all_time_high = float(df["High"].max())
    if current_price >= all_time_high * 0.995:
        signals.append("2-Year High (2YH)")

    # Pending Breakout (within PENDING_BREAKOUT_PCT of 52WH)
    if high_52w * (1 - PENDING_BREAKOUT_PCT / 100) <= current_price < high_52w * 0.995:
        signals.append("Pending Breakout")

    # Volume Surge (volume >= VOLUME_SURGE_MULT * 20-day average)
    if not pd.isna(latest["Vol20"]) and float(latest["Volume"]) >= float(latest["Vol20"]) * VOLUME_SURGE_MULT:
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

def scan_ticker(ticker, name, alerted_set=None):
    try:
        df = get_history(ticker)
        if df.empty:
            logging.info(f"📊 {name} ({ticker}): No data found, skip.")
            return None

        # --- PRE-CONDITIONS ---
        # 1. Price Range check
        current_price = float(df.iloc[-1]["Close"])
        if not (MIN_PRICE <= current_price <= MAX_PRICE):
            logging.info(f"💰 {name} ({ticker}): Price {current_price:.3f} out of range ({MIN_PRICE} - {MAX_PRICE}), skip.")
            return None

        # 2. Volume filter (must be above MIN_VOLUME)
        current_volume = float(df.iloc[-1]["Volume"])
        if current_volume <= MIN_VOLUME:
            logging.info(f"📊 {name} ({ticker}): Volume {current_volume:,.0f} <= {MIN_VOLUME:,.0f}, skip.")
            return None

        signals = compute_signals(df)
        if not signals:
            logging.info(f"🚫 {name} ({ticker}): No signal triggered.")
            return None

        return {
            "ticker": ticker,
            "name": name,
            "price": current_price,
            "signals": signals
        }
        
    except Exception as e:
        logging.error(f"❌ {name} ({ticker}): Error occurred - {e}")
        return None

def main():
    from concurrent.futures import ThreadPoolExecutor, as_completed

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

    stocks_to_scan = []
    for stock in STOCKS:
        ticker = stock.get("code")
        name = stock.get("name", ticker)
        if ticker in alerted_set:
            logging.info(f"⏳ {name} ({ticker}): Already alerted today, skip.")
        elif ticker:
            stocks_to_scan.append(stock)

    results = []
    max_workers = 15
    logging.info(f"Scanning {len(stocks_to_scan)} stocks in parallel (max_workers={max_workers})...")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_stock = {
            executor.submit(scan_ticker, s.get("code"), s.get("name", s.get("code"))): s 
            for s in stocks_to_scan
        }
        for future in as_completed(future_to_stock):
            res = future.result()
            if res:
                results.append(res)

    logging.info(f"Scan finished. Found {len(results)} stocks with signals.")

    # Process alerts sequentially in the main thread
    if results:
        results.sort(key=lambda x: x["ticker"])
        for res in results:
            ticker = res["ticker"]
            name = res["name"]
            price = res["price"]
            signals = res["signals"]
            
            # Format and send Telegram alert
            msg = format_message(ticker, name, signals, price)
            if send_telegram(msg):
                logging.info(f"🚀 {name} ({ticker}): Sent to Telegram! Signals: {signals}")
                alerted_set.add(ticker)
                save_alerted_today(alerted_set)
                # Sleep briefly between messages to respect Telegram rate limits
                time_module.sleep(0.5)
            else:
                logging.error(f"❌ {name} ({ticker}): Failed to send to Telegram.")

if __name__ == "__main__":
    main()
