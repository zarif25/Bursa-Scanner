import sys
import os
import csv
import json
import logging
import time as time_module
from datetime import datetime, time, timedelta, timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

import pandas as pd
import yfinance as yf
import requests
import holidays
import html

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")

# =============================================================================
# CONFIGURATION
# =============================================================================
# Intraday Bursa Scanner — runs every 30 minutes during Bursa trading hours
# (MYT), skipping the lunch break. Schedule is controlled by the GitHub
# Actions cron in scanner.yml; should_run() guards the trading window here.
#
# 1. PRE-FILTER (all 5 must pass before any indicator is checked)
#    a. Not Alerted Today : code not already in alerted_today.json
#    b. History Depth     : >= MIN_HISTORY_DAYS trading days of data
#    c. Price Range       : MIN_PRICE <= Close <= MAX_PRICE
#    d. Minimum Volume    : Volume > MIN_VOLUME
#    e. Positive Candle   : today's Close > yesterday's Open
#
# 2. TECHNICAL SIGNALS (any one triggers an alert)
#    - Price Up            : Close >= (1 + PRICE_UP_PCT) x Close from 2 days ago
#    - Golden Cross (GC)   : MA50 crosses above MA200 (today MA50 > MA200,
#                            yesterday MA50 <= MA200)
#    - 52-Week High (52WH) : Close >= 99.5% of 52-week High
#    - 2-Year High (2YH)   : Close >= 99.5% of 2-year High
#    - Volume Surge        : Volume >= VOLUME_SURGE_MULT x 20-day avg Volume
# =============================================================================

MIN_PRICE = 0.205
MAX_PRICE = 7.05
MIN_VOLUME = 50_000
MIN_HISTORY_DAYS = 250

PRICE_UP_PCT = 0.07          # 7% vs close 2 days ago
HIGH_PROXIMITY = 0.995       # within 0.5% of 52WH / 2YH
VOLUME_SURGE_MULT = 2.0      # 2x 20-day average volume
MA_FAST = 50
MA_SLOW = 200

# Bursa trading window (MYT). Lunch break 12:30-14:30; the 12:45 run is
# allowed so the morning-session close is captured.
MYT = timezone(timedelta(hours=8))
MARKET_OPEN = time(9, 0)
MARKET_CLOSE = time(17, 30)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STOCKS_FILE = os.path.join(BASE_DIR, "Bursa_Malaysia.csv")
DEDUP_FILE = os.path.join(BASE_DIR, "alerted_today.json")
CACHE_FILE = os.path.join(BASE_DIR, "history_cache.csv")
CACHE_META_FILE = os.path.join(BASE_DIR, "cache_meta.json")

# Fresh (non-cached) lookback window per run. 5 calendar days safely covers
# the previous close plus today's bar even across long weekends, without
# re-downloading the full 2y history every 30 minutes.
FRESH_LOOKBACK_PERIOD = "5d"

TELEGRAM_MAX_CHARS = 4096

SIG_PRICE_UP = "Price Up"
SIG_GC = "Golden Cross (GC)"
SIG_52WH = "52-Week High (52WH)"
SIG_2YH = "2-Year High (2YH)"
SIG_VOL = "Volume Surge"


# =============================================================================
# TICKER UNIVERSE
# =============================================================================
def _detect_delimiter(sample_line):
    """The source file has switched between comma- and tab-separated at
    least once already, so don't hardcode either -- sniff the first
    non-blank line each run. Defaults to comma if neither is present."""
    if "\t" in sample_line:
        return "\t"
    if "," in sample_line:
        return ","
    return ","


def load_tickers():
    """Reads Bursa_Malaysia.csv (no header row: code<sep>name) -> list of
    {"code": "1015.KL", "name": "AMBANK"}. The name column in this file
    carries a stray ".KL" suffix (e.g. "AMBANK.KL") which is stripped here
    so alerts show the plain company name. Delimiter (comma or tab) is
    auto-detected -- see _detect_delimiter()."""
    filename = os.path.basename(STOCKS_FILE)
    try:
        with open(STOCKS_FILE, "r", encoding="utf-8", newline="") as f:
            raw_lines = f.readlines()
        first_nonblank = next((ln for ln in raw_lines if ln.strip()), "")
        delimiter = _detect_delimiter(first_nonblank)
        with open(STOCKS_FILE, "r", encoding="utf-8", newline="") as f:
            reader = csv.reader(f, delimiter=delimiter)
            data = []
            skipped = 0
            for row_num, row in enumerate(reader, start=1):
                if not row or all(not cell.strip() for cell in row):
                    continue  # blank line
                if len(row) < 2:
                    logging.warning(f"⚠️ {filename} row {row_num}: expected 2 columns, got {row!r} — skipped.")
                    skipped += 1
                    continue
                code = row[0].strip()
                name = row[1].strip()
                if name.upper().endswith(".KL"):
                    name = name[:-3]
                if not code or not name:
                    logging.warning(f"⚠️ {filename} row {row_num}: empty code/name {row!r} — skipped.")
                    skipped += 1
                    continue
                data.append({"code": code, "name": name})
        logging.info(f"✅ Loaded {len(data)} tickers from {filename}" + (f" ({skipped} row(s) skipped)" if skipped else ""))
        return data
    except FileNotFoundError:
        logging.error(f"❌ {STOCKS_FILE} not found!")
        return []
    except Exception as e:
        logging.error(f"❌ Error reading {filename}: {e}")
        return []


STOCKS = load_tickers()


def get_bursa_tickers():
    """Ticker codes only (kept for test_setup.py compatibility)."""
    return [s.get("code") for s in STOCKS if s.get("code")]


# =============================================================================
# SCHEDULE / HOLIDAY GUARD
# =============================================================================
_cur_year = datetime.now().year
MY_HOLIDAYS = holidays.MY(years=[_cur_year - 1, _cur_year, _cur_year + 1])


def should_run(now=None):
    """Weekday, not a Malaysian public holiday, and inside trading hours."""
    if os.getenv("FORCE_RUN") == "true" or "--force" in sys.argv:
        logging.info("💪 Force run enabled. Bypassing schedule/holiday checks.")
        return True

    now = now or datetime.now(MYT)

    if now.weekday() >= 5:
        logging.info("📆 Weekend. Skipping scan.")
        return False
    if now.date() in MY_HOLIDAYS:
        logging.info(f"🎉 Malaysian public holiday ({MY_HOLIDAYS.get(now.date())}). Skipping scan.")
        return False
    if not (MARKET_OPEN <= now.time() <= MARKET_CLOSE):
        logging.info(f"⏰ {now.strftime('%H:%M')} MYT is outside trading hours. Skipping scan.")
        return False
    return True


# =============================================================================
# DEDUP (one alert per stock per day)
# =============================================================================
def get_today_str():
    return datetime.now(MYT).strftime("%Y-%m-%d")


def load_alerted_today():
    today = get_today_str()
    try:
        with open(DEDUP_FILE, "r") as f:
            data = json.load(f)
        if data.get("date") == today:
            return set(data.get("alerted", []))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return set()


def save_alerted_today(alerted_set):
    with open(DEDUP_FILE, "w") as f:
        json.dump({"date": get_today_str(), "alerted": sorted(alerted_set)}, f, indent=4)


# =============================================================================
# DATA
# =============================================================================
def normalise_df(df):
    """Flatten MultiIndex columns, capitalise names, drop rows without Close/Volume."""
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.copy()
    df.columns = [str(c).capitalize() for c in df.columns]
    return df.dropna(subset=["Close", "Volume"])


def get_history(ticker):
    df = yf.download(ticker, period="2y", interval="1d", progress=False, auto_adjust=False)
    return normalise_df(df)


def bulk_download(tickers, period="2y"):
    return yf.download(
        tickers,
        period=period,
        interval="1d",
        progress=False,
        group_by="ticker",
        auto_adjust=False,
        threads=True,
    )


def extract_ticker_df(df_all, ticker):
    if isinstance(df_all.columns, pd.MultiIndex):
        if ticker not in df_all.columns.get_level_values(0):
            return pd.DataFrame()
        return normalise_df(df_all[ticker])
    return normalise_df(df_all)


# =============================================================================
# HISTORY CACHE
# =============================================================================
# The last 2 years of OHLCV data for every ticker is static once a trading
# day has closed. Re-downloading all of it 15x/day is wasteful, so it is
# cached to disk and refreshed in full only once per calendar day. Every run
# still fetches a small FRESH_LOOKBACK_PERIOD window per ticker so today's
# price/volume is always live -- freshness every 30 minutes is unaffected.
CACHE_COLUMNS = ["Date", "Ticker", "Open", "High", "Low", "Close", "Volume"]


def load_cache_meta():
    try:
        with open(CACHE_META_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_cache_meta(baseline_date):
    with open(CACHE_META_FILE, "w") as f:
        json.dump({"baseline_date": baseline_date}, f, indent=4)


def cache_is_stale():
    """True if the cached baseline was not refreshed today (or doesn't exist)."""
    if not os.path.exists(CACHE_FILE):
        return True
    return load_cache_meta().get("baseline_date") != get_today_str()


def bulk_df_to_long(df_all, tickers):
    """Flatten a bulk_download() MultiIndex frame into long format:
    one row per (Date, Ticker). Rows dated today are dropped -- the baseline
    cache must only ever hold fully-closed trading days."""
    today = get_today_str()
    frames = []
    for ticker in tickers:
        df = extract_ticker_df(df_all, ticker)
        if df.empty:
            continue
        df = df.reset_index()
        df = df.rename(columns={df.columns[0]: "Date"})  # index col, whatever its name
        df["Date"] = pd.to_datetime(df["Date"]).dt.strftime("%Y-%m-%d")
        df = df[df["Date"] < today]  # exclude today's (possibly partial) bar
        if df.empty:
            continue
        df["Ticker"] = ticker
        frames.append(df[CACHE_COLUMNS])
    if not frames:
        return pd.DataFrame(columns=CACHE_COLUMNS)
    return pd.concat(frames, ignore_index=True)


def refresh_baseline_cache(tickers):
    """Full 2y re-download for every ticker in the universe; overwrites the cache.
    Runs once per calendar day (the first run after midnight MYT)."""
    logging.info(f"🔄 Refreshing baseline cache for {len(tickers)} tickers (once-daily, full 2y)...")
    df_all = bulk_download(tickers, period="2y")
    long_df = bulk_df_to_long(df_all, tickers)
    long_df.to_csv(CACHE_FILE, index=False)
    save_cache_meta(get_today_str())
    logging.info(f"💾 Baseline cache written: {len(long_df)} rows across {long_df['Ticker'].nunique()} tickers.")
    return long_df


def load_baseline_cache():
    """Returns {ticker: DataFrame(Date-indexed, Open/High/Low/Close/Volume)}."""
    if not os.path.exists(CACHE_FILE):
        return {}
    long_df = pd.read_csv(CACHE_FILE, parse_dates=["Date"])
    baseline = {}
    for ticker, g in long_df.groupby("Ticker"):
        baseline[ticker] = g.set_index("Date").sort_index()[["Open", "High", "Low", "Close", "Volume"]]
    return baseline


def build_full_history(baseline, fresh_df_all, ticker):
    """Combine the cached static baseline with this run's fresh lookback window
    for one ticker, returning a single ascending-date DataFrame ready for
    passes_prefilter()/compute_signals() -- identical shape to a full 2y download."""
    base = baseline.get(ticker, pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"]))
    fresh = extract_ticker_df(fresh_df_all, ticker)
    if fresh.empty:
        return base
    fresh = fresh.copy()
    fresh.index = pd.to_datetime(fresh.index)
    # Fresh data wins on any overlapping date (e.g. cache built before today's
    # close vs. an intraday partial bar) -- it is always the more current read.
    combined = pd.concat([base, fresh])
    combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    return combined


# =============================================================================
# PRE-FILTER
# =============================================================================
def passes_prefilter(df, name, ticker):
    """Returns (ok, reason). Conditions b–e; condition a (dedup) is applied in main()."""
    # b. History depth
    if len(df) < MIN_HISTORY_DAYS:
        return False, f"only {len(df)} days of history (< {MIN_HISTORY_DAYS})"

    latest = df.iloc[-1]
    prev = df.iloc[-2]
    close = float(latest["Close"])
    volume = float(latest["Volume"])
    prev_open = float(prev["Open"])

    # c. Price range
    if not (MIN_PRICE <= close <= MAX_PRICE):
        return False, f"price {close:.3f} out of range ({MIN_PRICE}–{MAX_PRICE})"

    # d. Minimum volume (strictly greater than)
    if volume <= MIN_VOLUME:
        return False, f"volume {volume:,.0f} <= {MIN_VOLUME:,.0f}"

    # e. Positive candle: Close > yesterday's Open
    if close <= prev_open:
        return False, f"close {close:.3f} not above yesterday's open {prev_open:.3f}"

    return True, ""


# =============================================================================
# SIGNAL ENGINE
# =============================================================================
def compute_signals(df):
    """Returns list of triggered signal names. Assumes df already passed pre-filter."""
    if len(df) < MIN_HISTORY_DAYS:
        return []

    df = df.copy()
    df["MA50"] = df["Close"].rolling(MA_FAST).mean()
    df["MA200"] = df["Close"].rolling(MA_SLOW).mean()
    # 20-day average volume EXCLUDING today's bar, so today's spike does not
    # dilute its own benchmark (with today included, a 2x day only reads ~1.9x).
    df["Vol20"] = df["Volume"].shift(1).rolling(20).mean()

    latest = df.iloc[-1]
    prev = df.iloc[-2]
    close = float(latest["Close"])
    signals = []

    # Price Up: >= 7% above close 2 days ago
    if len(df) >= 3:
        close_2d_ago = float(df.iloc[-3]["Close"])
        if close_2d_ago > 0 and close >= close_2d_ago * (1 + PRICE_UP_PCT):
            signals.append(SIG_PRICE_UP)

    # Golden Cross: MA50 crosses above MA200 today
    if not pd.isna(latest["MA50"]) and not pd.isna(latest["MA200"]) \
            and not pd.isna(prev["MA50"]) and not pd.isna(prev["MA200"]):
        if float(latest["MA50"]) > float(latest["MA200"]) and float(prev["MA50"]) <= float(prev["MA200"]):
            signals.append(SIG_GC)

    # 52-Week High
    high_52w = float(df.tail(252)["High"].max())
    if high_52w > 0 and close >= high_52w * HIGH_PROXIMITY:
        signals.append(SIG_52WH)

    # 2-Year High (full 2y download window)
    high_2y = float(df["High"].max())
    if high_2y > 0 and close >= high_2y * HIGH_PROXIMITY:
        signals.append(SIG_2YH)

    # Volume Surge: >= 2x 20-day average
    vol20 = latest["Vol20"]
    if not pd.isna(vol20) and float(vol20) > 0 and float(latest["Volume"]) >= float(vol20) * VOLUME_SURGE_MULT:
        signals.append(SIG_VOL)

    return signals


def analyze(ticker):
    """Single-ticker analysis (kept for test_setup.py compatibility)."""
    try:
        df = get_history(ticker)
        ok, _ = passes_prefilter(df, ticker, ticker)
        if not ok:
            return None
        signals = compute_signals(df)
        return {"signals": signals} if signals else None
    except Exception as e:
        logging.error(f"Error analyzing {ticker}: {e}")
        return None


# =============================================================================
# TELEGRAM
# =============================================================================
def send_telegram(message):
    token = (os.getenv("BOT_TOKEN") or "").strip().strip('"').strip("'")
    chat_id = (os.getenv("CHAT_ID") or "").strip().strip('"').strip("'")
    if not token or not chat_id:
        logging.warning("⚠️ Telegram credentials missing (BOT_TOKEN / CHAT_ID).")
        return False

    def _post(target_chat_id):
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {"chat_id": target_chat_id, "text": message, "parse_mode": "HTML"}
        return requests.post(url, data=payload, timeout=20)

    try:
        r = _post(chat_id)
        if r.ok:
            return True

        resp_json = r.json() if r.content else {}
        desc = str(resp_json.get("description", ""))

        if "chat not found" in desc.lower() and not chat_id.startswith("-"):
            fallbacks = [f"-{chat_id}"]
            if not chat_id.startswith("100"):
                fallbacks.append(f"-100{chat_id}")
            for fb in fallbacks:
                logging.info(f"🔄 Retrying with fallback chat_id: '{fb}'...")
                if _post(fb).ok:
                    logging.info(f"✅ Succeeded using fallback chat_id: '{fb}'")
                    return True

        if r.status_code == 429:
            try:
                retry_after = int(resp_json.get("parameters", {}).get("retry_after", 30))
            except Exception:
                retry_after = 30
            logging.warning(f"⚠️ Telegram rate limit (429). Retrying after {retry_after}s...")
            time_module.sleep(retry_after)
            r_retry = _post(chat_id)
            if r_retry.ok:
                return True
            logging.error(f"❌ Telegram retry failed: {r_retry.text}")
            return False

        logging.error(f"❌ Telegram HTTP Error: {r.text}")
        if "chat not found" in desc.lower():
            logging.error(
                "💡 'chat not found' checklist:\n"
                "   1. Bot must be added to the channel/group and promoted to ADMIN.\n"
                "   2. CHAT_ID format: channels -100xxxxxxxxxx, groups -xxxxxxxxx,\n"
                "      personal chat requires /start to the bot first.\n"
                "   3. Verify with @getmyid_bot."
            )
        return False
    except Exception as e:
        logging.error(f"❌ Telegram request failed: {e}")
        return False


def format_results(results, run_time=None):
    """Per-stock block alert, split into <=4096-char chunks.

    📊 Bursa Scanner — 2026-09-07 10:45 MYT

    <b>AMBANK (1015)</b>
    Current: RM 6.88
    Signals detected:
     - Price Up
     - 52-Week High (52WH)

    <b>ABMB (2488)</b>
    Current: RM 4.98
    Signals detected:
     - 52-Week High (52WH)
    """
    if not results:
        return []

    run_time = run_time or datetime.now(MYT)
    header = f"<b>📊 Bursa Scanner — {run_time.strftime('%Y-%m-%d %H:%M')} MYT</b>\n\n"

    entries = []
    for r in results:
        code = r["ticker"].split(".")[0]
        name = html.escape(r["name"])
        price = r["price"]
        price_str = f"{price:.2f}" if abs(price - round(price, 2)) < 1e-5 else f"{price:.3f}"
        sig_lines = "\n".join(f" - {sig}" for sig in r["signals"])
        entry = (
            f"<b>{name} ({code})</b>\n"
            f"Current: RM {price_str}\n"
            f"Signals detected:\n"
            f"{sig_lines}"
        )
        entries.append(entry)

    messages, chunk, cur_len = [], [], len(header)
    for entry in entries:
        entry_len = len(entry) + 2  # +2 for the blank-line separator between entries
        if chunk and cur_len + entry_len > TELEGRAM_MAX_CHARS - 50:
            messages.append(header + "\n\n".join(chunk))
            chunk, cur_len = [entry], len(header) + entry_len
        else:
            chunk.append(entry)
            cur_len += entry_len
    if chunk:
        messages.append(header + "\n\n".join(chunk))
    return messages


# =============================================================================
# MAIN
# =============================================================================
def main():
    logging.info("🤖 Starting Bursa Scanner...")

    if not should_run():
        logging.info("⏹️ Skipped (weekend / holiday / outside trading hours).")
        return
    if not STOCKS:
        logging.error("❌ No stocks loaded. Exiting.")
        return

    # Baseline history cache: full 2y re-download once per calendar day for the
    # WHOLE universe (not just today's remaining stocks -- tomorrow needs all
    # of them again). Every other run of the day reuses this on disk.
    all_tickers = get_bursa_tickers()
    if cache_is_stale():
        try:
            refresh_baseline_cache(all_tickers)
        except Exception as e:
            logging.error(f"❌ Baseline cache refresh failed: {e}")
            if not os.path.exists(CACHE_FILE):
                logging.error("❌ No usable cache exists. Exiting.")
                return
            logging.warning("⚠️ Continuing with yesterday's cached baseline.")
    else:
        logging.info("📦 Baseline cache is fresh for today, reusing it.")

    baseline = load_baseline_cache()

    # Pre-filter (a): not alerted today
    alerted_set = load_alerted_today()
    logging.info(f"📋 {len(alerted_set)} stock(s) already alerted today.")

    stocks_to_scan = [s for s in STOCKS if s.get("code") and s["code"] not in alerted_set]
    skipped = len(STOCKS) - len(stocks_to_scan)
    if skipped:
        logging.info(f"⏳ {skipped} stock(s) skipped — already alerted today.")
    if not stocks_to_scan:
        logging.info("📋 Nothing left to scan today.")
        return

    tickers = [s["code"] for s in stocks_to_scan]
    logging.info(f"⬇️ Fetching fresh {FRESH_LOOKBACK_PERIOD} window for {len(tickers)} tickers...")
    try:
        fresh_df_all = bulk_download(tickers, period=FRESH_LOOKBACK_PERIOD)
    except Exception as e:
        logging.error(f"❌ Fresh data download failed: {e}")
        return

    results = []
    stats = {"no_data": 0, "stale": 0, "prefilter": 0, "no_signal": 0, "error": 0}
    today = get_today_str()

    for stock in stocks_to_scan:
        ticker, name = stock["code"], stock.get("name", stock["code"])
        try:
            df = build_full_history(baseline, fresh_df_all, ticker)
            if df.empty:
                stats["no_data"] += 1
                logging.info(f"📊 {name} ({ticker}): no data, skip.")
                continue

            # If the fresh fetch failed for this ticker (rate limit, transient
            # error, or Yahoo hasn't posted today's bar yet), df's latest row
            # is still yesterday's. Evaluating it as if it were "today" would
            # silently re-run yesterday's already-seen numbers -- skip instead
            # and let a later run (this ticker will very likely succeed next
            # time) pick it up with genuinely fresh data.
            if df.index.max().strftime("%Y-%m-%d") != today:
                stats["stale"] += 1
                logging.info(f"🕒 {name} ({ticker}): no fresh bar for today yet, skip this run.")
                continue

            ok, reason = passes_prefilter(df, name, ticker)
            if not ok:
                stats["prefilter"] += 1
                logging.info(f"🚧 {name} ({ticker}): pre-filter failed — {reason}")
                continue

            signals = compute_signals(df)
            if not signals:
                stats["no_signal"] += 1
                logging.info(f"🚫 {name} ({ticker}): no signal.")
                continue

            results.append({
                "ticker": ticker,
                "name": name,
                "price": float(df.iloc[-1]["Close"]),
                "signals": signals,
            })
            logging.info(f"✅ {name} ({ticker}): {' | '.join(signals)}")
        except Exception as e:
            stats["error"] += 1
            logging.error(f"❌ {name} ({ticker}): {e}")

    logging.info(
        f"Scan done. {len(results)} hit(s) | no_data={stats['no_data']} stale={stats['stale']} "
        f"prefilter={stats['prefilter']} no_signal={stats['no_signal']} error={stats['error']}"
    )

    if not results:
        logging.info("📭 No signals this run. No Telegram message sent.")
        return

    results.sort(key=lambda x: x["ticker"])
    messages = format_results(results)
    logging.info(f"📨 Sending {len(messages)} message(s) for {len(results)} stock(s)...")

    all_sent = True
    for i, msg in enumerate(messages, start=1):
        if send_telegram(msg):
            logging.info(f"🚀 Sent {i}/{len(messages)}.")
        else:
            logging.error(f"❌ Failed to send {i}/{len(messages)}.")
            all_sent = False
        time_module.sleep(0.5)

    if all_sent:
        alerted_set.update(r["ticker"] for r in results)
        save_alerted_today(alerted_set)
        logging.info(f"💾 alerted_today.json updated ({len(alerted_set)} total today).")
    else:
        logging.error("❌ Not all messages sent; alerted_today.json left unchanged.")
        sys.exit(1)


if __name__ == "__main__":
    main()
