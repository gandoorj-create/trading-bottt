"""
account.py
Данс, позиц, leverage болон realized PnL.
"""
from settings import *
from state import state
import binance_client
import utils
from logging_setup import get_logger

log = get_logger(__name__)


def get_usdt_balance():
    data = binance_client.send_signed_request("GET", "/fapi/v3/balance")
    if not isinstance(data, list):
        return 0.0
    for item in data:
        if item.get("asset") == "USDT":
            return utils.safe_float(item.get("balance"))
    return 0.0


def get_position_mode():
    if state.position_mode_cache is not None:
        return state.position_mode_cache
    data = binance_client.send_signed_request("GET", "/fapi/v1/positionSide/dual")
    if utils.is_api_error(data):
        raise RuntimeError(f"Cannot get position mode: {data}")
    state.position_mode_cache = bool(data.get("dualSidePosition", False))
    log.info(f"📌 Position mode: {'HEDGE' if state.position_mode_cache else 'ONE-WAY'}")
    return state.position_mode_cache


class PositionFetchError(RuntimeError):
    """positionRisk API-аас позицын жагсаалтыг уншиж чадсангүй.

    Сүлжээний алдаа гарахад хоосон жагсаалт буцаах нь "позиц байхгүй" гэсэн
    утгатай ижил болж, бот нээлттэй позицуудыг хаагдсан гэж андуурч SL/TP-г
    цуцалдаг байсан. Тиймээс тодорхойгүй байдлыг заавал алдаагаар илэрхийлнэ.
    """


def get_positions():
    data = binance_client.send_signed_request("GET", "/fapi/v2/positionRisk")
    positions = []
    if not isinstance(data, list):
        raise PositionFetchError(utils.api_error_text(data))
    for pos in data:
        amount = utils.safe_float(pos.get("positionAmt"))
        if abs(amount) <= 0:
            continue
        positions.append({
            "symbol": pos.get("symbol"),
            "positionAmt": amount,
            "entryPrice": utils.safe_float(pos.get("entryPrice")),
            "markPrice": utils.safe_float(pos.get("markPrice")),
            "unRealizedProfit": utils.safe_float(pos.get("unRealizedProfit")),
            "positionSide": pos.get("positionSide", "BOTH")
        })
    return positions


def get_total_unrealized():
    positions = get_positions()
    return sum(p["unRealizedProfit"] for p in positions)


def get_trade_realized_pnl(symbol, opened_at_ms):
    try:
        start_time = max(0, int(opened_at_ms) - 5000)
        trades = binance_client.send_signed_request("GET", "/fapi/v1/userTrades", {
            "symbol": symbol,
            "startTime": start_time,
            "limit": PNL_LOOKBACK_LIMIT
        })
        if not isinstance(trades, list):
            return 0.0
        # realizedPnl нь ШИМТГЭЛГҮЙ дүн — Binance шимтгэлийг тусад нь
        # `commission` талбараар өгдөг. Өмнө нь үүнийг уншдаггүй байсан тул
        # бүртгэсэн ашиг бодитоос үргэлж өндөр гарч, +$1 gross атлаа сөрөг net
        # арилжаа ч "хожил" гэж тоологдож win rate хиймлээр өсдөг байв.
        # Backtest нь харин шимтгэл тооцдог тул хоёр тоо харьцуулагдахгүй байсан.
        pnl = 0.0
        fees = 0.0
        foreign_fee_assets = set()
        for trade in trades:
            trade_time = utils.safe_float(trade.get("time"), 0)
            if trade_time < start_time:
                continue
            pnl += utils.safe_float(trade.get("realizedPnl", 0))

            commission = utils.safe_float(trade.get("commission", 0))
            if not commission:
                continue
            fee_asset = str(trade.get("commissionAsset") or "").upper()
            margin_asset = str(trade.get("marginAsset") or "USDT").upper()
            if fee_asset in ("", margin_asset):
                fees += commission
            else:
                # Жишээ нь BNB-ээр шимтгэл төлсөн бол өөр нэгжтэй тул шууд
                # хасаж болохгүй — ханшгүйгээр буруу тоо гаргахаас татгалзана.
                foreign_fee_assets.add(fee_asset)

        if foreign_fee_assets:
            log.warning(
                f"⚠️ {symbol}: {', '.join(sorted(foreign_fee_assets))}-ээр төлсөн шимтгэл "
                f"тооцоонд ороогүй — бүртгэсэн ашиг бага зэрэг өндөр байж болно"
            )
        return pnl - fees
    except Exception as e:
        log.error(f"❌ PnL error {symbol}: {e}")
        return 0.0


def get_actual_leverage(symbol):
    if symbol in state.leverage_cache:
        return state.leverage_cache[symbol]
    result = binance_client.send_signed_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
    if utils.is_api_error(result) or not isinstance(result, list) or not result:
        return LEVERAGE
    lev = int(utils.safe_float(result[0].get("leverage", LEVERAGE), LEVERAGE))
    # safe_float нь "0"-г хүчинтэй тоо гэж үзэх тул default руу шилждэггүй.
    # Тэглэсэн leverage нь margin тооцоололд 0-д хуваах алдаа өгч бүтэн циклийг унагаана.
    if lev <= 0:
        log.warning(f"⚠️ {symbol}: leverage={lev} ирлээ — {LEVERAGE}x гэж үзэв")
        return LEVERAGE
    state.leverage_cache[symbol] = lev
    return lev


def ensure_leverage(symbol, leverage=LEVERAGE):
    if state.leverage_cache.get(symbol) == leverage:
        return True
    result = binance_client.send_signed_request("POST", "/fapi/v1/leverage", {
        "symbol": symbol,
        "leverage": leverage
    })
    if utils.is_api_error(result):
        if utils.safe_float(result.get("code"), 0) == -4141:
            state.leverage_cache[symbol] = leverage
            return True
        log.error(f"❌ {symbol}: leverage error {result}")
        return False
    state.leverage_cache[symbol] = leverage
    return True
