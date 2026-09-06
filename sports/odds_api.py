"""
odds_api.py
The Odds API-с спортын ханш татах client.
"""
import logging
import requests
from sports_config import ODDS_API_KEY, ODDS_API_BASE_URL, REQUEST_TIMEOUT

log = logging.getLogger(__name__)


class OddsAPIError(Exception):
    pass


def fetch_odds(sport_key, regions, markets, odds_format="decimal"):
    """Нэг спортын идэвхтэй event бүрийн ханшийг татна.

    regions, markets: жагсаалт (str list) — API руу таслалаар холбож явна.
    """
    if not ODDS_API_KEY:
        raise OddsAPIError("ODDS_API_KEY тохируулаагүй байна")

    url = f"{ODDS_API_BASE_URL}/v4/sports/{sport_key}/odds"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": ",".join(regions),
        "markets": ",".join(markets),
        "oddsFormat": odds_format,
        "dateFormat": "iso",
    }
    resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
    if resp.status_code != 200:
        raise OddsAPIError(f"{sport_key}: HTTP {resp.status_code} — {resp.text[:300]}")

    remaining = resp.headers.get("x-requests-remaining")
    if remaining is not None:
        log.debug(f"Odds API квот үлдсэн: {remaining}")

    data = resp.json()
    if not isinstance(data, list):
        raise OddsAPIError(f"{sport_key}: хүлээгдээгүй хариу формат")
    return data
