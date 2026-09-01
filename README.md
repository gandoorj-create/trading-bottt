# SMART BOT V2 — Fixed Pack

Энэ хувилбар нь эх кодын audit-аар илэрсэн execution/risk/backtest логикийн алдаануудыг зассан хувилбар.

## Гол засварууд

1. DCA хийхэд бүх protection order устгаад зөвхөн SL үлдээдэг асуудлыг зассан. DCA-ийн дараа TP + trailing + fallback SL-ийг дахин бүрдүүлнэ.
2. DCA дээр portfolio margin limit шалгалт нэмсэн.
3. DCA-г LONG болон SHORT хоёр чиглэлд symmetry-тэй болгосон.
4. Backtest-ийн BUY/SELL state-ийг LONG/SHORT гэж зөв тооцдог болсон.
5. Backtest signal-ийг одоогийн candle дээр шууд fill хийхгүй, дараагийн candle-ийн open дээр simulation хийдэг болсон.
6. Backtest-д fee болон slippage model нэмсэн.
7. Profit Factor, Expectancy, Max Drawdown зэрэг үзүүлэлт нэмсэн.
8. Strategy performance state-ийг strategy_state.json-д хадгалдаг болгосон тул restart хийхэд 0-оос эхлэхгүй.
9. telegram_format.py-г import-той таарах нэрээр багцалсан.
10. Binance-ийн одоогийн Algo Service схемд нийцсэн existing endpoint-үүдийг хэвээр ашигласан.

## Анхаарах зүйл

Backtest нь simulation хэвээр: funding fee, liquidation, exact fill microstructure зэрэг нь бүрэн загварчлагдаагүй.

Анх туршихдаа demo Futures endpoint ашигла. `.env.example` дээр demo endpoint байгаа.
