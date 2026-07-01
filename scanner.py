import os
import requests
from pathlib import Path

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message}
    r = requests.post(url, json=payload, timeout=20)
    r.raise_for_status()
    return r.json()

def main():
    if not BOT_TOKEN or not CHAT_ID:
        raise SystemExit("BOT_TOKEN or CHAT_ID missing")

    alerted = Path("alerted_today.json")
    alerted.write_text("{}", encoding="utf-8")

    msg = "Bursa scanner test: workflow started successfully"
    result = send_telegram(msg)
    print(result)

if __name__ == "__main__":
    main()
