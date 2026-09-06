"""
sports_journal.py
Илгээсэн value bet бүрийг CSV-д бүртгэнэ (цаасан дээрх бооцоо).

`result`, `pnl` баганыг гараар бооцоо тавьсны дараа нөхнө — эхний 200-300
бооцоог зөвхөн цаасан дээр бүртгэж, edge үнэхээр байгаа эсэхийг эндээс
шалгана.
"""
import csv
import logging
import os
import time
from sports_config import SPORTS_JOURNAL_FILE, SPORTS_JOURNAL_ENABLED

log = logging.getLogger(__name__)

FIELDNAMES = (
    "sent_at", "sport", "matchup", "commence_time", "market", "selection",
    "point", "book", "odds", "pinnacle_fair_odds", "ev_pct",
    "kelly_pct", "stake_amount", "result", "pnl",
)


def record_alert(bet, kelly_pct, stake):
    if not SPORTS_JOURNAL_ENABLED:
        return
    try:
        exists = os.path.exists(SPORTS_JOURNAL_FILE) and os.path.getsize(SPORTS_JOURNAL_FILE) > 0
        row = {
            "sent_at": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
            "sport": bet["sport"],
            "matchup": bet["matchup"],
            "commence_time": bet.get("commence_time", ""),
            "market": bet["market"],
            "selection": bet["selection"],
            "point": bet.get("point", ""),
            "book": bet["book"],
            "odds": bet["odds"],
            "pinnacle_fair_odds": bet.get("pinnacle_fair_odds", ""),
            "ev_pct": bet["ev_pct"],
            "kelly_pct": round(kelly_pct * 100, 2),
            "stake_amount": stake,
            "result": "",
            "pnl": "",
        }
        os.makedirs(os.path.dirname(SPORTS_JOURNAL_FILE) or ".", exist_ok=True)
        with open(SPORTS_JOURNAL_FILE, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
            if not exists:
                writer.writeheader()
            writer.writerow(row)
    except Exception as e:
        log.warning(f"⚠️ Sports journal бичигдсэнгүй: {e}")
