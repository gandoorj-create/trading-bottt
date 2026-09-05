"""
screening.py
Coin шинжлэх, корреляци, циклийн сонголт.
"""
from collections import defaultdict
import numpy as np
import time
from datetime import datetime
from settings import *
from state import state, STRATEGY_NAMES
import account
import indicators
import market_data
import reports
import strategies
from logging_setup import get_logger

log = get_logger(__name__)


def calculate_correlation_cached(symbol1, symbol2, lookback=50):
    key = "_".join(sorted((symbol1, symbol2)))
    now = time.time()
    if key in state.correlation_cache and (now - state.correlation_cache_time.get(key, 0)) < CORRELATION_CACHE_TTL:
        return state.correlation_cache[key]
    
    corr = calculate_correlation(symbol1, symbol2, lookback)
    state.correlation_cache[key] = corr
    state.correlation_cache_time[key] = now
    return corr


def calculate_correlation(symbol1, symbol2, lookback=50):
    try:
        df1 = market_data.get_klines(symbol1, interval="1h", limit=lookback + 10)
        df2 = market_data.get_klines(symbol2, interval="1h", limit=lookback + 10)
        if len(df1) < lookback or len(df2) < lookback:
            return 0.0
        close1 = df1["close"].iloc[-lookback:]
        close2 = df2["close"].iloc[-lookback:]
        returns1 = close1.pct_change().dropna()
        returns2 = close2.pct_change().dropna()
        if len(returns1) < 10 or len(returns2) < 10:
            return 0.0
        valid_idx = returns1.index.intersection(returns2.index)
        if len(valid_idx) < 10:
            return 0.0
        corr = returns1.loc[valid_idx].corr(returns2.loc[valid_idx])
        return corr if not np.isnan(corr) else 0.0
    except Exception as e:
        log.warning(f"⚠️ Correlation error {symbol1}-{symbol2}: {e}")
        return 0.0


def analyze_coin(symbol, check_correlation=True, active_symbols=None):
    try:
        # 260 closed bars so EMA-200 has enough history to be meaningful.
        df = market_data.get_klines(symbol, "1h", 260)
        if len(df) < 210:
            return None

        # MTF нь өмнө нь NEUTRAL coin-ыг бүрмөсөн хаядаг байсан. 4h ба 1h чиглэл
        # зөрөх нь trend эргэх үед байнга тохиолддог тул зах зээлийн ихэнх хэсэг
        # аль ч стратегид хүрэлгүй унадаг байв. Одоо хасахын оронд чиглэлийг нь
        # доор шалгаж, NEUTRAL үед calculate_strategy_score дахь -5 торгуулиар
        # барина (тэр торгууль өмнө нь хүрэшгүй dead code байсан).
        mtf_signal = strategies.get_mtf_signal(symbol)

        if CORRELATION_ENABLED and check_correlation and active_symbols:
            for sym in active_symbols:
                if sym == symbol:
                    continue
                corr = calculate_correlation_cached(symbol, sym, CORRELATION_LOOKBACK)
                if abs(corr) > CORRELATION_THRESHOLD:
                    log.info(f"🔴 SKIPPED {symbol}: Correlation with {sym} = {corr:.2f}")
                    return None

        close = df["close"].iloc[-1]
        if close <= 0:
            log.warning(f"⚠️ {symbol}: үнэ 0 ирлээ — алгаслаа")
            return None

        adx = indicators.calculate_adx(df).iloc[-1]
        rsi = indicators.calculate_rsi(df).iloc[-1]
        atr = indicators.calculate_atr(df).iloc[-1]
        atr_pct = atr / close * 100
        ema20 = indicators.calculate_ema(df, 20)
        ema50 = indicators.calculate_ema(df, 50)
        ema200 = indicators.calculate_ema(df, 200)
        ema_slope = (ema50.iloc[-1] - ema50.iloc[-5]) / ema50.iloc[-5] * 100
        volume_ratio = indicators.calculate_volume_ratio(df)
        
        chop = indicators.calculate_chop(df, CHOP_PERIOD).iloc[-1]
        vwap = indicators.calculate_vwap(df).iloc[-1]
        funding_rate = market_data.get_funding_rate(symbol)
        
        support, resistance = market_data.find_strong_levels(df)
        if support and resistance:
            log.info(f"🔹 {symbol} Support: {support:.6g} | Resistance: {resistance:.6g}")
        
        sentiment = 0.0
        if funding_rate > strategies.FUNDING_SENTIMENT_THRESHOLD:
            sentiment -= 0.5
        elif funding_rate < -strategies.FUNDING_SENTIMENT_THRESHOLD:
            sentiment += 0.5
        
        regime = strategies.determine_regime(chop, adx, ema_slope, atr_pct)

        strategy_results = {}
        for strategy in STRATEGY_NAMES:
            if not state.strategy_stats[strategy]["active"]:
                continue
            score = strategies.calculate_strategy_score(
                strategy, adx, rsi, atr_pct, volume_ratio, 
                ema_slope, sentiment, regime, chop, mtf_signal
            )
            signal = strategies.generate_strategy_signal(strategy, df, sentiment, regime, chop)
            
            if signal == "BUY" and strategy == "TREND_FOLLOWING" and ema20.iloc[-1] < ema50.iloc[-1]:
                signal = "HOLD"
            if signal == "SELL" and strategy == "TREND_FOLLOWING" and ema20.iloc[-1] > ema50.iloc[-1]:
                signal = "HOLD"

            # Өндөр давтамжийн trend-ийн эсрэг арилжаа хийхгүй. Өмнө нь MTF нь
            # тодорхойгүй coin-ыг хаядаг байсан ч эсрэг чиглэлийн арилжааг
            # саадгүй нэвтрүүлдэг байсан — санаанаасаа эсрэг ажиллаж байв.
            if MTF_ENABLED:
                if mtf_signal == "BULLISH" and signal == "SELL":
                    signal = "HOLD"
                elif mtf_signal == "BEARISH" and signal == "BUY":
                    signal = "HOLD"

            # Оноогоор таслахаас өмнөх чиглэлийг хадгална. Үүнгүйгээр
            # screen_coins дахь "оноо хэт бага" диагностик BUY/SELL хайдаг
            # мөртлөө HOLD-той тулгардаг тул хэзээ ч юу ч мэдээлдэггүй байв.
            raw_signal = signal
            if score < MIN_SIGNAL_SCORE:
                signal = "HOLD"

            strategy_results[strategy] = {
                "strategy": strategy,
                "symbol": symbol,
                "price": close,
                "score": score,
                "signal": signal,
                "raw_signal": raw_signal,
                "adx": adx,
                "rsi": rsi,
                "atr_pct": atr_pct,
                "volume_ratio": volume_ratio,
                "ema_slope": ema_slope,
                "regime": regime,
                "sentiment": sentiment,
                "chop": chop,
                "vwap": vwap,
                "funding": funding_rate,
                "mtf": mtf_signal
            }
        return {
            "symbol": symbol,
            "price": close,
            "adx": adx,
            "rsi": rsi,
            "atr_pct": atr_pct,
            "volume_ratio": volume_ratio,
            "ema_slope": ema_slope,
            "regime": regime,
            "chop": chop,
            "vwap": vwap,
            "funding": funding_rate,
            "mtf": mtf_signal,
            "strategies": strategy_results
        }
    except Exception as e:
        log.error(f"❌ analyze_coin {symbol}: {e}")
        return None


def screen_coins():
    log.info("\n" + "=" * 70)
    log.info(f"🔍 MARKET SCREENING {datetime.now().strftime('%H:%M:%S')}")
    log.info("=" * 70)

    skipped_reasons = []

    current_positions = account.get_positions()
    active_symbols = {p["symbol"] for p in current_positions}
    analyses = []
    for symbol in SYMBOLS_POOL:
        result = analyze_coin(symbol, check_correlation=True, active_symbols=active_symbols)
        if result:
            analyses.append(result)

    strategy_candidates = []
    for strategy in STRATEGY_NAMES:
        if not state.strategy_stats[strategy]["active"]:
            continue
        candidates = []
        for coin in analyses:
            result = coin["strategies"].get(strategy)
            if not result:
                continue
            if result["signal"] not in ["BUY", "SELL"]:
                continue
            if result["score"] < MIN_SIGNAL_SCORE:
                continue
            candidates.append(result)
        if not candidates:
            continue
        candidates.sort(key=lambda x: x["score"], reverse=True)
        # Стратеги бүрээс зөвхөн 1 coin авдаг байсан тул нэр дэвшигчийн тоо
        # стратегийн тоогоор (6) таглагдаж, давхардал болон корреляци хассаны
        # дараа ихэвчлэн 2 л үлддэг байв.
        for best in candidates[:MAX_CANDIDATES_PER_STRATEGY]:
            strategy_candidates.append(best)
            log.info(f"🎯 {strategy:<30} → {best['symbol']:<10} {best['signal']:<4} Score={best['score']:.2f}")

    by_symbol = defaultdict(list)
    for candidate in strategy_candidates:
        by_symbol[candidate["symbol"]].append(candidate)
    unique_candidates = []
    for symbol, candidates in by_symbol.items():
        winner = max(candidates, key=lambda x: x["score"])
        unique_candidates.append(winner)
        if len(candidates) > 1:
            log.info(f"🔄 DUPLICATE {symbol}: WINNER {winner['strategy']}")

    # Оноогоор эрэмбэлнэ. Өмнө нь нэр дэвшигчид стратегийн дарааллаар байсан тул
    # эхний стратегийн сул signal (оноо 14) сүүлийн стратегийн хүчтэйг (оноо 27)
    # байрнаас нь шахаж гаргадаг байв.
    unique_candidates.sort(key=lambda x: x["score"], reverse=True)

    if CORRELATION_ENABLED:
        final_selected = []
        removed_by_correlation = []
        for coin in unique_candidates:
            # ЗӨВХӨН сонгогдсонтой харьцуулна. Өмнө нь бүх өмнөх нэр дэвшигчтэй
            # харьцуулдаг байсан тул хасагдсан coin өөрөө бусдыг хасах чадвартай
            # хэвээр үлдэж, гинжин урвал үүсгэдэг байв: A-B хамааралтай, B-C
            # хамааралтай атлаа A-C хамааралгүй байхад C ч хасагддаг.
            clash = None
            for kept in final_selected:
                corr = calculate_correlation_cached(coin["symbol"], kept["symbol"], CORRELATION_LOOKBACK)
                if abs(corr) > CORRELATION_THRESHOLD:
                    clash = kept["symbol"]
                    break
            if clash:
                removed_by_correlation.append(coin["symbol"])
                log.info(f"🔴 REMOVED {coin['symbol']}: high correlation with {clash}")
                continue
            final_selected.append(coin)
            if len(final_selected) >= MAX_SELECTIONS:
                break
        selected = final_selected
        if removed_by_correlation:
            skipped_reasons.append(f"🔗 Корреляциас хасагдсан: {', '.join(removed_by_correlation)}")
    else:
        selected = unique_candidates[:MAX_SELECTIONS]

    total_balance = account.get_usdt_balance()
    positions = account.get_positions()
    current_margin_used = 0.0
    for pos in positions:
        actual_lev = account.get_actual_leverage(pos["symbol"])
        current_margin_used += abs(pos["positionAmt"]) * pos["entryPrice"] / actual_lev
    max_margin = total_balance * MAX_TOTAL_MARGIN_USAGE
    if current_margin_used >= max_margin * 0.95:
        skipped_reasons.append(f"💳 Маржин хязгаарт хүрсэн (ашигласан: {current_margin_used:.2f} / хязгаар: {max_margin:.2f} USDT)")

    inactive_strategies = [s for s, stats in state.strategy_stats.items() if not stats["active"]]
    if inactive_strategies:
        skipped_reasons.append(f"⏸️ Идэвхгүй стратеги: {', '.join(inactive_strategies)}")

    low_score_signals = []
    for coin in analyses:
        for strategy, result in coin["strategies"].items():
            if result.get("raw_signal") in ["BUY", "SELL"] and result["score"] < MIN_SIGNAL_SCORE:
                low_score_signals.append(f"{result['symbol']} ({strategy}): {result['score']:.1f}")
    if low_score_signals:
        skipped_reasons.append(f"📉 Оноо хэт бага (MIN_SIGNAL_SCORE={MIN_SIGNAL_SCORE}): {', '.join(low_score_signals[:5])}")

    log.info("\n🏆 FINAL SELECTION:")
    for i, coin in enumerate(selected, 1):
        log.info(f"{i}. {coin['symbol']} | {coin['strategy']} | {coin['signal']} | Score={coin['score']:.2f}")

    if CHART_SEND_ON_SIGNAL:
        for coin in selected:
            symbol = coin['symbol']
            df = market_data.get_klines(symbol, "1h", 200)
            reports.send_chart(symbol, df, coin['signal'], coin['score'])

    reports.send_selection_report(selected, strategy_candidates, skipped_reasons)
    return selected
