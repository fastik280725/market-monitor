
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ==================== SETTINGS ====================

BASE_URL = "https://open-api.bingx.com"
TICKER_URL = f"{BASE_URL}/openApi/swap/v2/quote/ticker"
KLINES_URL = f"{BASE_URL}/openApi/swap/v3/quote/klines"
FUNDING_URL = f"{BASE_URL}/openApi/swap/v2/quote/premiumIndex"
OPEN_INTEREST_URL = f"{BASE_URL}/openApi/swap/v2/quote/openInterest"

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")

SNAPSHOT_FILE = Path("market_snapshot.json")

TOP_MARKETS = 100
MAX_CANDIDATES = 5

INTERVAL = "15m"
INTERVAL_MS = 15 * 60 * 1000
CANDLE_LIMIT = 30

COOLDOWN_SECONDS = 60 * 60
REQUEST_TIMEOUT = 25
GEMINI_TIMEOUT = 90
REQUEST_PAUSE = 0.12
TELEGRAM_MAX_LENGTH = 3500

RETRY_INITIAL_SECONDS = 5
RETRY_MAX_SECONDS = 60

session = requests.Session()


# ==================== HELPERS ====================

def number(value, default=0.0):
    try:
        result = float(value)
        if result != result or abs(result) == float("inf"):
            return default
        return result
    except (TypeError, ValueError, OverflowError):
        return default


def api_get(url, params=None):
    last_error = None

    for attempt in range(3):
        try:
            response = session.get(
                url,
                params=params,
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            result = response.json()

            if isinstance(result, dict):
                code = result.get("code", 0)
                if code not in (0, "0", None):
                    raise RuntimeError(
                        f"BingX API: {code} {result.get('msg', '')}"
                    )

            return result

        except (
            requests.RequestException,
            ValueError,
            RuntimeError,
        ) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(attempt + 1)

    raise RuntimeError(f"Request failed: {last_error}")


def format_price(price):
    if price >= 1000:
        return f"{price:,.2f}"
    if price >= 1:
        return f"{price:.4f}".rstrip("0").rstrip(".")
    if price >= 0.01:
        return f"{price:.6f}".rstrip("0").rstrip(".")
    return f"{price:.10f}".rstrip("0").rstrip(".")


# ==================== MARKET DATA ====================

def fetch_markets():
    result = api_get(TICKER_URL)
    data = result.get("data", [])

    if isinstance(data, dict):
        data = [data]

    markets = []

    for item in data:
        if not isinstance(item, dict):
            continue

        symbol = str(item.get("symbol", ""))

        if not symbol.endswith("-USDT"):
            continue

        price = number(item.get("lastPrice"))
        quote_volume = number(item.get("quoteVolume"))

        if quote_volume <= 0:
            quote_volume = number(item.get("volume")) * price

        if price <= 0:
            continue

        markets.append({
            "symbol": symbol,
            "price": price,
            "volume": quote_volume,
            "change_24h": number(item.get("priceChangePercent")),
        })

    markets.sort(key=lambda item: item["volume"], reverse=True)
    return markets[:TOP_MARKETS]


def parse_candle(row):
    if isinstance(row, dict):
        timestamp = row.get(
            "time",
            row.get("timestamp", row.get("openTime", 0)),
        )
        open_price = number(row.get("open"))
        high = number(row.get("high"))
        low = number(row.get("low"))
        close = number(row.get("close"))

        quote_volume = number(
            row.get("quoteVolume", row.get("quote_volume", 0))
        )

        if quote_volume > 0:
            volume = quote_volume
            volume_type = "quote"
        else:
            volume = number(row.get("volume", row.get("vol", 0)))
            volume_type = "base"

    elif isinstance(row, (list, tuple)) and len(row) >= 6:
        timestamp = row[0]
        open_price = number(row[1])
        high = number(row[2])
        low = number(row[3])
        close = number(row[4])

        if len(row) >= 8 and number(row[7]) > 0:
            volume = number(row[7])
            volume_type = "quote"
        else:
            volume = number(row[5])
            volume_type = "base"
    else:
        return None

    timestamp = int(number(timestamp))

    candle = {
        "time": timestamp,
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "volume_type": volume_type,
    }

    if (
        timestamp <= 0
        or min(open_price, high, low, close) <= 0
        or volume < 0
    ):
        return None

    return candle


def fetch_candles(symbol):
    result = api_get(
        KLINES_URL,
        params={
            "symbol": symbol,
            "interval": INTERVAL,
            "limit": CANDLE_LIMIT,
        },
    )

    rows = result.get("data", [])
    if not isinstance(rows, list):
        return []

    candles = []

    for row in rows:
        try:
            candle = parse_candle(row)
            if candle:
                candles.append(candle)
        except (IndexError, TypeError, ValueError, OverflowError):
            continue

    candles = sorted(
        {c["time"]: c for c in candles}.values(),
        key=lambda c: c["time"],
    )

    now_ms = int(time.time() * 1000)

    candles = [
        c for c in candles
        if c["time"] < 10**12
        or c["time"] + INTERVAL_MS <= now_ms
    ]

    return candles


def fetch_optional_metrics(symbol):
    metrics = {
        "funding_rate": None,
        "open_interest": None,
        "funding_note": "Данные не получены",
        "oi_note": "Данные не получены",
    }

    try:
        result = api_get(FUNDING_URL, params={"symbol": symbol})
        data = result.get("data", {})
        if isinstance(data, list):
            data = data[0] if data else {}

        if isinstance(data, dict):
            for key in ("lastFundingRate", "fundingRate"):
                if data.get(key) is not None:
                    metrics["funding_rate"] = number(data[key])
                    metrics["funding_note"] = "Получено из BingX"
                    break
    except Exception as exc:
        metrics["funding_note"] = f"Недоступно: {str(exc)[:120]}"

    try:
        result = api_get(OPEN_INTEREST_URL, params={"symbol": symbol})
        data = result.get("data", {})
        if isinstance(data, list):
            data = data[0] if data else {}

        if isinstance(data, dict):
            for key in ("openInterest", "openInterestAmount", "amount"):
                if data.get(key) is not None:
                    metrics["open_interest"] = number(data[key])
                    metrics["oi_note"] = "Получено из BingX"
                    break
    except Exception as exc:
        metrics["oi_note"] = f"Недоступно: {str(exc)[:120]}"

    return metrics


# ==================== SNAPSHOT ====================

def load_snapshot():
    try:
        data = json.loads(
            SNAPSHOT_FILE.read_text(encoding="utf-8")
        )
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"markets": {}, "sent_signals": {}}

    if not isinstance(data, dict):
        return {"markets": {}, "sent_signals": {}}

    markets = data.get("markets", {})
    sent = data.get("sent_signals", {})

    if not isinstance(markets, dict):
        markets = {}
    if not isinstance(sent, dict):
        sent = {}

    return {"markets": markets, "sent_signals": sent}


def save_snapshot(markets, sent_signals):
    data = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "markets": markets,
        "sent_signals": sent_signals,
    }

    SNAPSHOT_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ==================== TECHNICAL ANALYSIS ====================

def analyze_market(market, candles):
    if len(candles) < 22:
        return None

    last = candles[-1]
    previous = candles[-2]
    one_hour_ago = candles[-5]

    close = last["close"]
    previous_close = previous["close"]
    hour_close = one_hour_ago["close"]

    if min(close, previous_close, hour_close) <= 0:
        return None

    change_15m = (close / previous_close - 1) * 100
    change_1h = (close / hour_close - 1) * 100
    change_24h = market["change_24h"]

    prior = candles[-21:-1]
    prior_high = max(c["high"] for c in prior)
    prior_low = min(c["low"] for c in prior)

    volume_types = {c["volume_type"] for c in candles[-21:]}
    volume_consistent = len(volume_types) == 1

    average_volume = sum(c["volume"] for c in prior) / len(prior)
    current_volume = last["volume"]

    volume_valid = (
        volume_consistent
        and average_volume > 0
        and current_volume > 0
    )

    volume_ratio = (
        current_volume / average_volume if volume_valid else 0.0
    )

    breakout_up = (
        volume_valid and close > prior_high and volume_ratio >= 1.2
    )
    breakout_down = (
        volume_valid and close < prior_low and volume_ratio >= 1.2
    )

    score = (
        abs(change_15m) * 1.6
        + abs(change_1h) * 0.8
        + min(volume_ratio, 5.0) * 1.2
        + min(abs(change_24h), 20.0) * 0.12
    )

    if breakout_up or breakout_down:
        score += 4.0

    if change_15m * change_1h > 0:
        score += 0.8

    interesting = (
        score >= 2.7
        or (
            volume_valid
            and abs(change_15m) >= 0.6
            and volume_ratio >= 1.1
        )
        or (
            volume_valid
            and abs(change_1h) >= 1.5
            and volume_ratio >= 1.1
        )
        or breakout_up
        or breakout_down
    )

    if not interesting:
        return None

    if breakout_up:
        direction, title = "LONG", "ПРОБОЙ ВВЕРХ"
    elif breakout_down:
        direction, title = "SHORT", "ПРОБОЙ ВНИЗ"
    elif change_15m >= 0.6 and change_1h > 0:
        direction, title = "LONG", "ИМПУЛЬС ВВЕРХ"
    elif change_15m <= -0.6 and change_1h < 0:
        direction, title = "SHORT", "ИМПУЛЬС ВНИЗ"
    elif change_15m > 0:
        direction, title = "LONG", "ВОЗМОЖНЫЙ ОТСКОК"
    else:
        direction, title = "SHORT", "ВОЗМОЖНЫЙ ОТКАТ"

    strong = (
        breakout_up
        or breakout_down
        or (
            volume_valid
            and abs(change_15m) >= 1.0
            and volume_ratio >= 1.4
        )
        or (
            volume_valid
            and abs(change_1h) >= 2.0
            and volume_ratio >= 1.3
            and change_15m * change_1h > 0
        )
    )

    return {
        "symbol": market["symbol"],
        "price": close,
        "direction": direction,
        "title": title,
        "strength": "СИЛЬНЫЙ" if strong else "НАБЛЮДЕНИЕ",
        "change_15m": change_15m,
        "change_1h": change_1h,
        "change_24h": change_24h,
        "volume_ratio": volume_ratio,
        "volume_valid": volume_valid,
        "score": score,
        "prior_high": prior_high,
        "prior_low": prior_low,
        "candles": candles,
    }


# ==================== GEMINI AI ====================

ANALYSIS_PROMPT = """
Ты — аналитический помощник по рынку криптовалютных USDT-M perpetual
фьючерсов. Тебе переданы данные нескольких монет. Сравни их между собой
и выбери не более одной лучшей торговой возможности.

Не обязан выбирать сделку. Если ни один вариант не обоснован, итог —
«НЕТ СДЕЛКИ». Не подменяй качество сетапа величиной движения или
предварительным рейтингом скрипта.

Используй только предоставленные данные. Не утверждай, что видишь
реальные ордербуки, ликвидационные карты, дельту, спуфинг или действия
маркет-мейкеров, если такие данные не переданы. Отсутствующие метрики
обозначай как «нет данных». Свечи и объём не позволяют достоверно
установить действия конкретных крупных игроков.

Сначала сравни все монеты по:
- структуре и направлению движения;
- возможным BoS/ChoCH и свингам, если их можно обосновать;
- ликвидности и вероятным зонам стопов;
- объёму относительно предыдущих свечей;
- funding rate и open interest, если они доступны;
- качеству потенциального входа и возможности определить отмену сценария;
- риску входа после уже состоявшегося импульса.

Не называй приблизительный диапазон свечей полноценным Volume Profile,
POC или дельтой. Не выдумывай Long/Short Ratio, ликвидации и данные стакана.

Ответ на русском языке, компактный, но содержательный. Используй строго
такую структуру:

🏆 ЛУЧШАЯ ВОЗМОЖНОСТЬ
Монета:
Направление: LONG / SHORT / НЕТ СДЕЛКИ
Уверенность: низкая / средняя / высокая
Почему этот вариант лучше остальных:
Ключевые подтверждения:
Что говорит против сделки:

📊 СРАВНЕНИЕ ПЯТИ МОНЕТ
Кратко перечисли каждую монету, bias и главную причину, почему она
лучше или хуже победителя. Если кандидатов меньше пяти, укажи фактическое
количество. Не придумывай отсутствующие данные.

🎯 ТОРГОВЫЙ ПЛАН
Тип: ожидание подтверждения / лимитный / рыночный
Вход: конкретная цена либо точное условие подтверждения
Stop Loss: конкретный уровень или «не определён»
Take Profit: конкретный уровень или «не определён»
Соотношение потенциальной прибыли к риску: оцени только если уровни
достаточно обоснованы; иначе «не определено».
Отмена сценария:
Что должно произойти перед входом:

Если нет убедительной сделки, укажи «НЕТ СДЕЛКИ» и объясни почему.
Не выдумывай точные уровни, если данные их не обосновывают. Не обещай
прибыль. Не называй план гарантированным. Не исполняй сделки.
"""


def build_candidate_data(candidate, metrics):
    candles = candidate["candles"][-22:]

    compact_candles = [
        {
            "time": datetime.fromtimestamp(
                c["time"] / 1000 if c["time"] >= 10**12 else c["time"],
                tz=timezone.utc,
            ).isoformat(),
            "open": c["open"],
            "high": c["high"],
            "low": c["low"],
            "close": c["close"],
            "volume": c["volume"],
            "volume_type": c["volume_type"],
        }
        for c in candles
    ]

    return {
        "symbol": candidate["symbol"],
        "current_price": candidate["price"],
        "preliminary_direction": candidate["direction"],
        "preliminary_label": candidate["title"],
        "preliminary_strength": candidate["strength"],
        "preliminary_score": round(candidate["score"], 2),
        "change_15m_pct": round(candidate["change_15m"], 3),
        "change_1h_pct": round(candidate["change_1h"], 3),
        "change_24h_pct": round(candidate["change_24h"], 3),
        "volume_ratio": (
            round(candidate["volume_ratio"], 3)
            if candidate["volume_valid"] else None
        ),
        "prior_20_candle_high": candidate["prior_high"],
        "prior_20_candle_low": candidate["prior_low"],
        "funding_rate": metrics["funding_rate"],
        "funding_data_status": metrics["funding_note"],
        "open_interest": metrics["open_interest"],
        "open_interest_status": metrics["oi_note"],
        "candles_15m_utc": compact_candles,
        "limitations": [
            "No order book snapshot",
            "No liquidation feed",
            "No long/short ratio",
            "No trade-level buy/sell delta",
            "No full volume profile",
        ],
    }


def is_retryable_status(status_code, error_text=""):
    if status_code in (408, 429, 500, 502, 503, 504):
        return True

    text = error_text.lower()
    retry_markers = (
        "overloaded",
        "high demand",
        "temporarily unavailable",
        "resource_exhausted",
        "rate limit",
        "too many requests",
        "try again later",
        "service unavailable",
        "internal error",
    )

    return any(marker in text for marker in retry_markers)


def gemini_analyze_many(candidates, metrics_by_symbol):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("Не задан секрет GEMINI_API_KEY")

    data = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "timeframe": INTERVAL,
        "candidate_count": len(candidates),
        "candidates": [
            build_candidate_data(
                candidate,
                metrics_by_symbol[candidate["symbol"]],
            )
            for candidate in candidates
        ],
    }

    url = f"{GEMINI_BASE_URL}/{GEMINI_MODEL}:generateContent"

    payload = {
        "system_instruction": {
            "parts": [{"text": ANALYSIS_PROMPT}]
        },
        "contents": [{
            "role": "user",
            "parts": [{
                "text": (
                    "Сравни все переданные рынки в рамках одного анализа. "
                    "Не анализируй их изолированно: выбери лучший сетап "
                    "с учётом риска или честно укажи, что сделки нет.\n\n"
                    + json.dumps(data, ensure_ascii=False)
                )
            }]
        }],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 3500,
        },
    }

    delay = RETRY_INITIAL_SECONDS
    attempt = 0

    while True:
        attempt += 1
        print(
            f"Gemini combined analysis attempt {attempt} "
            f"for {len(candidates)} candidates."
        )

        try:
            response = session.post(
                url,
                headers={
                    "x-goog-api-key": api_key,
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=GEMINI_TIMEOUT,
            )

        except (requests.Timeout, requests.ConnectionError) as exc:
            print(
                f"Temporary Gemini connection error: {exc}. "
                f"Retrying in {delay} seconds."
            )
            time.sleep(delay)
            delay = min(delay * 2, RETRY_MAX_SECONDS)
            continue

        except requests.RequestException as exc:
            print(
                f"Gemini request error: {exc}. "
                f"Retrying in {delay} seconds."
            )
            time.sleep(delay)
            delay = min(delay * 2, RETRY_MAX_SECONDS)
            continue

        try:
            result = response.json()
        except ValueError:
            result = {
                "error": {
                    "message": response.text[:1000]
                }
            }

        if not response.ok:
            error_message = result.get(
                "error", {}
            ).get("message", str(result))

            if is_retryable_status(
                response.status_code,
                error_message,
            ):
                retry_after = response.headers.get("Retry-After")
                try:
                    wait_seconds = max(delay, float(retry_after or 0))
                except ValueError:
                    wait_seconds = delay

                wait_seconds = min(wait_seconds, RETRY_MAX_SECONDS)

                print(
                    f"Temporary Gemini HTTP {response.status_code}: "
                    f"{error_message[:250]}. "
                    f"Retrying in {wait_seconds:g} seconds."
                )

                time.sleep(wait_seconds)
                delay = min(delay * 2, RETRY_MAX_SECONDS)
                continue

            raise RuntimeError(
                f"Gemini HTTP {response.status_code}: "
                f"{error_message[:700]}"
            )

        try:
            parts = result["candidates"][0]["content"]["parts"]
            analysis = "\n".join(
                part["text"] for part in parts if "text" in part
            ).strip()
        except (KeyError, IndexError, TypeError):
            error_message = json.dumps(
                result, ensure_ascii=False
            )[:1000]

            if is_retryable_status(200, error_message):
                print(
                    "Gemini returned a temporary overload response. "
                    f"Retrying in {delay} seconds."
                )
                time.sleep(delay)
                delay = min(delay * 2, RETRY_MAX_SECONDS)
                continue

            raise RuntimeError(
                f"Gemini вернул неожиданный ответ: {error_message}"
            )

        if not analysis:
            raise RuntimeError("Gemini вернул пустой анализ.")

        print(f"Gemini analysis succeeded on attempt {attempt}.")
        return analysis


# ==================== TELEGRAM ====================

def split_telegram_message(message, max_length=TELEGRAM_MAX_LENGTH):
    if len(message) <= max_length:
        return [message]

    chunks = []
    current = ""

    for paragraph in message.split("\n\n"):
        if len(paragraph) > max_length:
            lines = paragraph.split("\n")
        else:
            lines = [paragraph]

        for line in lines:
            candidate = f"{current}\n\n{line}" if current else line

            if len(candidate) > max_length:
                if current:
                    chunks.append(current)
                current = line
            else:
                current = candidate

    if current:
        chunks.append(current)

    return chunks


def send_telegram(message):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        raise RuntimeError(
            "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID"
        )

    chunks = split_telegram_message(message)

    for index, chunk in enumerate(chunks, start=1):
        response = session.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": chunk,
                "disable_web_page_preview": True,
            },
            timeout=REQUEST_TIMEOUT,
        )

        try:
            result = response.json()
        except ValueError:
            result = {"description": response.text}

        if not response.ok or not result.get("ok"):
            raise RuntimeError(
                f"Telegram error {index}/{len(chunks)}: {result}"
            )

        print(
            f"Telegram message {index}/{len(chunks)} sent "
            f"({len(chunk)} characters)."
        )


# ==================== MAIN ====================

def main():
    started = datetime.now(timezone.utc)
    print("BingX market scanner started:", started.isoformat())

    snapshot = load_snapshot()
    sent_signals = snapshot["sent_signals"]

    markets = fetch_markets()

    if not markets:
        raise RuntimeError("BingX returned no markets.")

    rising = sum(1 for m in markets if m["change_24h"] > 0)
    falling = sum(1 for m in markets if m["change_24h"] < 0)

    print(
        f"Scanning {len(markets)} markets. "
        f"24h green: {rising}, red: {falling}."
    )

    candidates = []
    current_time = int(time.time())

    for index, market in enumerate(markets, start=1):
        symbol = market["symbol"]

        try:
            candles = fetch_candles(symbol)
            candidate = analyze_market(market, candles)

            if candidate:
                key = (
                    f"{symbol}:{candidate['direction']}:"
                    f"{candidate['strength']}"
                )
                last_sent = int(sent_signals.get(key, 0) or 0)

                if current_time - last_sent >= COOLDOWN_SECONDS:
                    candidate["key"] = key
                    candidates.append(candidate)

        except Exception as exc:
            print(f"[{index}/{len(markets)}] {symbol}: {exc}")

        if index < len(markets):
            time.sleep(REQUEST_PAUSE)

    candidates.sort(key=lambda item: item["score"], reverse=True)
    selected = candidates[:MAX_CANDIDATES]

    print("Qualifying candidates:", len(candidates))
    print("Candidates sent to Gemini:", len(selected))

    if selected:
        metrics_by_symbol = {}

        for index, candidate in enumerate(selected, start=1):
            symbol = candidate["symbol"]
            print(
                f"Fetching futures metrics {index}/{len(selected)}: "
                f"{symbol}"
            )
            metrics_by_symbol[symbol] = fetch_optional_metrics(symbol)

        try:
            # Один запрос Gemini на весь набор кандидатов.
            analysis = gemini_analyze_many(
                selected,
                metrics_by_symbol,
            )

            symbols = ", ".join(
                candidate["symbol"] for candidate in selected
            )

            message = (
                "🧠 BINGX | СРАВНЕНИЕ РЫНКОВ\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"🕒 {started.strftime('%d.%m.%Y %H:%M UTC')}\n"
                f"🔎 Проверено рынков: {len(markets)}\n"
                f"📊 Кандидаты: {symbols}\n"
                f"🟢 Рост: {rising} | 🔴 Падение: {falling}\n"
                "━━━━━━━━━━━━━━━━━━\n\n"
                f"{analysis}\n\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "⚠️ Анализ не гарантирует результат. "
                "Проверь актуальную цену, риск и условия входа. "
                "Автоматическое исполнение сделок отключено."
            )

            # Промежуточные ошибки Gemini в Telegram не отправляются.
            send_telegram(message)

            # Cooldown обновляется только после успешного анализа
            # и отправки итогового отчёта.
            for candidate in selected:
                sent_signals[candidate["key"]] = current_time

        except Exception as exc:
            # Сюда попадут неисправимые ошибки, например неверный ключ
            # или недоступная модель. Перегрузки и временные ошибки
            # обрабатываются внутри gemini_analyze_many.
            print(f"Combined analysis failed: {exc}")

            try:
                send_telegram(
                    "⚠️ Не удалось выполнить анализ рынка.\n"
                    f"Причина: {str(exc)[:700]}\n"
                    "Проверь конфигурацию Gemini и журнал GitHub Actions."
                )
            except Exception as telegram_exc:
                print("Telegram error report failed:", telegram_exc)

    else:
        print("No interesting new candidates.")

    market_snapshot = {
        market["symbol"]: {
            "price": market["price"],
            "volume": market["volume"],
            "change_24h": market["change_24h"],
        }
        for market in markets
    }

    save_snapshot(market_snapshot, sent_signals)
    print("Snapshot saved.")


if __name__ == "__main__":
    main()
