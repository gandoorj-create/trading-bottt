"""
signal_edge.py
Дохионуудад таамаглах чадвар БАЙГАА ЭСЭХИЙГ шалгана.

Backtest-ээс юугаараа ялгаатай вэ: backtest нь дохио, гарцын бүтэц, байрлалын
хэмжээ, слотын өрсөлдөөн, шимтгэл — бүгдийг нэг тоонд хольдог. Тэр тоо сөрөг
гарахад аль хэсэг нь эвдэрсэнийг ялгах арга байхгүй. Энэ скрипт бусдыг нь
бүгдийг хаяад ганц асуулт тавина:

    Дохио дуугармагц үнэ тэр чиглэлд явдаг уу? Хэр их вэ?

Хоёр хэмжигдэхүүнийг зэрэг гаргана:
  • ЧИГЛЭЛИЙН өгөөж  — dir * (P[t+h]/P[t] - 1). Бот бодитоор юу олохыг харуулна.
  • ИЛҮҮДЭЛ өгөөж    — dir * (symbol-ийн өгөөж - тэр үеийн зах зээлийн дундаж).
                       Beta-г хасна. "Bull зах зээлд long байх" нь давуу тал
                       биш — илүүдэл нь түүнийг тэг болгож харуулна.

Ажиллуулах:
    python signal_edge.py --days 180
    python signal_edge.py --days 90 --offset-days 90 --telegram
"""
import argparse
import sys

import numpy as np
import pandas as pd

import backtest
from settings import *
from state import STRATEGY_NAMES
from logging_setup import get_logger, setup_logging
import binance_client

log = get_logger(__name__)

# Цаг (1h лаа) тутмын урагшлах горизонтууд.
HORIZONS = (1, 4, 12, 24)
# Нэг эргэлтийн зардал: орох+гарах шимтгэл + slippage, үнийн хувиар.
COST_BPS = (BACKTEST_FEE_RATE * 2 + BACKTEST_SLIPPAGE_RATE * 2) * 10_000
SCORE_BUCKETS = ((0, 14, "<14"), (14, 18, "14-18"), (18, 22, "18-22"),
                 (22, 26, "22-26"), (26, 1e9, "26+"))


def _bucket(value):
    for low, high, label in SCORE_BUCKETS:
        if low <= value < high:
            return label
    return SCORE_BUCKETS[-1][2]


def build_observations(data, step=1, progress=True):
    """Дохио бүрийг урагшлах өгөөжтэй нь хослуулсан хүснэгт.

    Мөр бүр = (цаг, symbol, стратеги) нэг дохио. Оноо нь MIN_SIGNAL_SCORE-оос
    хамаарахгүй байхын тулд `neutral_screening` дотор ажиллаж, таслагдаагүй
    `raw_signal`-ыг ашиглана — эс тэгвээс зөвхөн одоогийн босгоос дээш
    дохиог хэмжиж, босго өөрчлөх утгагүй болно.
    """
    symbols = list(data)
    master = sorted(set().union(*[set(d["signal"]["time"]) for d in data.values()]))
    index_by_time = {sym: dict(zip(d["signal"]["time"], range(len(d["signal"]))))
                     for sym, d in data.items()}
    closes = {sym: d["signal"]["close"].to_numpy(dtype=float) for sym, d in data.items()}

    max_h = max(HORIZONS)
    first, last = backtest.SIGNAL_WINDOW, len(master) - 1 - max_h
    if last < first:
        return pd.DataFrame()

    rows = []
    total = max(1, last - first)
    with backtest.neutral_screening():
        for n, mi in enumerate(range(first, last + 1, step)):
            t = master[mi]
            analyses = backtest._analyses_at(data, symbols, index_by_time, t)
            if not analyses:
                continue

            # Тухайн мөчид бүх symbol-ийн урагшлах өгөөж — зах зээлийн дундажийг
            # эндээс гаргана. Дундажийг ЗӨВХӨН энэ мөчид шинжлэгдсэн symbol-оос
            # авна, эс тэгвээс жагсаалтад байхгүй coin-оор beta-г гажуудуулна.
            forward = {}
            for a in analyses:
                sym = a["symbol"]
                i = index_by_time[sym].get(t)
                c = closes[sym]
                if i is None or i + max_h >= len(c) or c[i] <= 0:
                    continue
                forward[sym] = {h: c[i + h] / c[i] - 1.0 for h in HORIZONS}
            if len(forward) < 2:
                continue
            market = {h: float(np.mean([f[h] for f in forward.values()])) for h in HORIZONS}

            for a in analyses:
                sym = a["symbol"]
                if sym not in forward:
                    continue
                for result in a["strategies"].values():
                    signal = result.get("raw_signal")
                    if signal not in ("BUY", "SELL"):
                        continue
                    direction = 1.0 if signal == "BUY" else -1.0
                    row = {
                        "t": t,
                        "symbol": sym,
                        "strategy": result["strategy"],
                        "regime": result.get("regime") or "?",
                        "score": float(result.get("score") or 0.0),
                        "side": signal,
                    }
                    for h in HORIZONS:
                        row[f"dir_{h}"] = direction * forward[sym][h]
                        row[f"exc_{h}"] = direction * (forward[sym][h] - market[h])
                    rows.append(row)

            if progress and n and n % 200 == 0:
                log.info(f"   дохио цуглуулж байна {100 * (mi - first) / total:5.1f}%")

    return pd.DataFrame(rows)


def _matrix(df, prefix, key, lines, note="", step=1):
    """Бүлэг × горизонтын дундаж өгөөж (bps) ба хамгийн урт горизонтын t."""
    long_h = max(HORIZONS)
    header = (f"  {'':<22}" + "".join(f"{f'{h}ц':>9}" for h in HORIZONS)
              + f"{'t(' + str(long_h) + 'ц)':>9}{'n':>8}")
    lines.append(header)
    order = df.groupby(key)[f"{prefix}_{long_h}"].mean().sort_values(ascending=False)
    for label in order.index:
        part = df[df[key] == label]
        cells = "".join(f"{part[f'{prefix}_{h}'].mean() * 10_000:>9.1f}" for h in HORIZONS)
        _, t_stat, periods = _honest_tstat(part, f"{prefix}_{long_h}", long_h, step)
        # 3-аас цөөн давхцаагүй мөчид t утгагүй — 0.00 гэж хэвлэвэл
        # "тооцоод тэг гарлаа" мэт уншигдана.
        shown = f"{t_stat:>9.2f}" if periods >= 3 else f"{'—':>9}"
        lines.append(f"  {str(label):<22}{cells}{shown}{len(part):>8}")
    if note:
        lines.append(f"  {note}")
    lines.append("")


def _honest_tstat(df, column, horizon, step):
    """Давхцал ба symbol хоорондын хамаарлыг тооцсон t-статистик.

    Гэнэн t (мөр бүрийг бие даасан гэж үзэх) нь хоёр талаар хуурдаг: нэг
    мөчид 15 coin бараг ижил хөдөлдөг, мөн h цагийн горизонтууд цаг тутам
    давхцдаг. Тиймээс эхлээд мөч бүрийн дундажийг авч (coin хоорондын
    хамаарал арилна), дараа нь h цаг тутмын мөчийг л авна (давхцал арилна).
    """
    per_time = df.groupby("t")[column].mean().sort_index()
    stride = max(1, int(round(horizon / max(1, step))))
    sample = per_time.iloc[::stride]
    n = len(sample)
    if n < 3:
        return 0.0, 0.0, n
    mean = float(sample.mean())
    sd = float(sample.std(ddof=1))
    if sd <= 0:
        return mean * 10_000, 0.0, n
    return mean * 10_000, mean / (sd / np.sqrt(n)), n


def build_report(df, data, step):
    symbols = sorted(df["symbol"].unique())
    times = df["t"]
    span_h = (times.max() - times.min()) / backtest.HOUR_MS

    lines = [
        "🔬 ДОХИОНЫ ДАВУУ ТАЛ",
        f"{len(symbols)} coin | {span_h / 24:.0f} хоног | {len(df):,} дохио | алхам {step}ц",
        f"Зардлын босго: {COST_BPS:.1f} bps (нэг эргэлт). Дохио үүнээс дээш "
        f"таамаглаж чадахгүй бол ямар ч тохиргоо аварахгүй.",
        "",
        "─ ЧИГЛЭЛИЙН ӨГӨӨЖ (bps) — бот бодитоор юу олохыг харуулна ──────────",
    ]
    _matrix(df, "dir", "strategy", lines, step=step)

    lines.append("─ ЗАХ ЗЭЭЛИЙН ИЛҮҮДЭЛ (bps) — beta хассан, жинхэнэ ур чадвар ──────")
    _matrix(df, "exc", "strategy", lines, step=step,
            note="дээрхээс их зөрвөл ашиг нь зах зээлийн хөдөлгөөнөөс ирж байна")

    lines.append("─ ИЛҮҮДЭЛ × ГОРИМ (bps) ───────────────────────────────────────────")
    _matrix(df, "exc", "regime", lines, step=step)

    df = df.copy()
    df["bucket"] = df["score"].map(_bucket)
    lines.append("─ ИЛҮҮДЭЛ × ОНОО (bps) — өндөр оноо үнэхээр дээр үү? ──────────────")
    _matrix(df, "exc", "bucket", lines, step=step)

    lines.append("─ НИЙТ ДҮН ба ЗӨВ t-СТАТИСТИК ─────────────────────────────────────")
    lines.append(f"  {'горизонт':<12}{'чиглэл':>10}{'илүүдэл':>10}{'t':>8}{'мөч':>8}")
    for h in HORIZONS:
        dir_bps, dir_t, _ = _honest_tstat(df, f"dir_{h}", h, step)
        exc_bps, exc_t, n = _honest_tstat(df, f"exc_{h}", h, step)
        lines.append(f"  {f'{h} цаг':<12}{dir_bps:>10.1f}{exc_bps:>10.1f}{exc_t:>8.2f}{n:>8}")
    lines.append("  |t| > 2 бол статистикийн хувьд утга учиртай. Түүнээс доош бол")
    lines.append("  дундаж эерэг байсан ч шуугианаас ялгах боломжгүй.")
    lines.append("  t нь ДАВХЦААГҮЙ мөчүүд дээр тооцогддог тул дээрх хүснэгтийн")
    lines.append("  энгийн дунджаас бага зэрэг зөрж болно — дээж нь өөр.")
    lines.append("")

    # Хамгийн сайн стратегийг СОНГОСОН тул t-г нь мөн түүн дээр нь тооцно —
    # нийт дүнгийн t-г зээлж болохгүй. Мөн "олонхоос хамгийн сайн" нь дээшээ
    # гажуудсан сонголт тул босгыг 2 биш 3 тавина.
    means = df.groupby("strategy")["exc_24"].mean()
    n_strategies = len(means)
    best_name = means.idxmax()
    best_bps = float(means.max()) * 10_000
    _, best_t, best_periods = _honest_tstat(df[df["strategy"] == best_name], "exc_24", 24, step)
    _, overall_t, _ = _honest_tstat(df, "exc_24", 24, step)
    threshold = 3.0 if n_strategies > 1 else 2.0

    lines.append("─ ДҮГНЭЛТ ─────────────────────────────────────────────────────────")
    lines.append(f"  Хамгийн сайн: {best_name} — 24ц илүүдэл {best_bps:.1f} bps, "
                 f"t={best_t:.2f} ({best_periods} мөч)")
    lines.append(f"  Зардлын босго {COST_BPS:.1f} bps | нийт илүүдлийн t = {overall_t:.2f}")
    if n_strategies > 1:
        lines.append(f"  ({n_strategies} стратегийн ХАМГИЙН САЙНЫГ сонгосон тул t босго = {threshold:.0f})")
    if best_bps > COST_BPS and best_t > threshold:
        lines.append("  ✅ Зардлаас дээш, статистикт утгатай давуу тал БАЙНА —")
        lines.append("     асуудал дохионд биш, гарц/зардалд байна.")
    elif best_bps > COST_BPS:
        lines.append("  ⚠️ Дундаж нь зардлаас дээш ч t хангалтгүй — шуугианаас")
        lines.append("     ялгагдахгүй. Илүү урт хугацаа шаардлагатай.")
    else:
        lines.append("  ❌ Дохионууд зардлын босгыг давахгүй байна. Тохиргоо")
        lines.append("     тааруулах нь утгагүй — дохио өөрөө шуугиан.")
    if overall_t < -threshold:
        lines.append("  ⚠️ Нийт илүүдэл СӨРӨГ талдаа утга учиртай — дохионууд")
        lines.append("     системтэйгээр БУРУУ чиглэл заасан байж болзошгүй.")
    return "\n".join(lines)


def run(days=180, symbols=None, offset_days=0, step=1, data_url=None, progress=True):
    symbols = symbols or SYMBOLS_POOL
    # exec лаа энд хэрэггүй (гарц симуляц хийхгүй) тул 1h-ээр ачаалж,
    # 15m татах илүүдэл хугацаа/санах ойг хэмнэнэ.
    data = backtest.load_history(symbols, days, exec_interval="1h", progress=progress,
                                 data_url=data_url, offset_days=offset_days)
    if not data:
        return None, "❌ Өгөгдөл татагдсангүй."
    df = build_observations(data, step=step, progress=progress)
    if df.empty:
        return None, "❌ Дохио олдсонгүй."
    return df, build_report(df, data, step)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Дохионы таамаглах чадварын шинжилгээ")
    parser.add_argument("--days", type=int, default=180, help="хэмжих хугацаа (өдөр)")
    parser.add_argument("--offset-days", type=int, default=0, help="цонхны төгсгөлийг ухраах")
    parser.add_argument("--step", type=int, default=1, help="хэдэн цаг тутамд дээж авах")
    parser.add_argument("--symbols", type=str, default=None, help="таслалаар тусгаарласан")
    parser.add_argument("--data-url", type=str, default=None)
    parser.add_argument("--csv", type=str, default=None, help="дохио бүрийг CSV-д бичих")
    parser.add_argument("--telegram", action="store_true")
    args = parser.parse_args(argv)
    if args.step < 1:
        parser.error("--step дор хаяж 1 байх ёстой")
    if args.offset_days < 0:
        parser.error("--offset-days сөрөг байж болохгүй")

    setup_logging(STATE_DIR, STATE_DIR_IS_PERSISTENT)
    binance_client.sync_server_time()

    symbols = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else None
    df, report = run(days=args.days, symbols=symbols, offset_days=args.offset_days,
                     step=args.step, data_url=args.data_url)
    print(report)
    if df is not None and args.csv:
        df.to_csv(args.csv, index=False)
    if df is not None and args.telegram:
        backtest.send_report_to_telegram(report)
    return 0 if df is not None else 1


if __name__ == "__main__":
    sys.exit(main())
