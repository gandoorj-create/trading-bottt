"""
journal_report.py
trades.csv-д бүртгэгдсэн ЖИНХЭНЭ арилжааны үр дүнг задлан шинжилнэ.

Яагаад тусдаа скрипт вэ: backtest нь өнгөрснийг дуурайлгадаг бол энэ нь
бодитоор болсныг хардаг. Хоёулаа хэрэгтэй — ялгаа нь гарвал аль нэг нь
худлаа гэсэн үг.

Ажиллуулах:
    python journal_report.py              # STATE_DIR доторх trades.csv
    python journal_report.py --file x.csv
    python journal_report.py --telegram   # үр дүнг Telegram руу ч илгээнэ
"""
import argparse
import csv
import os
import sys
from collections import defaultdict

from settings import JOURNAL_FILE

# Score-ын босго сонгох нь эргэлт (шимтгэл) бууруулах хамгийн шууд хөшүүрэг
# тул тус бүрээр нь "хэрэв зөвхөн энэ дээш нь авсан бол" гэж тооцно.
SCORE_THRESHOLDS = (14, 16, 18, 20, 22, 24, 26, 28)
SCORE_BUCKETS = ((0, 18, "<18"), (18, 22, "18-22"), (22, 26, "22-26"), (26, 1e9, "26+"))
HOLD_BUCKETS = ((0, 2, "<2h"), (2, 6, "2-6h"), (6, 12, "6-12h"), (12, 24, "12-24h"), (24, 1e9, "24h+"))


def _f(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def load_rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if (r.get("pnl") or "").strip() != ""]
    if not rows:
        raise SystemExit(f"❌ {path} дотор арилжаа алга.")
    return rows


def _bucket(value, buckets):
    for low, high, label in buckets:
        if low <= value < high:
            return label
    return buckets[-1][2]


def _stats(pnls):
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    return {
        "n": len(pnls),
        "sum": sum(pnls),
        "avg": sum(pnls) / len(pnls),
        "win_pct": 100 * len(wins) / len(pnls),
        "avg_win": sum(wins) / len(wins) if wins else 0.0,
        "avg_loss": sum(losses) / len(losses) if losses else 0.0,
    }


def group_table(title, rows, key_fn, lines):
    groups = defaultdict(list)
    for row in rows:
        groups[key_fn(row) or "?"].append(_f(row.get("pnl")))
    lines.append(f"--- {title} ---")
    # Нийт ашгаар эрэмбэлнэ — хамгийн их нэмсэн/хассан нь дээрээ.
    for label, pnls in sorted(groups.items(), key=lambda kv: -sum(kv[1])):
        s = _stats(pnls)
        lines.append(
            f"{label:<20} n={s['n']:<4d} win={s['win_pct']:>3.0f}%  "
            f"sum={s['sum']:>+9.2f}  avg={s['avg']:>+7.2f}  "
            f"W{s['avg_win']:>+7.2f} / L{s['avg_loss']:>+7.2f}"
        )
    lines.append("")


def threshold_table(rows, lines):
    """"Зөвхөн score >= X-ийг авсан бол" гэсэн таамаглалт үр дүн.

    Анхаар: энэ бол ойролцоо тооцоо. Арилжаа цөөрвөл слот суларч, өөр
    арилжаа орох байсан тул бодит үр дүн үүнээс ялгаатай гарна. Гэхдээ
    босго өсгөх нь ашгийг эргэлтээс хурдан иддэг эсэхийг эндээс харна.
    """
    total = sum(_f(r.get("pnl")) for r in rows)
    lines.append("--- SCORE БОСГО (зөвхөн >= X авсан бол) ---")
    for threshold in SCORE_THRESHOLDS:
        kept = [_f(r.get("pnl")) for r in rows if _f(r.get("score")) >= threshold]
        if not kept:
            lines.append(f">= {threshold:<4} арилжаа үлдэхгүй")
            continue
        s = _stats(kept)
        share = 100 * s["n"] / len(rows)
        lines.append(
            f">= {threshold:<4} n={s['n']:<4d} ({share:>3.0f}%)  win={s['win_pct']:>3.0f}%  "
            f"sum={s['sum']:>+9.2f}  avg={s['avg']:>+7.2f}  "
            f"алдсан ашиг={s['sum'] - total:>+8.2f}"
        )
    lines.append("")


def build_report(rows):
    pnls = [_f(r.get("pnl")) for r in rows]
    s = _stats(pnls)
    stamps = sorted(r.get("closed_at", "") for r in rows if r.get("closed_at"))
    holds = [_f(r.get("hold_hours")) for r in rows]

    lines = [
        "📒 АРИЛЖААНЫ ЖУРНАЛ",
        f"Хугацаа: {stamps[0] if stamps else '?'} → {stamps[-1] if stamps else '?'} (UTC)",
        f"Арилжаа: {s['n']}   Ашиг: {s['sum']:+.2f}   Win: {s['win_pct']:.0f}%",
        f"Дундаж: {s['avg']:+.2f}   Ялалт {s['avg_win']:+.2f} / Ялагдал {s['avg_loss']:+.2f}",
        f"Дундаж барих хугацаа: {sum(holds) / len(holds):.1f} цаг",
        "",
    ]

    group_table("STRATEGY", rows, lambda r: r.get("strategy"), lines)
    group_table("REGIME", rows, lambda r: r.get("regime"), lines)
    group_table("SCORE BUCKET", rows, lambda r: _bucket(_f(r.get("score")), SCORE_BUCKETS), lines)
    threshold_table(rows, lines)
    group_table("BARIH HUGATSAA", rows, lambda r: _bucket(_f(r.get("hold_hours")), HOLD_BUCKETS), lines)
    group_table("EXIT", rows, lambda r: r.get("exit_reason"), lines)
    group_table("PARTIAL TP", rows, lambda r: f"partial={r.get('partial_tp_hit')}", lines)
    group_table("SIDE", rows, lambda r: r.get("side"), lines)
    group_table("SYMBOL", rows, lambda r: r.get("symbol"), lines)
    group_table("ӨДӨР", rows, lambda r: (r.get("closed_at") or "?")[:10], lines)

    return "\n".join(lines).rstrip()


def main(argv=None):
    parser = argparse.ArgumentParser(description="trades.csv журналын тайлан")
    parser.add_argument("--file", default=JOURNAL_FILE, help=f"CSV зам (анхдагч: {JOURNAL_FILE})")
    parser.add_argument("--telegram", action="store_true", help="Тайланг Telegram руу ч илгээнэ")
    args = parser.parse_args(argv)

    if not os.path.exists(args.file):
        raise SystemExit(
            f"❌ {args.file} олдсонгүй. STATE_DIR зөв эсэхийг шалгана уу "
            f"(одоо: {os.environ.get('STATE_DIR', 'тохируулаагүй')})."
        )

    report = build_report(load_rows(args.file))
    print(report)

    if args.telegram:
        # Импортыг энд хийнэ — тайлан хэвлэхэд Telegram тохиргоо шаардахгүй.
        import backtest
        backtest.send_report_to_telegram(report)
    return report


if __name__ == "__main__":
    main(sys.argv[1:])
