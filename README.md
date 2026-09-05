# Trading Bot

Binance USDⓈ-M Futures дээр ажилладаг автомат арилжааны бот. 6 стратегиар зэрэг
дүн шинжилгээ хийж, оноогоор эрэмбэлж, эрсдэлийн шүүлтүүр давсан хэдэн coin дээр
позиц нээдэг. Бүх мэдэгдэл Telegram руу явна.

## Хэрхэн ажилладаг вэ

Бот `main()` дотор тасралтгүй давталтаар ажиллана. Хоёр өөр давтамжтай:

| Юу | Хэр олон удаа | Тохиргоо |
|---|---|---|
| Позиц хянах, зорилт шалгах | 30 секунд тутам | `monitor_interval_sec` |
| Шинэ coin хайх (screening) | 2 цаг тутам | `selection_interval_minutes` |

**Тохирох сигнал гарвал restart шаардлагагүй** — screening цикл өөрөө дуудагдаж
арилжаа эхэлнэ. Зөвхөн drawdown circuit breaker ажилласан үед л гараар restart
хийх шаардлагатай (доороос үзнэ үү).

### Сонголтын юүлүүр

```
15 coin (symbols_pool)
   ↓  analyze_coin — индикатор бодож, стратеги бүрд signal + оноо гаргана
   ↓  MTF шүүлтүүр — 4h/1h trend-ийн эсрэг арилжаа хийхгүй
   ↓  min_signal_score — оноо хүрэхгүй signal HOLD болно
   ↓  стратеги бүрээс дээд max_candidates_per_strategy coin
   ↓  ижил symbol давхардвал өндөр оноотой нь үлдэнэ
   ↓  оноогоор эрэмбэлнэ
   ↓  корреляцийн шүүлтүүр — сонгогдсонтой correlation_threshold-оос дээш бол хасна
   ↓  max_selections хүртэл
execute_trades — маржин, minQty, minNotional шалгаад захиалга өгнө
```

### Стратегиуд

`SUPERTREND`, `MACD_MOMENTUM`, `GRID_TRADING`, `BOLLINGER_MEAN_REVERSION`,
`RSI_STRATEGY`, `TREND_FOLLOWING` — тус бүр өөрийн signal нөхцөл, оноо бодох
томьёотой. Оноонууд стратеги хооронд ижил масштабгүй тул `min_signal_score`-г
өөрчлөхдөө болгоомжтой байх (доод хэсгээс үзнэ үү).

## Эрсдэлийн хамгаалалт

| Хамгаалалт | Юу хийдэг | Тохиргоо |
|---|---|---|
| Emergency stop-loss | Позиц бүр дээр заавал тавигдана | `emergency_sl_pct` |
| Take profit | Позиц бүр дээр заавал тавигдана | `take_profit_pct` |
| Trailing stop | Ашигтай явбал идэвхжинэ (best effort) | `trailing_activation_pct`, `trailing_callback_rate` |
| Маржины дээд хязгаар | Нийт маржин балансын X%-аас хэтрэхгүй | `max_total_margin_usage` |
| Корреляци | Хоорондоо хэт хамааралтай позиц нээхгүй | `correlation_threshold` |
| Дараалсан алдагдал | Стратеги N удаа алдвал түр зогсоно | `consecutive_loss_limit`, `strategy_cooldown_cycles` |
| **Drawdown circuit breaker** | Сессийн оргилоос X% буурвал **бүрмөсөн зогсоно** | `max_session_drawdown_pct` |

**Circuit breaker ажиллавал**: бүх позиц хаагдаж, бот шинэ арилжаа хийхгүй.
Энэ бол зориудын hard stop — **гараар restart хийтэл автоматаар үргэлжлэхгүй**.
Бусад `safety_lock` тохиолдлууд (зорилтод хүрсэн гэх мэт) өөрөө сэргэдэг.

Хамгаалалтын нэг зарчим: **тодорхойгүй байдлыг аюулгүй гэж үзэхгүй**. Жишээ нь
позицын жагсаалт уншигдаагүй бол "позиц байхгүй" гэж үзэлгүй тухайн мөчлөгийг
алгасна — эс тэгвээс амьд позицын SL/TP цуцлагдах эрсдэлтэй.

## Тохируулах

### 1. Нууц утгууд — `.env`

`env.example`-ийг хуулж `.env` болгоод бөглөнө:

```
BINANCE_API_KEY=...
BINANCE_API_SECRET=...
BINANCE_BASE_URL=https://demo-fapi.binance.com   # demo. Бодит: https://fapi.binance.com
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
STATE_DIR=/data                                  # persistent volume (доороос үз)
LOG_LEVEL=INFO                                   # DEBUG/INFO/WARNING/ERROR
```

### 2. Стратегийн тохиргоо — `config.json`

Түгээмэл өөрчилдөг утгууд:

| Түлхүүр | Утга | Тайлбар |
|---|---|---|
| `trade_allocation` | 0.09 | Позиц бүрд балансын хэдэн хувийг маржин болгох |
| `leverage` | 5 | Хөшүүрэг |
| `max_selections` | 6 | Зэрэг байх позицын дээд тоо |
| `max_candidates_per_strategy` | 3 | Стратеги бүрээс хэдэн coin дэвшүүлэх |
| `min_signal_score` | 14.0 | Онооны босго |
| `correlation_threshold` | 0.85 | Үүнээс дээш хамааралтай бол хасна |
| `max_session_drawdown_pct` | 15.0 | Circuit breaker |
| `selection_interval_minutes` | 120 | Screening давтамж |

**`min_signal_score`-г өөрчлөхдөө**: стратегиудын дээд оноо 22.5–27.2 хооронд
байдаг. 20 гэдэг нь бараг таазанд байрлаж, бараг төгс нөхцөл шаарддаг (стратеги
бүр 6–29% магадлалтай давна). 14 дээр 40–64% болдог. Хэт доогуур бол сул signal
орно, хэт өндөр бол зарим стратеги бүрмөсөн унтардаг.

**`selection_interval_minutes`-г өөрчлөх бол** `strategy_cooldown_cycles`-г
хамт тааруулах — cooldown нь цаг биш **цикл**-ээр тоологддог.

## Ажиллуулах

```bash
pip install -r requirements.txt
python bot.py
```

### Тест

```bash
pip install -r requirements-dev.txt
pytest -v                                    # 228 тест
pytest --cov=bot --cov=state --cov-report=term-missing
```

Тестүүд сүлжээ рүү огт хандахгүй: `conftest.py` дэх autouse fixture нь `requests`-ийг
блоклож, Telegram-ыг мок болгож, state файлуудыг `tmp_path` руу чиглүүлж, runtime
state-ийг тест бүрийн өмнө цэвэрлэдэг.

## Railway дээр deploy хийх

`railway.toml` дотор `startCommand = "python bot.py"`.

### Volume заавал хэрэгтэй

Volume-гүй бол redeploy болгонд контейнер шинээр үүсч, state файлууд алга болно.
Үр дагавар:

- Drawdown-ы оргил утга тэглэгдэнэ → circuit breaker өмнөх алдагдлыг "уучилна"
- Нээлттэй позицууд стратегиэ алдаж `RECOVERED` болно

**Тохируулах**:
1. Project canvas дээр үйлчилгээн дээрээ баруун товших (эсвэл `+ New`) → **Volume**
2. Mount path: `/data`
3. **Variables** → `STATE_DIR` = `/data`

Гар утаснаас хийж байвал browser дээрээ "Desktop site" горим асаах нь хялбар.

**Шалгах** — Deploy Logs дотор:

```
💾 State хадгалалт: /data (persistent volume)     ← зөв
⚠️ State хадгалалт: ... түр зуурын диск!          ← STATE_DIR хүрээгүй
```

Volume холбогдсон үед log нь `/data/bot.log` руу ч бичигдэнэ (5 MB × 3 файл
эргэлдэнэ), тиймээс redeploy хийсний дараа өмнөх түүх үлдэнэ.

## Файлын бүтэц

Модулиуд доороос дээш нэг чиглэлд хамаарна — доод давхаргынх нь дээдийгээ
хэзээ ч импортлохгүй, тиймээс circular import үүсэхгүй.

| Файл | Мөр | Юу байдаг |
|---|---:|---|
| `bot.py` | 278 | Оруулах цэг: тохиргоо шалгах, эхлүүлэх, үндсэн давталт |
| **Гүйцэтгэл** | | |
| `execution.py` | 207 | Сонгогдсон signal-уудыг захиалга болгох |
| `position_manager.py` | 485 | Позицын амьдралын мөчлөг: хамгаалалт, хяналт, хаалт, сэргээлт |
| `screening.py` | 293 | Coin шинжлэх, корреляци, циклийн сонголт |
| **Шийдвэр** | | |
| `strategies.py` | 168 | Зах зээлийн горим, стратеги бүрийн signal ба оноо |
| `indicators.py` | 135 | Техникийн индикаторууд (гадаад хамааралгүй, цэвэр функцууд) |
| `risk.py` | 118 | Drawdown breaker, стратегийн түр зогсоолт, realized PnL бүртгэл |
| **Биржийн давхарга** | | |
| `order_api.py` | 210 | Захиалга байрлуулах/цуцлах (conditional захиалга Algo service дээр) |
| `account.py` | 121 | Данс, позиц, leverage, realized PnL |
| `market_data.py` | 184 | Klines, exchange info, тоймлолт, min notional |
| `binance_client.py` | 117 | REST давхарга: гарын үсэг, хүсэлт, rate limit, серверийн цаг |
| **Дэд бүтэц** | | |
| `state.py` | 90 | Runtime state (`BotState` объект) — нэг эх сурвалж |
| `settings.py` | 134 | `.env` + `config.json`-оос тохиргоо ачаалах |
| `persistence.py` | 127 | State файлуудыг унших/бичих |
| `logging_setup.py` | 68 | Log тохиргоо (консол + volume дээрх файл) |
| `notifications.py` | 55 | Telegram илгээлт |
| `reports.py` | 145 | Telegram тайлангууд ба график |
| `telegram_format.py` | — | Telegram мессежийн формат |
| `utils.py` | 39 | Жижиг туслахууд (safe_float, clamp, round_down…) |
| **Нэмэлт** | | |
| `news.py` | 150 | Мэдээний цагийн хуваарь ба дараах арилжаа |
| `backtest.py` | 138 | Түүхэн өгөгдөл дээр стратеги турших |
| `test_bot.py`, `conftest.py` | — | Тестүүд |

### Модуль хооронд хэрхэн дууддаг вэ

Модулиуд бие биенээсээ **нэр биш, модуль** импортолдог:

```python
import account
positions = account.get_positions()      # ✅
```

`from account import get_positions` гэж бичихгүй. Учир нь тэгвэл нэр хуулбарлагдаж,
дараа нь `account.get_positions`-ыг солиход (тест эсвэл засварын үед) хуучин
хуулбар нь хэвээр үлдэнэ. Энэ бол `state.py` үүссэн шалтгаантай яг ижил занга.

Мөн `config.json`-ы тогтмолууд `from settings import *`-аар модуль бүрд
хуулбарлагддаг тул тестэд нэгийг нь солихдоо `conftest.patch_setting()` ашиглаж
**бүх модульд нэгэн зэрэг** солино.

### State яагаад тусдаа файлд байдаг вэ

`safety_lock`, `strategy_stats`, drawdown-ы оргил зэрэг өөрчлөгддөг утгууд
`state` объектын атрибут байдаг. Энэ нь module-level global байсан бол кодыг
модуль болгон хуваахад `from state import safety_lock` → `safety_lock = True`
гэж бичихэд зөвхөн локал нэр солигдож, бусад модуль хуучин утгыг хараад
**арилжаа зогсох ёстой газраа зогсохгүй** байх эрсдэлтэй.

## Мэдэгдэж буй хязгаарлалт

- `position_manager.py` 485 мөр — цаашид хаалт/хяналтын хэсгийг салгаж болно
- Сүлжээний timeout дээр retry байхгүй (rate limit дээр байгаа)
- `rebuild_protection_orders` тестийн хамралт бага
- Algo (conditional) захиалга жагсаах endpoint нь баримтжуулалтгүй тул
  `ALGO_LIST_ENDPOINT_CANDIDATES` жагсаалтаас туршиж олдог. Аль нь ч ажиллахгүй
  бол Telegram-д сэрэмжлүүлэг явна
