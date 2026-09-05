"""
order_api.py
Захиалга байрлуулах, цуцлах. Conditional (SL/TP/trailing) захиалга Algo service дээр байрладаг.
"""
import time
from telegram_format import format_block
from state import state
import account
import binance_client
import market_data
import notifications
import utils
from logging_setup import get_logger

log = get_logger(__name__)


def place_market_order(symbol, side, quantity, reduce_only=False, position_side=None, client_order_id=None):
    if client_order_id is None:
        client_order_id = f"bot_{int(time.time()*1000)}_{symbol[:4]}"
    params = {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "quantity": market_data.format_qty(symbol, quantity),
        "newOrderRespType": "RESULT",
        "newClientOrderId": client_order_id
    }
    hedge_mode = account.get_position_mode()
    if hedge_mode:
        if position_side:
            params["positionSide"] = position_side
    else:
        if reduce_only:
            params["reduceOnly"] = "true"
    return binance_client.send_signed_request("POST", "/fapi/v1/order", params)


def place_conditional_order(params):
    # Since 2025-12-09 Binance USD-M Futures requires conditional orders
    # (STOP_MARKET / TAKE_PROFIT_MARKET / STOP / TAKE_PROFIT / TRAILING_STOP_MARKET)
    # to go through the separate Algo Order service, NOT /fapi/v1/order
    # (that now returns -4120). Endpoint: POST /fapi/v1/algoOrder, algoType=CONDITIONAL.
    # Trigger price param is `triggerPrice`, trailing activation is `activatePrice`.
    params = params.copy()
    params["algoType"] = "CONDITIONAL"
    if account.get_position_mode():
        params.pop("reduceOnly", None)
    return binance_client.send_signed_request("POST", "/fapi/v1/algoOrder", params)


def place_trailing_stop_order(symbol, side, quantity, callback_rate, activation_price=None, position_side=None):
    params = {
        "symbol": symbol,
        "side": side,
        "type": "TRAILING_STOP_MARKET",
        "quantity": market_data.format_qty(symbol, quantity),
        "callbackRate": str(callback_rate),
        "workingType": "MARK_PRICE",
        "newOrderRespType": "RESULT"
    }
    hedge_mode = account.get_position_mode()
    if hedge_mode:
        if position_side:
            params["positionSide"] = position_side
    else:
        params["reduceOnly"] = "true"
    if activation_price is not None:
        params["activatePrice"] = market_data.format_price(symbol, activation_price)
    return place_conditional_order(params)


def place_stop_loss_order(symbol, side, quantity, stop_price, position_side=None):
    # Full-position hard stop. closePosition=true => no quantity / no reduceOnly,
    # and Binance auto-cancels it once the position is flat.
    params = {
        "symbol": symbol,
        "side": side,
        "type": "STOP_MARKET",
        "triggerPrice": market_data.format_price(symbol, stop_price),
        "closePosition": "true",
        "workingType": "MARK_PRICE",
        "newOrderRespType": "RESULT"
    }
    if account.get_position_mode() and position_side:
        params["positionSide"] = position_side
    return place_conditional_order(params)


def place_take_profit_order(symbol, side, quantity, tp_price, position_side=None):
    params = {
        "symbol": symbol,
        "side": side,
        "type": "TAKE_PROFIT_MARKET",
        "triggerPrice": market_data.format_price(symbol, tp_price),
        "closePosition": "true",
        "workingType": "MARK_PRICE",
        "newOrderRespType": "RESULT"
    }
    if account.get_position_mode() and position_side:
        params["positionSide"] = position_side
    return place_conditional_order(params)


def cancel_all_orders(symbol):
    return binance_client.send_signed_request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})


# Conditional (SL / TP / trailing) захиалгууд Algo service дээр байрладаг.
# Байрлуулах зам нь батлагдсан (POST /fapi/v1/algoOrder), харин жагсаах замын
# нэр нь тодорхойгүй: өмнө хэрэглэж байсан /fapi/v1/algoOpenOrders нь
# -5000 "Path is invalid" буцаадаг. Тиймээс ажиллах хувилбарыг эхний
# хэрэглээнд туршиж олоод кэшилнэ. Буруу зам зүгээр л алдаа буцаана — хор хөнөөлгүй.
ALGO_LIST_ENDPOINT_CANDIDATES = [
    "/fapi/v1/openAlgoOrders",
    "/fapi/v1/algoOrders",
    "/fapi/v1/algoOpenOrders",
    "/fapi/v1/openOrders",
]


CONDITIONAL_ORDER_TYPES = (
    "STOP_MARKET", "TAKE_PROFIT_MARKET", "STOP", "TAKE_PROFIT", "TRAILING_STOP_MARKET",
)


def _extract_order_list(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("orders", "data", "algoOrders"):
            if isinstance(data.get(key), list):
                return data[key]
    return None


def discover_algo_list_endpoint(symbol):
    """Algo захиалга жагсаах ажиллах endpoint-ыг нэг удаа олж кэшилнэ."""
    if state.algo_list_endpoint is not None:
        return state.algo_list_endpoint or None

    for endpoint in ALGO_LIST_ENDPOINT_CANDIDATES:
        orders = _extract_order_list(binance_client.send_signed_request("GET", endpoint, {"symbol": symbol}))
        if orders is None:
            continue
        state.algo_list_endpoint = endpoint
        log.info(f"✅ Algo order жагсаах endpoint олдлоо: {endpoint}")
        return endpoint

    # Үр дүнг кэшилсэн тул энэ хэсэг сесс тутамд нэг л удаа ажиллана
    state.algo_list_endpoint = ""
    log.error("🚨 Algo order жагсаах endpoint олдсонгүй — SL/TP цуцлалт ажиллахгүй")
    notifications.send_telegram(format_block("ALGO ORDER ENDPOINT ОЛДСОНГҮЙ", "🚨", [
        ("Туршсан", ", ".join(ALGO_LIST_ENDPOINT_CANDIDATES)),
        ("Үр дагавар", "Хуучин SL/TP цуцлагдахгүй — давхардаж хуримтлагдана"),
    ]))
    return None


def get_open_algo_orders(symbol):
    """Тухайн symbol дээрх нээлттэй conditional захиалгууд.

    None буцаах нь "мэдэхгүй" гэсэн утга — "байхгүй" гэсэн утгатай хольж
    болохгүй (тэгвэл хамгаалалтгүй позицыг хамгаалалттай гэж андуурна).
    """
    endpoint = discover_algo_list_endpoint(symbol)
    if not endpoint:
        return None
    orders = _extract_order_list(binance_client.send_signed_request("GET", endpoint, {"symbol": symbol}))
    if orders is None:
        return None
    return [
        o for o in orders
        if o.get("symbol") == symbol
        and (o.get("orderType") or o.get("type")) in CONDITIONAL_ORDER_TYPES
    ]


def cancel_all_algo_orders(symbol):
    """Conditional захиалгуудыг нэг бүрчлэн цуцална.

    Bulk cancel-ийн зам тодорхойгүй тул байрлуулахад ашигладаг ижил нөөц
    (/fapi/v1/algoOrder) дээр нэг бүрчлэн цуцална.
    """
    orders = get_open_algo_orders(symbol)
    if orders is None:
        return {"code": -9998, "msg": "algo order list endpoint unknown"}

    results = []
    for order in orders:
        params = {"symbol": symbol}
        if order.get("algoId") is not None:
            params["algoId"] = order["algoId"]
        elif order.get("orderId") is not None:
            params["orderId"] = order["orderId"]
        else:
            continue
        result = binance_client.send_signed_request("DELETE", "/fapi/v1/algoOrder", params)
        if utils.is_api_error(result):
            log.warning(f"⚠️ {symbol}: algo order цуцлагдсангүй {params}: {result}")
        results.append(result)
    return results


def cancel_all_symbol_orders(symbol):
    normal = cancel_all_orders(symbol)
    time.sleep(0.15)
    algo = cancel_all_algo_orders(symbol)
    return {"normal": normal, "algo": algo}
