
import json
import os
import time
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode

TICKER_URL = (
    "https://open-api.bingx.com/openApi/swap/v2/quote/ticker"
)
KLINE_URL = (
    "https://open-api.bingx.com/openApi/swap/v3/quote/klines"
)

SNAPSHOT_FILE = Path("market_snapshot.json")

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# Начальные фильтры для тестирования, не торговые рекомендации.
IMPULSE_15M = 1.5
IMPULSE_1H = 1.0
BREAKOUT_VOLUME_RATIO = 1.3
REVERSAL_1H = 3.5
REVERSAL_15M = 1.0
VOLUME_CONFIRM_RATIO = 1.2

COOLDOWN_MS = 60 * 60 * 1000
REQUEST_PAUSE = 0.15


def request_json(url, retries=2):
    last_error = None

    for attempt in range(retries + 1):
        try:
            request = urllib.request.Request(
                url,
                headers={"User-Agent": "MarketMonitor/1.0"},
            )
            with urllib.request.urlopen(request, timeout=20) as response:
                result = json.loads(response.read().decode("utf-8"))

            if result.get("code") != 0:
                raise RuntimeError(
                    f"API error: {result.get('msg', 'unknown error')}"
                )

            return result

        except Exception as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))

    raise RuntimeError(str(last_error))


def fetch_markets():
    result = request_json(TICKER_URL)
    data = result.get("data", [])

    if isinstance(data, dict):
        data = [data]

    markets = []

    for item in data:
        symbol = item.get("symbol", "")

        if not symbol.endswith("-USDT"):
            continue

        base = symbol[:-5]
        if not base.isalnum() or base.startswith("NC"):
            continue

        try:
            price = float(item.get("lastPrice") or 0)
            change = float(item.get("priceChangePercent") or 0)
            volume = float(item.get("quoteVolume") or 0)
        except (TypeError, ValueError):
            continue

        if price <= 0:
            continue

        markets.append({
            "symbol": symbol,
            "price": price,
            "change": change,
            "volume": volume,
        })

    markets.sort(key=lambda item: item["volume"], reverse=True)
    return markets[:100]


def fetch_klines(symbol):
    params = urlencode({
        "symbol": symbol,
        "interval": "15m",
        "limit": 30,
    })

    result = request_json(f"{KLINE_URL}?{params}")
    now_ms = int(time.time() * 1000)
    candles = []

    for row in result.get("data", []):
        try:
            if len(row) < 8:
                continue

            candle = {
                "time": int(row[0]),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "close_time": int(row[6]),
                "quote_volume": float(row[7]),
            }

            if (
                candle["close_time"] < now_ms
                and candle["close"] > 0
            ):
                candles.append(candle)

        except (TypeError, ValueError, IndexError):
            continue

    candles.sort(key=lambda candle: candle["time"])
    return candles


def load_snapshot():
    try:
        data = json.loads(
            SNAPSHOT_FILE.read_text(encoding="utf-8")
        )
    except (FileNotFoundError, json.JSONDecodeError):
        return {"markets": {}, "sent_signals": {}}

    # Совместимость со старым форматом snapshot.
    if "markets" in data and isinstance(data["markets"], dict):
        return {
            "markets": data["markets"],
            "sent_signals": data.get("sent_signals", {}),
        }

    return {"markets": data, "sent_signals": {}}


def save_snapshot(markets, sent_signals):
    snapshot = {
        "markets": {
            market["symbol"]: {
                "price": market["price"],
                "volume": market["volume"],
            }
            for market in markets
        },
        "sent_signals": sent_signals,
    }

    SNAPSHOT_FILE.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def fmt_price(value):
    number = Decimal(str(value))

    if abs(number) >= 1000:
        return f"{number:,.2f}"

    if abs(number) >= 1:
        return f"{number:.4f}".rstrip("0").rstrip(".")

    return f"{number:.10f}".rstrip("0").rstrip(".")


def fmt_volume(value):
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.2f}K"

    return f"{value:.2f}"


def send_telegram(message):
    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_TOKEN}/sendMessage"
    )

    payload = json.dumps({
        "chat_id": CHAT_ID,
        "text": message,
        "disable_web_page_preview": True,
    }).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=20) as response:
        result = json.loads(response.read().decode("utf-8"))

    if not result.get("ok"):
        raise RuntimeError("Telegram did not accept the message")


def send_chunks(lines, limit=3500):
    chunks = []
    current = ""

    for line in lines:
        addition = line + "\n"

        if len(current) + len(addition) > limit:
            if current:
                chunks.append(current)
            current = ""

        current += addition

    if current:
        chunks.append(current)

    for chunk in chunks:
        send_telegram(chunk)


def detect_signal(symbol, candles):
    # Для анализа нужны последняя закрытая свеча и
    # минимум 20 предыдущих свечей.
    if len(candles) < 22:
        return None

    last = candles[-1]
    previous = candles[-2]
    hour_ago = candles[-5]

    if min(previous["close"], hour_ago["close"]) <= 0:
        return None

    change_15m = (
        last["close"] / previous["close"] - 1
    ) * 100

    change_1h = (
        last["close"] / hour_ago["close"] - 1
    ) * 100

    prior_20 = candles[-21:-1]
    prior_high = max(candle["high"] for candle in prior_20)
    prior_low = min(candle["low"] for candle in prior_20)

    average_volume = sum(
        candle["quote_volume"] for candle in prior_20
    ) / len(prior_20)

    volume_ratio = (
        last["quote_volume"] / average_volume
        if average_volume > 0
        else 0
    )

    # Приоритет 1: пробой диапазона 20 свечей.
    if (
        last["close"] > prior_high
        and volume_ratio >= BREAKOUT_VOLUME_RATIO
    ):
        return {
            "type": "breakout_up",
            "title": "ПРОБОЙ ВВЕРХ",
            "symbol": symbol,
            "price": last["close"],
            "change_15m": change_15m,
            "change_1h": change_1h,
            "volume_ratio": volume_ratio,
            "candle_time": last["time"],
        }

    if (
        last["close"] < prior_low
        and volume_ratio >= BREAKOUT_VOLUME_RATIO
    ):
        return {
            "type": "breakout_down",
            "title": "ПРОБОЙ ВНИЗ",
            "symbol": symbol,
            "price": last["close"],
            "change_15m": change_15m,
            "change_1h": change_1h,
            "volume_ratio": volume_ratio,
            "candle_time": last["time"],
        }

    # Приоритет 2: импульс в ту же сторону на 15m и 1h.
    if (
        change_15m >= IMPULSE_15M
        and change_1h >= IMPULSE_1H
        and volume_ratio >= VOLUME_CONFIRM_RATIO
    ):
        return {
            "type": "pump",
            "title": "ВОСХОДЯЩИЙ ИМПУЛЬС",
            "symbol": symbol,
            "price": last["close"],
            "change_15m": change_15m,
            "change_1h": change_1h,
            "volume_ratio": volume_ratio,
            "candle_time": last["time"],
        }

    if (
        change_15m <= -IMPULSE_15M
        and change_1h <= -IMPULSE_1H
        and volume_ratio >= VOLUME_CONFIRM_RATIO
    ):
        return {
            "type": "dump",
            "title": "НИСХОДЯЩИЙ ИМПУЛЬС",
            "symbol": symbol,
            "price": last["close"],
            "change_15m": change_15m,
            "change_1h": change_1h,
            "volume_ratio": volume_ratio,
            "candle_time": last["time"],
        }

    # Приоритет 3: движение за час и свеча в обратную сторону.
    if (
        change_1h <= -REVERSAL_1H
        and change_15m >= REVERSAL_15M
        and volume_ratio >= VOLUME_CONFIRM_RATIO
    ):
        return {
            "type": "reversal_up",
            "title": "ВОЗМОЖНЫЙ ОТСКОК",
            "symbol": symbol,
            "price": last["close"],
            "change_15m": change_15m,
            "change_1h": change_1h,
            "volume_ratio": volume_ratio,
            "candle_time": last["time"],
        }

    if (
        change_1h >= REVERSAL_1H
        and change_15m <= -REVERSAL_15M
        and volume_ratio >= VOLUME_CONFIRM_RATIO
    ):
        return {
            "type": "reversal_down",
            "title": "ВОЗМОЖНЫЙ ОТКАТ",
            "symbol": symbol,
            "price": last["close"],
            "change_15m": change_15m,
            "change_1h": change_1h,
            "volume_ratio": volume_ratio,
            "candle_time": last["time"],
        }

    return None


def format_signal(signal):
    return [
        f"⚠️ {signal['title']}",
        signal["symbol"],
        f"Цена закрытия: {fmt_price(signal['price'])}",
        f"Изменение за 15м: {signal['change_15m']:+.2f}%",
        f"Изменение за 1ч: {signal['change_1h']:+.2f}%",
        f"Объём к среднему: {signal['volume_ratio']:.2f}x",
        "Сигнал по закрытым свечам; проверь график.",
        "",
    ]


def main():
    markets = fetch_markets()

    if not markets:
        raise RuntimeError("BingX returned no markets")

    previous_data = load_snapshot()
    previous = previous_data["markets"]
    sent_signals = previous_data["sent_signals"]

    now = datetime.now(timezone.utc).strftime(
        "%d.%m.%Y %H:%M UTC"
    )

    rising = sum(1 for market in markets if market["change"] > 0)
    falling = sum(1 for market in markets if market["change"] < 0)
    unchanged = len(markets) - rising - falling

    lines = [
        "BingX Futures — TOP 100 CRYPTO",
        f"Обновлено: {now}",
        "Сортировка: оборот за 24 часа",
        "",
        "Сводка рынка:",
        f"Растут: {rising} | Падают: {falling} | Без изменений: {unchanged}",
        "",
        "Топ-5 роста за 24ч:",
    ]

    for market in sorted(
        markets, key=lambda item: item["change"], reverse=True
    )[:5]:
        lines.append(
            f"{market['symbol']}: {market['change']:+.2f}%"
        )

    lines.extend(["", "Топ-5 падения за 24ч:"])

    for market in sorted(
        markets, key=lambda item: item["change"]
    )[:5]:
        lines.append(
            f"{market['symbol']}: {market['change']:+.2f}%"
        )

    if previous:
        changes = []

        for market in markets:
            old = previous.get(market["symbol"])
            if not old:
                continue

            old_price = float(old.get("price") or 0)
            if old_price <= 0:
                continue

            change = (market["price"] / old_price - 1) * 100
            changes.append((market, change))

        lines.extend([
            "",
            "Изменение цены с прошлого снимка:",
            "(интервал зависит от времени успешных запусков)",
            "",
            "Сильнейший рост:",
        ])

        for market, change in sorted(
            changes, key=lambda item: item[1], reverse=True
        )[:5]:
            lines.append(f"{market['symbol']}: {change:+.2f}%")

        lines.append("Сильнейшее падение:")

        for market, change in sorted(
            changes, key=lambda item: item[1]
        )[:5]:
            lines.append(f"{market['symbol']}: {change:+.2f}%")

    else:
        lines.extend([
            "",
            "История пока отсутствует.",
            "Следующий запуск добавит сравнение.",
        ])

    lines.extend(["", "Рынки по обороту:", ""])

    for rank, market in enumerate(markets, start=1):
        lines.append(
            f"{rank}. {market['symbol']} | "
            f"Цена: {fmt_price(market['price'])} | "
            f"24ч: {market['change']:+.2f}% | "
            f"Оборот: ${fmt_volume(market['volume'])}"
        )

    # Сначала отправляем привычный отчёт TOP 100.
    send_chunks(lines)

    # Затем ищем события по 15-минутным свечам.
    candidates = []
    errors = 0

    for market in markets:
        try:
            candles = fetch_klines(market["symbol"])
            signal = detect_signal(market["symbol"], candles)

            if signal:
                key = f"{signal['symbol']}|{signal['type']}"
                last_sent = int(sent_signals.get(key, 0) or 0)
                now_ms = int(time.time() * 1000)

                if now_ms - last_sent >= COOLDOWN_MS:
                    candidates.append(signal)

        except Exception as exc:
            errors += 1
            print(f"Skipped {market['symbol']}: {exc}")

        time.sleep(REQUEST_PAUSE)

    # Ограничиваем число уведомлений за один цикл.
    candidates = candidates[:15]

    if candidates:
        signal_lines = [
            "BingX — ОБНАРУЖЕНЫ СИГНАЛЫ",
            f"Время: {now}",
            "Это фильтры для наблюдения, не команды на вход.",
            "",
        ]

        for signal in candidates:
            signal_lines.extend(format_signal(signal))

        send_chunks(signal_lines)

        now_ms = int(time.time() * 1000)

        for signal in candidates:
            key = f"{signal['symbol']}|{signal['type']}"
            sent_signals[key] = now_ms

    else:
        print("No new signals.")

    # Сохраняем снимок и время отправленных сигналов.
    save_snapshot(markets, sent_signals)

    print(
        f"Report sent; markets={len(markets)}; "
        f"signals={len(candidates)}; candle_errors={errors}"
    )


if __name__ == "__main__":
    main()
