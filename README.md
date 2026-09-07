Trading Bot {#trading-bot}

An automated trading bot for Binance USDⓈ\-M Futures. It analyzes coins across 6 strategies simultaneously, ranks them by score, and opens positions on the coins that pass the risk filters. All notifications are sent to Telegram.

## How It Works {#how-it-works}

The bot runs in a continuous loop inside `main()`, on two different cadences:

| What | How Often | Setting |
| --- | --- | --- |
| Monitor positions, check targets | Every 30 seconds | `monitor_interval_sec` |
| Search for new coins (screening) | Every 2 hours | `selection_interval_minutes` |

When a valid signal appears, no restart is needed — the screening cycle triggers itself and trading begins. A manual restart is only required after the drawdown circuit breaker has tripped (see below).

**Selection filter**

```
15 coins (symbols_pool)
   ↓  analyze_coin — computes indicators and produces a signal + score for each strategy
   ↓  MTF filter — no trades against the 4h/1h trend
   ↓  min_signal_score — a signal below the threshold becomes HOLD
   ↓  up to max_candidates_per_strategy coins from each strategy
   ↓  if the same symbol appears twice, the higher-scoring one is kept
   ↓  ranked by score
   ↓  correlation filter — drops anything above correlation_threshold relative to what's already selected
   ↓  capped at max_selections
execute_trades — checks margin, minQty, minNotional, then places the order
```

## Strategies {#strategies}

`SUPERTREND`, `MACD_MOMENTUM`, `BREAKOUT`, `BOLLINGER_MEAN_REVERSION`, `RSI_STRATEGY`, `TREND_FOLLOWING` — each has its own signal conditions and scoring formula. Scores aren't on the same scale across strategies, so change `min_signal_score` carefully (see below).

`BOLLINGER_MEAN_REVERSION` and `RSI_STRATEGY` bet that price reverts after hitting a band/RSI extreme (fade), while `BREAKOUT` bets the opposite — that a band break with a volume spike continues (it requires volume above `breakout_volume_ratio`). The old `GRID_TRADING` strategy had no real grid logic and was effectively a weaker version of `BOLLINGER_MEAN_REVERSION` without the VWAP check, so it was removed and replaced with this new, directionally\-opposite strategy.

`MACD_MOMENTUM`'s scoring formula included `volume_ratio`, but it wasn't actually checked in the signal condition — so low\-volume (noise) MACD moves still became BUY/SELL, just ranked lower on score. It now returns HOLD whenever volume is below `macd_min_volume_ratio`.

## Timeframe Layering: SUPERTREND (1h) vs TREND\_FOLLOWING (4h) {#timeframe-layering-supertrend-1h-vs-trend_following-4h}

Both used to run on 1h, so they'd score highly together in the same regime, risking a portfolio that's purely trend\-biased. They're now separated:

| Strategy | Timeframe | What it captures |
| --- | --- | --- |
| `SUPERTREND` | 1h | Trend\-reversal moments (supertrend flip) — tactical entry |
| `TREND_FOLLOWING` | 4h | Macro trend — EMA20/50/100 spans 3–17 days |

4h data isn't fetched separately from the exchange — it's resampled from the same 1h dataframe via `resample_ohlcv`. This matters because backtesting steps through historical windows, and fetching live 4h data there would cause lookahead bias — live trading and backtesting would run on different logic. It also means no extra API calls.

The thresholds (`trend_htf_min_adx: 30`, `trend_htf_min_slope: 1.0`) were chosen by measuring on synthetic data: they catch 40/40 trending samples and give only 1 false signal out of 40 in choppy conditions. On 1h, chop's ADX sits around 25 — dangerously close to the threshold — but it drops as low as 17 on 4h. The slope threshold is higher than 1h's 0.5% because a 5\-bar slope on 4h spans 20 hours, so it's naturally about 3x larger.

`analyze_coin` now fetches 600 1h candles instead of 260 (600 / 4 \= 150 4h candles — enough for a 4h EMA\-100). Still a single request, just a larger payload.

## Risk Protections {#risk-protections}

| Protection | What it does | Setting |
| --- | --- | --- |
| Emergency stop\-loss | Always placed on every position | `emergency_sl_pct` |
| Take profit | Always placed on every position | `take_profit_pct` |
| Partial TP → breakeven | Closes half the position early, then moves the remaining stop to breakeven | `partial_tp_*`, `breakeven_offset_pct` |
| Trailing stop | Activates once in profit (best effort) | `trailing_activation_pct`, `trailing_callback_rate` |
| Pre\-news halt | No new trades opened before a scheduled CPI/FOMC event | `news_trading.enabled` |
| ATR\-scaled sizing | Volatile coins get smaller size and wider stops | `atr_risk_sizing_enabled`, `risk_per_trade_pct` |
| Time stop | Closes a position that hasn't moved, freeing the slot | `max_hold_hours`, `time_stop_flat_pct` |
| Max margin usage | Total margin can't exceed X% of balance | `max_total_margin_usage` |
| Correlation | Won't open positions too correlated with each other | `correlation_threshold` |
| Consecutive losses | Pauses a strategy after N losses in a row | `consecutive_loss_limit`, `strategy_cooldown_cycles` |
| Drawdown circuit breaker | Halts entirely if the session drops X% from its peak | `max_session_drawdown_pct` |

When the circuit breaker trips: all positions are closed and the bot stops opening new trades. This is a deliberate hard stop — it does not resume automatically until manually restarted. Other `safety_lock` conditions (like hitting the profit target) recover on their own.

## Partial Take\-Profit (Scale\-Out) {#partial-take-profit-scale-out}

There used to be only one exit: `take_profit_pct` (4.5%) or `emergency_sl_pct` (3%). A trade that reached \+2% and then reversed into the SL became a full loss.

Now, when a position opens, a separate `reduceOnly` `TAKE_PROFIT_MARKET` order is placed at `partial_tp_pct` (2%) for `partial_tp_ratio` (50%) of the size. Its fill is detected by the drop in position size (there's no reliable endpoint to list algo\-order fills, so position size is the source of truth), and the stop on the remaining portion is then re\-placed at `breakeven_offset_pct` (0.1% — covers entry/exit fees).

Three outcomes: hit SL → \-3%; reach 2% and reverse → \+1.05% (was \-3%); reach full TP → \+3.25% (was \+4.5%). In other words, it gives up some size of the big wins in exchange for turning an entire category of losses into small gains — a deliberate trade\-off, not a free improvement.

Partial TP is best\-effort: if half the position doesn't meet minQty/minNotional, or the order is rejected, it's simply skipped — the hard SL and full TP stay live regardless, so the trade is never left unprotected.

## ATR\-Scaled Position Sizing {#atr-scaled-position-sizing}

Previously every coin took the same `trade_allocation` (9%) of the balance. But with BTC at 0.5% ATR and DOGE at 2.5% ATR both sized the same, a fixed 3% stop sits \~6 ATR away for the first (almost never hit) and \~1 ATR away for the second (hit constantly) — the same number meant completely different things.

The stop is now `atr_sl_multiplier × ATR%` (clamped to `atr_sl_min_pct`/`atr_sl_max_pct`), and size scales inversely with it: hitting the stop always costs `risk_per_trade_pct` of the balance. Exit ratios still come from config — ATR only stretches or shrinks the whole structure together, so it doesn't add a new dial, and with `atr_risk_sizing_enabled: false` behavior is exactly as before.

| ATR% | Stop | TP | Margin | Risk |
| --- | --- | --- | --- | --- |
| 0\.5 | 2\.00% | 3\.00% | 9\.0% | $57 |
| 1\.5 | 3\.00% | 4\.50% | 8\.3% | $79 |
| 2\.0 | 4\.00% | 6\.00% | 6\.2% | $79 |
| 3\.0 | 5\.00% | 7\.50% | 5\.0% | $79 |

(Balance $6,356, `risk_per_trade_pct: 1.25`.) `max_trade_allocation: 0.09` keeps 6 × 9% \= 54% ≤ `max_total_margin_usage`, so holding 6 positions at once is still possible. On a calm coin the clamp kicks in and risk becomes $57 instead of $79 — asymmetric on purpose.

## Time Stop {#time-stop}

If `max_hold_hours` (24h) has elapsed and the position is still sitting within `time_stop_flat_pct` (±1%), it's closed to free up the slot/margin/funding. A position that's in profit is left alone — that's handled by the trailing stop and TP. After sending the close order, it won't be resent for 5 minutes (a few cycles may pass before the position actually disappears).

## Trade Journal {#trade-journal}

When `journal_enabled`, every closed trade is written as one row to `STATE_DIR/trades.csv`\: entry score, ADX, RSI, ATR%, volume ratio, regime, MTF, the SL/TP percentages used, hold duration, exit reason (`SL_TP_TRAILING`, `TIME_STOP`, `TARGET`), whether the partial TP filled, and PnL.

The goal: tune `min_signal_score`, TP/SL, and the ATR multiplier with numbers, not guesses. After 30–50 trades, for example:

```bash
python3 -c "import pandas as pd; d=pd.read_csv('trades.csv'); \
print(d.groupby(pd.cut(d.score,[0,16,20,25,100])).pnl.agg(['count','sum','mean']))"
```

Journaling never blocks the trading loop — any write error is swallowed and left as a warning in the log.

> ⚠️ `STATE_DIR` must be on a Railway volume, otherwise the journal is wiped on every redeploy.

## News Window {#news-window}

When `news_trading.enabled: true`, the bot stops opening new technical trades starting `pause_before_minutes` before a scheduled USD event (`event_keywords`, CPI and FOMC by default), then resumes after waiting `wait_after_minutes`. Monitoring of open positions is never interrupted.

The calendar is fetched once per hour. If the fetch fails (HTTP error, non\-JSON response), the retry interval backs off with each failure (15 → 30 → 45 → 60 min) instead of flooding the log, and a single Telegram alert fires on the 3rd failure — a protection that fails silently is the most dangerous kind. A failed calendar fetch never halts trading, it just means the pre\-news halt won't trigger.

`post_news_trade` is a separate flag, off by default. It's logic that jumps into the direction of the post\-event move — it's unproven because it can't be backtested. The halt alone is meaningful on its own (it protects against getting stopped out on the spike), so the two were kept separate.

One safety principle throughout: uncertainty is never treated as safe. For example, if the position list fails to load, the bot doesn't assume "no positions" — it skips that cycle instead, since otherwise a live position's SL/TP could get cancelled.

## Backtest {#backtest}

```bash
python backtest.py --days 90                 # last 90 days, using the real account balance
python backtest.py --days 180 --balance 6000 --csv trades_bt.csv
python backtest.py --days 30 --exec-interval 5m --telegram
python backtest.py --days 90 --disable MACD_MOMENTUM,BREAKOUT   # without specific strategies
python backtest.py --days 90 --no-halt                          # full period, no drawdown breaker
python backtest.py --days 90 --sweep --disable MACD_MOMENTUM,BREAKOUT   # compare variants
python backtest.py --days 91 --offset-days 91 --sweep                   # prior quarter (out-of-sample)
python backtest.py --days 60 --windows 4 --sweep                        # 4 non-overlapping windows (walk-forward)
```

Core principle: it doesn't simulate the bot's logic, it runs the bot's actual code. The simulation only plays the exchange's role (filling orders, fees, funding, timing). Every decision comes from the live code:

| What | Which code |
| --- | --- |
| Signal, score, MTF/regime/score filters | `screening.analyze_frame` |
| Candidate selection, correlation, slots | `screening.pick_candidates` |
| ATR exit levels and position sizing | `risk.exit_levels`, `risk.position_margin` |
| Strategy cooldown, drawdown breaker | `risk.update_strategy_performance`, `risk.check_drawdown_circuit_breaker` |

So changing any value in `config.json` immediately flows through to the backtest, and there's no way to test an old strategy version once the code has been edited.

#### What It Models {#what-it-models}

The full portfolio: 6 slots, margin limits, ATR\-scaled sizing, hard SL, full TP, partial TP → breakeven, trailing stop, time stop, closing everything when `target_profit` is hit, strategy cooldown, drawdown breaker. On the cost side: taker fees (on every leg), slippage, and real historical funding.

#### Rules Set for Honesty {#rules-set-for-honesty}

- **No lookahead.** Signals only come from closed candles, and entries happen at the open of the NEXT bar. A test directly checks the timestamp of the last candle handed to the analysis.
- **Intra\-candle ordering is conservative.** If a single candle touches both SL and TP, the SL is assumed to have filled first. If multiple stops are hit at once, the one closest to the direction of the move is assumed first (physics, not a guess).
- **Gaps are accounted for.** If a candle opens below the stop, it fills at the OPEN, not at the stop price.
- **Breakeven takes effect from the next candle.** The live bot only sees a partial fill on its next monitoring cycle — that lag is replicated.
- **A trailing stop that arms within a candle doesn't also fill within that same candle.**
- **Exits are resolved on 15m candles by default** (`--exec-interval`) — resolving on 1h candles would quadruple the uncertainty.

#### What It Doesn't Model {#what-it-doesnt-model}

Liquidation, partial order fills, exchange\-info rounding (stepSize/minNotional), maker/taker fee tiers, exchange outages.

#### Early\-Stopped Runs {#early-stopped-runs}

If the drawdown circuit breaker trips, the simulation stops right there. The report is measured over the period actually traded, not the full data period — otherwise both the "X% per month" figure and the BTC comparison would count the non\-trading days too and make the bot look better than it is. The header notes which day, out of how many days of data, the run stopped on.

#### `--no-halt`\: Measuring the Full Period {#no-halt-measuring-the-full-period}

With the current settings, the bot hits its 15% limit and stops within the first week. Run `--days 90` at that point and it still only measures that one week — `--days 30` and `--days 90` give the same result, and the rest of the data is fetched for nothing.

`--no-halt` disables the breaker and runs the full period. The report separately notes which day the live bot would have stopped on (computed on the realized balance — the live breaker watches `walletBalance`), so a single run gives two answers: long\-run behavior, and where the current limit would cut it off.

#### `--sweep`\: Comparing Variants {#sweep-comparing-variants}

Running a full pass for every config to test (fetch data \+ 90\-day simulation) takes hours. But candle analysis eats \~85% of that time and doesn't depend on the config — `min_signal_score`, allowed regimes, partial TP all only affect the stage AFTER analysis, in `pick_candidates` and order placement.

So `--sweep` computes the analysis once and shares it across all variants.

The cache is built with every strategy enabled and the threshold at 0, otherwise variants looser than whatever config built the cache would silently lose trades and look falsely "worse." The actual filtering still happens inside `pick_candidates`, so results are identical to running without a cache (a test verifies this).

Default variants: baseline, STRONG\_TREND\-only, trend regimes, partial TP off, min\_score 24, STRONG\_TREND \+ partial TP off.

Sweep always disables the breaker — if variants stopped on different days, each would measure a different period and the comparison would be meaningless. The report separately notes when each one would have stopped.

> ⚠️ The best variant is being picked from a SINGLE period, so it might just have gotten lucky there. Always verify the winner on a different period.

#### `--offset-days`\: Testing on a Different Period {#offset-days-testing-on-a-different-period}

By default the window ends today, so every run measures the same recent period. Picking the best variant from a sweep is then overfitting — with 6 variants, the odds are decent that one just got lucky.

`--offset-days N` pushes the end of the window back by N days:

```bash
python backtest.py --days 91 --sweep                     # this quarter
python backtest.py --days 91 --offset-days 91  --sweep   # previous quarter
python backtest.py --days 91 --offset-days 182 --sweep   # the quarter before that
```

If the same variant wins across all three, it's real; otherwise it just got lucky. This is the last check before making a decision.

Since sweep runs with the breaker off, some variants' returns are numbers the real bot could never reach — they include the "what if there'd been no limit" assumption past the point it would have stopped. Those are marked ❌ in the table and excluded from "best." If everything hits the limit, no winner is declared — that means the risk structure needs to change first.

#### `--windows`\: Rising Above the Noise {#windows-rising-above-the-noise}

A single number from a single window can't be trusted. For example, with 167 trades and a per\-trade standard deviation of \~$52:

```
aggregate deviation = √167 × $52 ≈ $672 ≈ 10.6% of the account
```

In other words, a difference under 10% within a single window means nothing. This was confirmed in practice: running the same config twice, hours apart, produced \+3.96% and −4.64% for the baseline variant.

`--windows N` repeats the sweep across N non\-overlapping windows, and reports each variant's average, worst/best, and how many windows it was positive in. Windows aren't overlapped — that would double\-count the same data and artificially inflate confidence — so the step size equals the window length.

The criterion isn't a single number: only variants positive in EVERY window AND that never hit the 15% limit make the list. Being positive across 4 consecutive windows by chance has a 6.2% probability — that counts as evidence.

> ⚠️ Each window fetches its own data, so `--windows 4 --days 60` ≈ 40–60 minutes. Only the numbers are kept in memory (each window's \~200 MB of data is released immediately).

## What the Report Shows {#what-the-report-shows}

Balance, win rate, expectancy, profit factor, max drawdown, average hold time — plus a cost breakdown (how much of gross profit fees \+ funding ate), per\-strategy P&L, score buckets (whether `min_signal_score` is set right), exit reasons, market regime, and a plain buy\-and\-hold BTC benchmark.

## Where the Data Comes From {#where-the-data-comes-from}

Historical candles and funding are always read from the production endpoint (`backtest_data_url`, default `https://fapi.binance.com`) — demo/testnet history is too short or unrealistic to backtest meaningfully. This is public data, so no API key is needed; the live trading path still uses `BINANCE_BASE_URL`.

## Timing (Measured) {#timing-measured}

With 15 coins: 10 days ≈ 1.5 min, 30 days ≈ 4 min, 90 days ≈ 14 min of simulation. Plus 1–2 min for data fetching (\~165 paginated requests).

`--sweep` runs 6 variants in 15 minutes — running them sequentially would take 83 minutes, so it's 5.6x faster. Peak memory is 268 MB. Best run separately, without touching the main bot.

## Configuration {#configuration}

#### 1\. Secrets — `.env` {#1-secrets-env}

Copy `env.example` to `.env` and fill it in:

```bash
BINANCE_API_KEY=...
BINANCE_API_SECRET=...
BINANCE_BASE_URL=https://demo-fapi.binance.com   # demo. Live: https://fapi.binance.com
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
STATE_DIR=/data                                  # persistent volume (see below)
LOG_LEVEL=INFO                                   # DEBUG/INFO/WARNING/ERROR
```

#### 2\. Strategy Configuration — `config.json` {#2-strategy-configuration-configjson}

Commonly changed values:

| Key | Value | Description |
| --- | --- | --- |
| `trade_allocation` | 0\.09 | What % of the balance becomes margin per position |
| `leverage` | 5 | Leverage |
| `max_selections` | 6 | Max number of simultaneous positions |
| `max_candidates_per_strategy` | 3 | How many coins each strategy can put forward |
| `min_signal_score` | 14\.0 | Score threshold |
| `correlation_threshold` | 0\.85 | Drops anything more correlated than this |
| `max_session_drawdown_pct` | 15\.0 | Circuit breaker |
| `selection_interval_minutes` | 120 | Screening cadence |

When changing `min_signal_score`\: strategies' max scores range 22.5–27.2. 20 sits near the ceiling and requires near\-perfect conditions (each strategy clears it only 6–29% of the time). At 14 that becomes 40–64%. Too low lets weak signals through; too high shuts some strategies off entirely.

If you change `selection_interval_minutes`, adjust `strategy_cooldown_cycles` along with it — cooldown is counted in cycles, not minutes.

#### Exit Structure: Don't Change TP / SL / Trailing in Isolation {#exit-structure-dont-change-tp-sl-trailing-in-isolation}

These 4 values together form a single piece of math. They determine how big wins and losses are, which directly sets the win rate needed to be profitable:

| {} | TP | SL | Trailing | Breakeven win rate (TP exit) | Breakeven win rate (trailing exit) |
| --- | --- | --- | --- | --- | --- |
| Old | 3\.0% | 5\.0% | 1\.0% / 0.5% | 63\.5% | 92\.4% |
| Now | 4\.5% | 3\.0% | 3\.0% / 1.0% | 41\.1% | 61\.6% |

The old structure risked 5 to win 3, and since trailing activated at \+1% and closed on a 0.5% pullback, most wins were cut around \+0.5%. Calculated out, that structure was net −0.36% per trade even at a 70% win rate. The current structure turns positive above roughly a 50% win rate.

Trailing works together with TP, so it can't be changed in isolation: if activation is too low, it cuts wins short and raising TP becomes pointless.

> ⚠️ These numbers are structurally sounder, but not yet validated on real data. Tightening SL from 5% to 3% likely increases how often it gets hit, lowering the win rate — two effects pulling in opposite directions. Re\-evaluate this after 30\+ closed trades, same as `min_signal_score`.

## Profit Accounting Includes Fees {#profit-accounting-includes-fees}

`get_trade_realized_pnl` subtracts commission from Binance's `realizedPnl`. Since `realizedPnl` excludes fees, logging profit without this subtraction always overstates it — a trade that's \+$1 gross but net\-negative would falsely count as a "win," inflating the win rate. If fees were paid in BNB, they aren't converted and subtracted (no price feed for that) — it's only flagged.

## Running {#running}

```bash
pip install -r requirements.txt
python bot.py
```

## Testing {#testing}

```bash
pip install -r requirements-dev.txt
pytest -v                                    # 234 tests
pytest --cov=. --cov-report=term-missing
```

Tests never touch the network: an autouse fixture in `conftest.py` blocks `requests`, mocks Telegram, redirects state files to `tmp_path`, and clears runtime state before every test.

## Deploying on Railway {#deploying-on-railway}

In `railway.toml`\: `startCommand = "python bot.py"`.

**A volume is required.** Without one, every redeploy creates a fresh container and the state files are lost. The consequences:

- The drawdown peak resets → the circuit breaker "forgives" prior losses
- Open positions lose their strategy tag and become `RECOVERED`

To set it up:

1. On the project canvas, right\-click your service (or **\+ New**) → **Volume**
2. Mount path: `/data`
3. Variables → `STATE_DIR = /data`

If doing this from a phone, turning on "Desktop site" mode in the browser makes it easier.

To verify — in the Deploy Logs:

```
💾 State storage: /data (persistent volume)     ← correct
⚠️ State storage: ... temporary disk!           ← STATE_DIR not mounted
```

Once the volume is attached, logs are also written to `/data/bot.log` (rotating 5 MB × 3 files), so history survives past a redeploy.

## File Structure {#file-structure}

Modules form a one\-directional dependency chain bottom to top — a lower layer never imports from a layer above it, so there are no circular imports.

| File | Lines | Layer | What's in it |
| --- | --- | --- | --- |
| `bot.py` | 278 | — | Entry point: config checks, startup, main loop |
| `execution.py` | 222 | Execution | Turns selected signals into orders |
| `position_manager.py` | 682 | Execution | Position lifecycle: protection, monitoring, closing, recovery |
| `screening.py` | 315 | Execution | Coin analysis, correlation, cycle selection |
| `strategies.py` | 217 | Decision | Market regime, per\-strategy signal and score |
| `indicators.py` | 168 | Decision | Technical indicators (no external deps, pure functions) |
| `risk.py` | 171 | Decision | Drawdown breaker, strategy cooldown, realized PnL tracking |
| `order_api.py` | 233 | Exchange layer | Place/cancel orders (conditional orders via the Algo service) |
| `account.py` | 141 | Exchange layer | Account, positions, leverage, realized PnL |
| `market_data.py` | 260 | Exchange layer | Klines, exchange info, rounding, min notional |
| `binance_client.py` | 116 | Exchange layer | REST layer: signing, requests, rate limiting, server time |
| `state.py` | 91 | Infrastructure | Runtime state (`BotState` object) — single source of truth |
| `settings.py` | 175 | Infrastructure | Loads config from `.env` \+ `config.json` |
| `persistence.py` | 127 | Infrastructure | Reads/writes state files |
| `logging_setup.py` | 68 | Infrastructure | Log config (console \+ file on the volume) |
| `notifications.py` | 35 | Infrastructure | Telegram sending |
| `reports.py` | 94 | Infrastructure | Telegram reports |
| `telegram_format.py` | — | Infrastructure | Telegram message formatting |
| `utils.py` | 37 | Infrastructure | Small helpers (`safe_float`, `clamp`, `round_down`…) |
| `journal.py` | 81 | Extras | Logs closed trades with their entry context to CSV |
| `news.py` | 244 | Extras | News event schedule and the post\-event trade |
| `backtest.py` | 743 | Extras | Portfolio simulation: runs the bot's decision code over historical candles |
| `test_bot.py`, `conftest.py` | — | Extras | Tests |

## How Modules Call Each Other {#how-modules-call-each-other}

Modules import other modules, not names from them:

```python
import account
positions = account.get_positions()      # ✅
```

Never `from account import get_positions`. That copies the name, so if `account.get_positions` is later replaced (in a test or a patch), the old copy keeps pointing at the original. It's the exact same trap that `state.py` exists to avoid.

Similarly, `config.json` constants get copied into every module via `from settings import *`, so tests use `conftest.patch_setting()` to override one everywhere at once.

## Why State Lives in Its Own File {#why-state-lives-in-its-own-file}

Mutable values like `safety_lock`, `strategy_stats`, and the drawdown peak are attributes on the state object. If these were module\-level globals, splitting the code into modules and writing `from state import safety_lock` then `safety_lock = True` would only rebind the local name — other modules would keep seeing the old value, risking a case where trading fails to halt when it should.

## Known Limitations {#known-limitations}

- `position_manager.py` is 682 lines — the close/monitor logic could still be split out
- No retry on network timeouts (retry does exist for rate limits)
- `rebuild_protection_orders` has low test coverage
- The Algo (conditional) order listing endpoint is undocumented, so it's found by trying candidates from `ALGO_LIST_ENDPOINT_CANDIDATES`. If none of them work, a Telegram alert is sent.
