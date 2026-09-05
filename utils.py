"""
utils.py
Төрөл хөрвүүлэлт, тоймлолт, API алдаа таних жижиг туслахууд.
"""
import math


def safe_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def clamp(value, minimum, maximum):
    return max(minimum, min(maximum, value))


def round_down(value, decimals):
    factor = 10 ** decimals
    return math.floor(value * factor + 1e-12) / factor


def is_api_error(data):
    if not isinstance(data, dict):
        return False
    try:
        code = int(data.get("code", 0))
        return code < 0
    except Exception:
        return False


def api_error_text(data):
    if isinstance(data, dict):
        return str(data)
    return repr(data)
