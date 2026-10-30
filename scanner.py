
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

SNAPSHOT_FILE = Path("market_snapshot.json")

TOP_MARKETS = 100
MAX_SIGNALS = 15
INTERVAL = "15m"
INTERVAL_MS = 15 * 60 * 1000
CANDLE_LIMIT = 30

COOLDOWN_SECONDS = 60 * 60
REQUEST_TIMEOUT = 15
REQUEST_PAUSE = 0.12
TELEGRAM_MAX_LENGTH = 3500

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

        if symbol.split("-")[0].startswith("NC"):
            continue

        price = number(item.get("lastPrice"))
        quote_volume = number(item.get("quoteVolume"))

        if quote_volume <= 0:
            base_volume = number(item.get("volume"))
            quote_volume = base_volume * price

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
    """Parse candle data returned as a dictionary or an array."""

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

        # In the extended array format, field 7 is quote volume.
        # In shorter formats, field 5 is base-asset volume.
        if len(row) >= 8 and number(row[7]) > 0:
            volume = number(row[7])
            volume_type = "quote"
        else:
            volume = number(row[5])
            volume_type = "base"

    else:
        return None

    candle = {
        "time": int(number(timestamp)),
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "volume_type": volume_type,
    }

    if (
        candle["time"] <= 0
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
        print(f"WARNING {symbol}: candle data is not a list")
        return []

    candles = []

    for row in rows:
        try:
            candle = parse_candle(row)
            if candle is not None:
                candles.append(candle)
        except (IndexError, TypeError, ValueError, OverflowError):
            continue

    # Remove duplicate timestamps.
    unique = {candle["time"]: candle for candle in candles}
    candles = sorted(unique.values(), key=lambda candle: candle["time"])

    # Remove the current unfinished candle when timestamps are in ms.
    now_ms = int(time.time() * 1000)
    candles = [
        candle for candle in candles
        if (
            candle["time"] < 10**12
            or candle["time"] + INTERVAL_MS <= now_ms
        )
    ]

    return candles


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

    if isinstance(data.get("markets"), dict):
        sent = data.get("sent_signals", {})
        return {
            "markets": data["markets"],
            "sent_signals": sent if isinstance(sent, dict) else {},
        }

    return {"markets": data, "sent_signals": {}}


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


# ==================== ANALYSIS ====================

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

    prior_candles = candles[-21:-1]

    prior_high = max(candle["high"] for candle in prior_candles)
    prior_low = min(candle["low"] for candle in prior_candles)

    # Volume ratios are meaningful only when all compared
    # candles use the same volume unit.
    volume_types = {
        candle["volume_type"] for candle in candles[-21:]
    }
    volume_consistent = len(volume_types) == 1

    average_volume = (
        sum(candle["volume"] for candle in prior_candles)
        / len(prior_candles)
    )

    current_volume = last["volume"]

    volume_valid = (
        volume_consistent
        and average_volume > 0
        and current_volume > 0
    )

    volume_ratio = (
        current_volume / average_volume
        if volume_valid else 0.0
    )

    breakout_up = (
        volume_valid
        and close > prior_high
        and volume_ratio >= 1.2
    )

    breakout_down = (
        volume_valid
        and close < prior_low
        and volume_ratio >= 1.2
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
        direction = "LONG"
        title = "ПРОБОЙ ВВЕРХ"
    elif breakout_down:
        direction = "SHORT"
        title = "ПРОБОЙ ВНИЗ"
    elif change_15m >= 0.6 and change_1h > 0:
        direction = "LONG"
        title = "ИМПУЛЬС ВВЕРХ"
    elif change_15m <= -0.6 and change_1h < 0:
        direction = "SHORT"
        title = "ИМПУЛЬС ВНИЗ"
    elif change_15m > 0:
        direction = "LONG"
        title = "ВОЗМОЖНЫЙ ОТСКОК"
    else:
        direction = "SHORT"
        title = "ВОЗМОЖНЫЙ ОТКАТ"

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
    }


# ==================== TELEGRAM ====================

def split_telegram_message(message, max_length=TELEGRAM_MAX_LENGTH):
    """Split a report into chunks without breaking signal blocks."""

    paragraphs = message.split("\n\n")
    chunks = []
    current = ""

    for paragraph in paragraphs:
        if len(paragraph) > max_length:
            if current:
                chunks.append(current)
                current = ""

            for line in paragraph.split("\n"):
                if len(line) > max_length:
                    raise RuntimeError(
                        "A single Telegram line exceeds the message limit."
                    )

                candidate = f"{current}\n{line}" if current else line

                if len(candidate) > max_length:
                    if current:
                        chunks.append(current)
                    current = line
                else:
                    current = candidate

            continue

        candidate = (
            f"{current}\n\n{paragraph}" if current else paragraph
        )

        if len(candidate) > max_length:
            if current:
                chunks.append(current)
            current = paragraph
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
                f"Telegram error on message {index}/{len(chunks)}: "
                f"HTTP {response.status_code}; {result}"
            )

        print(
            f"Telegram message {index}/{len(chunks)} sent "
            f"({len(chunk)} characters)."
        )


def format_candidate(item, index):
    is_long = item["direction"] == "LONG"
    is_strong = item["strength"] == "СИЛЬНЫЙ"

    direction_icon = "🟢" if is_long else "🔴"
    strength_icon = "🔥" if is_strong else "👀"
    movement_icon = "📈" if item["change_15m"] > 0 else "📉"

    if item["volume_valid"]:
        volume_line = (
            f"┃ 🔊 Объём: {item['volume_ratio']:.2f}x от среднего"
        )
    else:
        volume_line = "┃ ⚠️ Объём: нет достоверных данных"

    return (
        f"┏━━━━━━━━━━━━━━━━━━\n"
        f"┃ {strength_icon} СИГНАЛ #{index}\n"
        f"┃ {direction_icon} {item['symbol']}\n"
        f"┃ {'🟩 LONG' if is_long else '🟥 SHORT'}\n"
        f"┣━━━━━━━━━━━━━━━━━━\n"
        f"┃ 🎯 Тип: {item['title']}\n"
        f"┃ 💵 Цена: {format_price(item['price'])} USDT\n"
        f"┃ {movement_icon} 15 минут: "
        f"{item['change_15m']:+.2f}%\n"
        f"┃ 🕐 1 час: {item['change_1h']:+.2f}%\n"
        f"┃ 📊 24 часа: {item['change_24h']:+.2f}%\n"
        f"{volume_line}\n"
        f"┃ ⭐ Рейтинг: {item['score']:.1f}\n"
        f"┗━━━━━━━━━━━━━━━━━━"
    )


# ==================== MAIN ====================

def main():
    now = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")

    snapshot = load_snapshot()
    sent_signals = snapshot["sent_signals"]

    markets = fetch_markets()

    if not markets:
        raise RuntimeError("BingX returned no markets.")

    rising = sum(1 for market in markets if market["change_24h"] > 0)
    falling = sum(1 for market in markets if market["change_24h"] < 0)

    print(
        f"Scanning {len(markets)} markets. "
        f"24h green: {rising}, red: {falling}."
    )

    try:
        test_candles = fetch_candles(markets[0]["symbol"])
        print(
            f"DEBUG {markets[0]['symbol']}: "
            f"{len(test_candles)} closed candles"
        )

        if test_candles:
            print("DEBUG latest closed candle:", test_candles[-1])
    except Exception as exc:
        print("DEBUG candle request failed:", repr(exc))

    candidates = []
    current_time = int(time.time())

    for index, market in enumerate(markets, start=1):
        symbol = market["symbol"]

        try:
            candles = fetch_candles(symbol)
            candidate = analyze_market(market, candles)

            if candidate:
                key = (
                    f"{symbol}:"
                    f"{candidate['direction']}:"
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

    candidates.sort(
        key=lambda item: item["score"],
        reverse=True,
    )

    selected = candidates[:MAX_SIGNALS]

    print("Qualifying candidates:", len(candidates))

    if selected:
        strong_count = sum(
            1 for item in selected
            if item["strength"] == "СИЛЬНЫЙ"
        )
        watch_count = len(selected) - strong_count

        lines = [
            "📡 BINGX | РЫНОЧНЫЙ РАДАР",
            "━━━━━━━━━━━━━━━━━━━━",
            f"🕒 Время: {now}",
            f"🔎 Проверено рынков: {len(markets)}",
            f"🟢 Рост за 24ч: {rising}",
            f"🔴 Падение за 24ч: {falling}",
            "",
            "🎯 РЕЗУЛЬТАТ СКАНИРОВАНИЯ",
            f"🔥 Сильных сигналов: {strong_count}",
            f"👀 Для наблюдения: {watch_count}",
            "",
            "📋 СПИСОК КАНДИДАТОВ",
            "",
        ]

        for index, item in enumerate(selected, start=1):
            lines.append(format_candidate(item, index))
            lines.append("")

        lines.extend([
            "━━━━━━━━━━━━━━━━━━━━",
            "📖 КАК ЧИТАТЬ СИГНАЛЫ",
            "🔥 СИЛЬНЫЙ — больше подтверждений движения.",
            "👀 НАБЛЮДЕНИЕ — нужна дополнительная проверка.",
            "",
            "⚠️ Сигналы не гарантируют прибыль.",
            "🛑 Проверь график и риск перед любым решением.",
        ])

        send_telegram("\n".join(lines).strip())

        for item in selected:
            sent_signals[item["key"]] = current_time

        print(f"Telegram report sent: {len(selected)} candidates.")
    else:
        print("No interesting new candidates. Telegram message not sent.")

    market_snapshot = {
        market["symbol"]: {
            "price": market["price"],
            "volume": market["volume"],
            "change_24h": market["change_24h"],
        }
        for market in markets
    }

    save_snapshot(market_snapshot, sent_signals)


if __name__ == "__main__":
    main()
