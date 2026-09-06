"""
sports_state.py
Илгээсэн value bet-үүдийн key-г хадгалж давхардлыг таслана
(event+market+line+book+selection).
"""
import json
import time
from pathlib import Path
from settings import SPORTS_SEEN_FILE, SPORTS_SEEN_TTL_HOURS
from logging_setup import get_logger

log = get_logger(__name__)


def load_seen():
    try:
        path = Path(SPORTS_SEEN_FILE)
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as e:
        log.warning(f"⚠️ Seen cache уншихад алдаа: {e}")
        return {}


def save_seen(seen):
    try:
        cutoff = time.time() - SPORTS_SEEN_TTL_HOURS * 3600
        pruned = {k: v for k, v in seen.items() if v >= cutoff}
        path = Path(SPORTS_SEEN_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(pruned), encoding="utf-8")
        tmp.replace(path)
    except Exception as e:
        log.warning(f"⚠️ Seen cache бичихэд алдаа: {e}")
