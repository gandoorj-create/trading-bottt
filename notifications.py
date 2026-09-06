"""
notifications.py
Telegram руу мессеж болон зураг илгээх.
"""
import requests
from settings import *
from logging_setup import get_logger

log = get_logger(__name__)


def send_telegram(text, pin=False):
    if not BOT_TOKEN or not CHAT_ID:
        return False
    if text and len(text) > 4096:
        text = text[:4000] + "\n… (truncated)"
    try:
        url = f"{TELEGRAM_API_ROOT}/bot{BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": CHAT_ID,
            "text": text,
        }
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code != 200:
            log.error(f"❌ Telegram error: {response.text}")
            return False
        result = response.json()
        if pin and result.get("ok"):
            message_id = result["result"]["message_id"]
            pin_url = f"{TELEGRAM_API_ROOT}/bot{BOT_TOKEN}/pinChatMessage"
            requests.post(pin_url, json={"chat_id": CHAT_ID, "message_id": message_id}, timeout=10)
        return True
    except Exception as e:
        log.error(f"❌ Telegram exception: {e}")
        return False
