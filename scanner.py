
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
CANDLE_LIMIT = 30

COOLDOWN_SECONDS = 60 * 60
REQUEST_TIMEOUT = 15
REQUEST_PAUSE = 0.12

session = requests.Session()


# ==================== HELPERS ====================

def number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
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
        return f"{price:.4f}"
    if price >= 0.01:
        return f"{price:.5f}"
    return f"{price:.8f}".rstrip("0")


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
        volume = number(item.get("quoteVolume"))

        if price <= 0:
            continue

        markets.append({
            "symbol": symbol,
            "price": price,
            "volume": volume,
            "change_24h": number(item.get("priceChangePercent")),
        })

    markets.sort(key=lambda x: x["volume"], reverse=True)
    return markets[:TOP_MARKETS]


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
        print(f"DEBUG {symbol}: candle data is not a list")
        return []

    candles = []

    for row in rows:
        try:
            if isinstance(row, dict):
                candle = {
                    "time": int(number(
                        row.get("time", row.get("timestamp", 0))
                    )),
                    "open": number(row.get("open")),
                    "high": number(row.get("high")),
                    "low": number(row.get("low")),
                    "close": number(row.get("close")),
                    "volume": number(row.get("quoteVolume")),
                }
            else:
                candle = {
                    "time": int(number(row[0])),
                    "open": number(row[1]),
                    "high": number(row[2]),
                    "low": number(row[3]),
                    "close": number(row[4]),
                    "volume": number(row[7]),
                }

            if candle["close"] > 0:
                candles.append(candle)

        except (IndexError, TypeError, ValueError):
            continue

    candles.sort(key=lambda c: c["time"])

    # Exclude the currently forming candle when timestamps are in ms.
    now_ms = int(time.time() * 1000)
    candles = [
        c for c in candles
        if not (
            c["time"] > 10**12
            and c["time"] + 15 * 60 * 1000 > now_ms
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
        return {
            "markets": data["markets"],
            "sent_signals": data.get("sent_signals", {}),
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
    prior_high = max(c["high"] for c in prior_candles)
    prior_low = min(c["low"] for c in prior_candles)

    average_volume = (
        sum(c["volume"] for c in prior_candles)
        / len(prior_candles)
    )

    current_volume = last["volume"]
    volume_ratio = (
        current_volume / average_volume
        if average_volume > 0 else 0.0
    )

    breakout_up = close > prior_high and volume_ratio >= 1.2
    breakout_down = close < prior_low and volume_ratio >= 1.2

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
        or (abs(change_15m) >= 0.6 and volume_ratio >= 1.1)
        or (abs(change_1h) >= 1.5 and volume_ratio >= 1.1)
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
            abs(change_15m) >= 1.0
            and volume_ratio >= 1.4
        )
        or (
            abs(change_1h) >= 2.0
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
        "score": score,
    }


# ==================== TELEGRAM ====================

def send_telegram(message):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        raise RuntimeError(
            "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID"
        )

    response = session.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
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


def format_candidate(item, index):
    return (
        f"{index}. {item['symbol']} — {item['strength']}\n"
        f"{item['title']} | {item['direction']}\n"
        f"Цена: {format_price(item['price'])} USDT\n"
        f"15м: {item['change_15m']:+.2f}% | "
        f"1ч: {item['change_1h']:+.2f}% | "
        f"24ч: {item['change_24h']:+.2f}%\n"
        f"Объём: {item['volume_ratio']:.1f}x от среднего"
    )


# ==================== MAIN ====================

def main():
    now = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")

    snapshot = load_snapshot()
    sent_signals = snapshot.get("sent_signals", {})

    markets = fetch_markets()

    if not markets:
        print("BingX returned no markets.")
        return

    # -------- TEMPORARY DIAGNOSTICS --------
    print("DEBUG: First market ticker:", markets[0])

    try:
        test_candles = fetch_candles(markets[0]["symbol"])
        print("DEBUG symbol:", markets[0]["symbol"])
        print("DEBUG candle count:", len(test_candles))

        if test_candles:
            print("DEBUG latest candle:", test_candles[-1])
            if len(test_candles) >= 2:
                print("DEBUG previous candle:", test_candles[-2])
        else:
            print("DEBUG: No usable candles returned.")
    except Exception as exc:
        print("DEBUG candle request failed:", repr(exc))
    # -------- END DIAGNOSTICS --------

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

    print("DEBUG total qualifying candidates:", len(candidates))

    if selected:
        lines = [
            "BINGX — РЫНОЧНЫЙ РАДАР",
            f"Время: {now}",
            f"Монет проверено: {len(markets)}",
            f"За 24ч: рост {rising} | падение {falling}",
            f"Интересных кандидатов: {len(selected)}",
            "",
        ]

        for index, item in enumerate(selected, start=1):
            lines.append(format_candidate(item, index))
            lines.append("")

        lines.extend([
            "СИЛЬНЫЙ — больше подтверждений движения.",
            "НАБЛЮДЕНИЕ — кандидат слабее, нужна проверка.",
            "Сигналы не гарантируют прибыль и не являются командой на вход.",
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
