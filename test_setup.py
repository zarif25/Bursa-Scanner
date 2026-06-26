"""
Quick test script — run this locally before deploying to GitHub Actions.

Usage:
    python test_setup.py

Checks:
  1. Python dependencies are installed
  2. Telegram bot token and chat ID work
  3. A sample stock (MAYBANK) can be downloaded and analysed
  4. Signal detection produces sensible output
"""

import sys
import os

print("\n── Nota Saham Alert V4 — Setup Test ──────────────────────\n")

# ── 1. Dependency check ────────────────────────────────────────────────────────
print("1. Checking dependencies...")
missing = []
for pkg in ["yfinance", "pandas", "requests"]:
    try:
        __import__(pkg)
        print(f"   ✓ {pkg}")
    except ImportError:
        print(f"   ✗ {pkg}  ← run: pip install -r requirements.txt")
        missing.append(pkg)

if missing:
    sys.exit("\nInstall missing packages first.")

import yfinance as yf
import pandas as pd
import requests

# ── 2. Telegram test ───────────────────────────────────────────────────────────
print("\n2. Checking Telegram credentials...")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID   = os.environ.get("CHAT_ID", "")

if not BOT_TOKEN:
    print("   ✗ BOT_TOKEN not set — export BOT_TOKEN=your_token")
elif not CHAT_ID:
    print("   ✗ CHAT_ID not set  — export CHAT_ID=your_chat_id")
else:
    url  = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {
        "chat_id":    CHAT_ID,
        "text":       "✅ <b>Nota Saham Alert V4</b>\nSetup test successful! Scanner is ready.",
        "parse_mode": "HTML",
    }
    try:
        r = requests.post(url, json=data, timeout=10)
        r.raise_for_status()
        print("   ✓ Telegram message sent — check your channel/group")
    except Exception as e:
        print(f"   ✗ Telegram error: {e}")
        print("     Double-check BOT_TOKEN, CHAT_ID, and that bot is admin in the channel")

# ── 3. Data download test ──────────────────────────────────────────────────────
print("\n3. Testing data download (Maybank 1155.KL)...")
try:
    df = yf.download("1155.KL", period="2y", interval="1d",
                     progress=False, auto_adjust=True)
    if df is not None and not df.empty:
        print(f"   ✓ Downloaded {len(df)} rows  (latest: {df.index[-1].date()})")
    else:
        print("   ✗ Empty dataframe — check internet connection")
except Exception as e:
    print(f"   ✗ Download error: {e}")

# ── 4. Signal detection test ───────────────────────────────────────────────────
print("\n4. Testing signal detection...")
try:
    from scanner import analyze
    result = analyze("1155.KL")
    if result:
        print(f"   ✓ Signal detected for 1155.KL:")
        for sig in result["signals"]:
            print(f"     {sig}")
    else:
        print("   ✓ No signals for 1155.KL today (this is normal)")
except Exception as e:
    print(f"   ✗ Signal detection error: {e}")

# ── 5. Stock list test ─────────────────────────────────────────────────────────
print("\n5. Testing stock list fetch...")
try:
    from scanner import get_bursa_tickers
    tickers = get_bursa_tickers()
    print(f"   ✓ Got {len(tickers)} tickers  (first 5: {tickers[:5]})")
except Exception as e:
    print(f"   ✗ Stock list error: {e}")

print("\n── Test complete ──────────────────────────────────────────\n")
print("Next steps:")
print("  1. Push this folder to GitHub")
print("  2. Add BOT_TOKEN and CHAT_ID to repo Secrets")
print("  3. Enable GitHub Actions → the scanner runs automatically\n")
