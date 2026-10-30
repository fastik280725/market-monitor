
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
CANDLE_INTERVAL = "15m"
CANDLE_LIMIT = 30

COOLDOWN_SECONDS = 60 * 60
REQUEST_TIMEOUT = 15
REQUEST_PAUSE = 0.12

session = requests.Session()


# ==================== API ====================

def request_json(url, params=None):
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
                        f"BingX API error: {code} "
                        f"{result.get('msg', '')}"
                    )

            return result

        except (requests.RequestException, ValueError, RuntimeError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(1 + attempt)

    raise RuntimeError(f"Request failed: {last_error}")


def as_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def fetch_markets():
    result = request_json(TICKER_URL)
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

        base = symbol.split("-")[0]
        if base.startswith("NC"):
            continue

        price = as_float(item.get("lastPrice"))
        volume = as_float(
            item.get("quoteVolume", item.get("volume", 0))
        )

        change = as_float(
            item.get(
                "priceChangePercent",
                item.get("priceChange", 0),
            )
        )

        # Some API responses express this value as a fraction.
        if abs(change) <= 1 and change != 0:
            change *= 100

        if price <= 0:
            continue

        markets.append({
            "symbol": symbol,
            "price": price,
            "volume": volume,
            "change_24h": change,
        })

    markets.sort(key=lambda x: x["volume"], reverse=True)
    return markets[:TOP_MARKETS]


def fetch_klines(symbol):
    result = request_json(
        KLINES_URL,
        params={
            "symbol": symbol,
            "interval": CANDLE_INTERVAL,
            "limit": CANDLE_LIMIT,
        },
    )

    rows = result.get("data", [])

    if not isinstance(rows, list):
        return []

    candles = []

    for row in rows:
        try:
            if isinstance(row, dict):
                candle = {
                    "time": int(row.get("time", row.get("timestamp", 0))),
                    "open": as_float(row.get("open")),
                    "high": as_float(row.get("high")),
                    "low": as_float(row.get("low")),
                    "close": as_float(row.get("close")),
                    "quote_volume": as_float(row.get("quoteVolume")),
                }
            else:
                candle = {
                    "time": int(float(row[0])),
                    "open": as_float(row[1]),
                    "high": as_float(row[2]),
                    "low": as_float(row[3]),
                    "close": as_float(row[4]),
                    "quote_volume": as_float(row[7]),
                }

            if candle["close"] > 0:
                candles.append(candle)

        except (IndexError, TypeError, ValueError):
            continue

    candles.sort(key=lambda x: x["time"])

    # Exclude the currently forming candle where possible.
    now_ms = int(time.time() * 1000)
    candles = [
        c for c in candles
        if c["time"] == 0 or c["time"] + 15 * 60 * 1000 <= now_ms
    ]

    return candles


# ==================== SNAPSHOT ====================

def load_snapshot():
    try:
        data = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"markets": {}, "sent_signals": {}}

    if not isinstance(data, dict):
        return {"markets": {}, "sent_signals": {}}

    if isinstance(data.get("markets"), dict):
        return {
            "markets": data["markets"],
            "sent_signals": data.get("sent_signals", {}),
        }

    # Compatibility with the older snapshot format.
    return {"markets": data, "sent_signals": {}}


def save_snapshot(markets, sent_signals):
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "markets": markets,
        "sent_signals": sent_signals,
    }

    SNAPSHOT_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ==================== SIGNAL DETECTION ====================

def detect_signal(symbol, candles):
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

    previous_candles = candles[-21:-1]
    prior_high = max(c["high"] for c in previous_candles)
    prior_low = min(c["low"] for c in previous_candles)

    avg_volume = sum(
        c["quote_volume"] for c in previous_candles
    ) / len(previous_candles)

    current_volume = last["quote_volume"]

    if avg_volume > 0:
        volume_ratio = current_volume / avg_volume
    else:
        volume_ratio = 0

    signal_type = None
    direction = None
    description = None
    bonus = 0

    # Breakout above/below the previous 20 candles.
    if close > prior_high and volume_ratio >= 1.3:
        signal_type = "ПРОБОЙ ВВЕРХ"
        direction = "LONG"
        description = "Цена выше максимума предыдущих свечей"
        bonus = 4

    elif close < prior_low and volume_ratio >= 1.3:
        signal_type = "ПРОБОЙ ВНИЗ"
        direction = "SHORT"
        description = "Цена ниже минимума предыдущих свечей"
        bonus = 4

    # Strong short-term impulse.
    elif change_15m >= 1.5 and change_1h >= 1.0 and volume_ratio >= 1.2:
        signal_type = "ИМПУЛЬС ВВЕРХ"
        direction = "LONG"
        description = "Сильное движение с повышенным объёмом"
        bonus = 2

    elif change_15m <= -1.5 and change_1h <= -1.0 and volume_ratio >= 1.2:
        signal_type = "ИМПУЛЬС ВНИЗ"
        direction = "SHORT"
        description = "Сильное движение с повышенным объёмом"
        bonus = 2

    # Possible reversal after a sizeable move.
    elif change_1h <= -3.5 and change_15m >= 1.0 and volume_ratio >= 1.2:
        signal_type = "ВОЗМОЖНЫЙ ОТСКОК"
        direction = "LONG"
        description = "Рост после сильного падения"
        bonus = 1

    elif change_1h >= 3.5 and change_15m <= -1.0 and volume_ratio >= 1.2:
        signal_type = "ВОЗМОЖНЫЙ ОТКАТ"
        direction = "SHORT"
        description = "Снижение после сильного роста"
        bonus = 1

    if signal_type is None:
        return None

    # Heuristic ranking: stronger movement, volume and breakout
    # receive higher priority. This is not a probability of profit.
    score = (
        abs(change_15m) * 2
        + abs(change_1h)
        + min(volume_ratio, 5) * 2
        + bonus
    )

    return {
        "symbol": symbol,
        "type": signal_type,
        "direction": direction,
        "description": description,
        "price": close,
        "change_15m": change_15m,
        "change_1h": change_1h,
        "volume_ratio": volume_ratio,
        "score": score,
    }


# ==================== TELEGRAM ====================

def send_telegram(message):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        raise RuntimeError(
            "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID secret"
        )

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    response = session.post(
        url,
        json={
            "chat_id": chat_id,
            "text": message,
            "disable_web_page_preview": True,
        },
        timeout=REQUEST_TIMEOUT,
    )

    response.raise_for_status()
    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(f"Telegram error: {result}")


def format_price(price):
    if price >= 1000:
        return f"{price:,.2f}"
    if price >= 1:
        return f"{price:.4f}"
    if price >= 0.01:
        return f"{price:.5f}"
    return f"{price:.8f}".rstrip("0")


def format_signal(signal, number):
    return (
        f"{number}. {signal['symbol']} — {signal['type']}\n"
        f"   Цена: {format_price(signal['price'])} USDT\n"
        f"   15м: {signal['change_15m']:+.2f}% | "
        f"1ч: {signal['change_1h']:+.2f}%\n"
        f"   Объём: {signal['volume_ratio']:.1f}x от среднего"
    )


# ==================== MAIN ====================

def main():
    now = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    snapshot = load_snapshot()
    sent_signals = snapshot.get("sent_signals", {})

    try:
        markets = fetch_markets()
    except Exception as exc:
        print(f"Could not fetch market list: {exc}")
        raise

    if not markets:
        print("No markets returned by BingX.")
        return

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
            candles = fetch_klines(symbol)
            signal = detect_signal(symbol, candles)

            if signal:
                key = f"{symbol}:{signal['type']}"
                last_sent = int(sent_signals.get(key, 0) or 0)

                if current_time - last_sent >= COOLDOWN_SECONDS:
                    signal["key"] = key
                    candidates.append(signal)

        except Exception as exc:
            print(f"[{index}/{len(markets)}] {symbol}: {exc}")

        if index < len(markets):
            time.sleep(REQUEST_PAUSE)

    # Best candidates first, rather than whichever symbols were scanned first.
    candidates.sort(key=lambda item: item["score"], reverse=True)
    selected = candidates[:MAX_SIGNALS]

    if selected:
        lines = [
            "BINGX — РЫНОЧНЫЙ РАДАР",
            f"Время: {now}",
            f"Сканировано: {len(markets)} монет из топа по обороту",
            f"Рынок за 24ч: рост {rising} | падение {falling}",
            f"Новых сигналов: {len(selected)}",
            "",
        ]

        for number, signal in enumerate(selected, start=1):
            lines.append(format_signal(signal, number))
            lines.append("")

        lines.extend([
            "Сигналы — повод проверить график, не команда на вход.",
            "Крипторынок волатилен; возможны ложные пробои.",
        ])

        message = "\n".join(lines).strip()
        send_telegram(message)

        # Only mark signals as sent after Telegram accepts the message.
        for signal in selected:
            sent_signals[signal["key"]] = current_time

        print(f"Telegram report sent: {len(selected)} signals.")
    else:
        print("No new signals. Telegram message not sent.")

    # Keep the previous snapshot format compatible with this version.
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
