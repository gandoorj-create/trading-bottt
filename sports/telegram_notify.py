"""
telegram_notify.py
Sports bot-ын Telegram мэдэгдэл — crypto ботын notifications.py-тай код
хуваалцахгүй, бүрэн тусдаа функц.
"""
import logging
import requests
from sports_config import SPORTS_BOT_TOKEN, SPORTS_CHAT_ID, TELEGRAM_API_ROOT

log = logging.getLogger(__name__)


def send_telegram(text, bot_token=None, chat_id=None):
    token = bot_token or SPORTS_BOT_TOKEN
    chat = chat_id or SPORTS_CHAT_ID
    if not token or not chat:
        return False
    if text and len(text) > 4096:
        text = text[:4000] + "\n… (truncated)"
    try:
        url = f"{TELEGRAM_API_ROOT}/bot{token}/sendMessage"
        response = requests.post(url, json={"chat_id": chat, "text": text}, timeout=10)
        if response.status_code != 200:
            log.error(f"❌ Telegram error: {response.text}")
            return False
        return True
    except Exception as e:
        log.error(f"❌ Telegram exception: {e}")
        return False
