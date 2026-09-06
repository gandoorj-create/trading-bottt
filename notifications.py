"""
notifications.py
Telegram руу мессеж болон зураг илгээх.
"""
import requests
from settings import *
from logging_setup import get_logger

log = get_logger(__name__)


def send_telegram(text, pin=False, bot_token=None, chat_id=None):
    """Telegram руу мессеж явуулна.

    bot_token/chat_id өгөөгүй бол crypto ботын өгөгдмөл (BOT_TOKEN/CHAT_ID)
    хэрэглэнэ. sports_bot.py өөрийн тусдаа bot-оор дуудахдаа эдгээрийг дамжуулна.
    """
    token = bot_token or BOT_TOKEN
    chat = chat_id or CHAT_ID
    if not token or not chat:
        return False
    if text and len(text) > 4096:
        text = text[:4000] + "\n… (truncated)"
    try:
        url = f"{TELEGRAM_API_ROOT}/bot{token}/sendMessage"
        payload = {
            "chat_id": chat,
            "text": text,
        }
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code != 200:
            log.error(f"❌ Telegram error: {response.text}")
            return False
        result = response.json()
        if pin and result.get("ok"):
            message_id = result["result"]["message_id"]
            pin_url = f"{TELEGRAM_API_ROOT}/bot{token}/pinChatMessage"
            requests.post(pin_url, json={"chat_id": chat, "message_id": message_id}, timeout=10)
        return True
    except Exception as e:
        log.error(f"❌ Telegram exception: {e}")
        return False
