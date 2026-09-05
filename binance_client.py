"""
binance_client.py
Binance REST давхарга: гарын үсэг, хүсэлт, rate limit, серверийн цаг.
"""
from urllib.parse import urlencode
import hashlib
import hmac
import requests
import time
from settings import *
from state import state
import utils
from logging_setup import get_logger

log = get_logger(__name__)


def _rate_limit_wait(response, attempt):
    """Seconds to sleep after a 418/429/-1003. Honours Retry-After when present,
    otherwise exponential backoff capped at 60s."""
    try:
        retry_after = int(response.headers.get("Retry-After", "0") or 0)
    except Exception:
        retry_after = 0
    return max(retry_after, min(60, 2 ** (attempt + 1)))


def sync_server_time():
    try:
        local_before = int(time.time() * 1000)
        response = requests.get(f"{BASE_URL}/fapi/v1/time", timeout=REQUEST_TIMEOUT)
        local_after = int(time.time() * 1000)
        data = response.json()
        server_time = int(data.get("serverTime", local_after))
        local_mid = (local_before + local_after) // 2
        state.server_time_offset_ms = server_time - local_mid
        log.info(f"🕐 Server time offset: {state.server_time_offset_ms} ms")
        return True
    except Exception as e:
        log.warning(f"⚠️ Server time sync failed: {e}")
        return False


def current_timestamp_ms():
    return int(time.time() * 1000) + state.server_time_offset_ms


def get_signature(params_str, secret):
    return hmac.new(secret.encode("utf-8"), params_str.encode("utf-8"), hashlib.sha256).hexdigest()


def send_signed_request(method, endpoint, params=None, retry_on_time_error=True, _rl_attempt=0):
    if params is None:
        params = {}
    params = params.copy()
    params["timestamp"] = current_timestamp_ms()
    params["recvWindow"] = 5000
    params = {k: v for k, v in params.items() if v is not None}
    query_str = urlencode(sorted(params.items()), doseq=True)
    signature = get_signature(query_str, API_SECRET)
    url = f"{BASE_URL}{endpoint}?{query_str}&signature={signature}"
    headers = {"X-MBX-APIKEY": API_KEY}
    try:
        method = method.upper()
        if method == "GET":
            response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        elif method == "POST":
            response = requests.post(url, headers=headers, timeout=REQUEST_TIMEOUT)
        elif method == "DELETE":
            response = requests.delete(url, headers=headers, timeout=REQUEST_TIMEOUT)
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")

        # Back off on weight bans (429) / IP bans (418) before we dig the hole deeper.
        if response.status_code in (418, 429) and _rl_attempt < 4:
            wait = _rate_limit_wait(response, _rl_attempt)
            log.info(f"⏳ Rate limit {response.status_code} {endpoint} — sleeping {wait}s")
            time.sleep(wait)
            return send_signed_request(method, endpoint, params, retry_on_time_error, _rl_attempt + 1)

        try:
            data = response.json()
        except Exception:
            data = {"code": response.status_code, "msg": response.text}

        if retry_on_time_error and isinstance(data, dict) and utils.safe_float(data.get("code"), 0) == -1021:
            log.warning("⚠️ Timestamp error. Resyncing server time...")
            sync_server_time()
            return send_signed_request(method, endpoint, params, retry_on_time_error=False, _rl_attempt=_rl_attempt)

        if isinstance(data, dict) and utils.safe_float(data.get("code"), 0) == -1003 and _rl_attempt < 4:
            wait = _rate_limit_wait(response, _rl_attempt)
            log.info(f"⏳ Too many requests (-1003) {endpoint} — sleeping {wait}s")
            time.sleep(wait)
            return send_signed_request(method, endpoint, params, retry_on_time_error, _rl_attempt + 1)

        if response.status_code >= 400:
            log.error(f"❌ HTTP {response.status_code} {endpoint}: {data}")
        return data
    except Exception as e:
        log.error(f"❌ API error {endpoint}: {e}")
        return {"code": -9999, "msg": str(e)}


def send_public_request(endpoint, params=None, _rl_attempt=0):
    try:
        response = requests.get(f"{BASE_URL}{endpoint}", params=params, timeout=REQUEST_TIMEOUT)
        if response.status_code in (418, 429) and _rl_attempt < 4:
            wait = _rate_limit_wait(response, _rl_attempt)
            log.info(f"⏳ Rate limit {response.status_code} on {endpoint} — sleeping {wait}s")
            time.sleep(wait)
            return send_public_request(endpoint, params, _rl_attempt + 1)
        return response.json()
    except Exception as e:
        log.error(f"❌ Public API error {endpoint}: {e}")
        return {"code": -9999, "msg": str(e)}
