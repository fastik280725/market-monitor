
import json
import os
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

BINGX_URL = "https://open-api.bingx.com/openApi/swap/v2/quote/ticker"
SNAPSHOT_FILE = Path("market_snapshot.json")

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]


def fetch_markets():
    request = urllib.request.Request(
        BINGX_URL,
        headers={"User-Agent": "MarketMonitor/1.0"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        result = json.loads(response.read().decode("utf-8"))

    if result.get("code") != 0:
        raise RuntimeError(f"BingX API error: {result.get('msg')}")

    data = result.get("data", [])
    if isinstance(data, dict):
        data = [data]

    excluded_prefixes = ("NC",)
    markets = []

    for item in data:
        symbol = item.get("symbol", "")
        if not symbol.endswith("-USDT"):
            continue

        base = symbol[:-5]
        if not base.isalnum() or base.startswith(excluded_prefixes):
            continue

        try:
            price = float(item.get("lastPrice") or 0)
            change = float(item.get("priceChangePercent") or 0)
            volume = float(item.get("quoteVolume") or 0)
        except (ValueError, TypeError):
            continue

        if price <= 0:
            continue

        markets.append({
            "symbol": symbol,
            "price": price,
            "change": change,
            "volume": volume,
        })

    markets.sort(key=lambda m: m["volume"], reverse=True)
    return markets[:100]


def load_snapshot():
    try:
        return json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_snapshot(markets):
    snapshot = {
        market["symbol"]: {
            "price": market["price"],
            "volume": market["volume"],
        }
        for market in markets
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
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
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


def main():
    markets = fetch_markets()
    if not markets:
        raise RuntimeError("No markets returned by BingX")

    previous = load_snapshot()
    now = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")

    changes = [(m, m["change"]) for m in markets]
    rising = sum(1 for _, change in changes if change > 0)
    falling = sum(1 for _, change in changes if change < 0)
    unchanged = len(changes) - rising - falling

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

    for market, change in sorted(changes, key=lambda x: x[1], reverse=True)[:5]:
        lines.append(f"{market['symbol']}: {change:+.2f}%")

    lines.extend(["", "Топ-5 падения за 24ч:"])
    for market, change in sorted(changes, key=lambda x: x[1])[:5]:
        lines.append(f"{market['symbol']}: {change:+.2f}%")

    if previous:
        hourly = []
        volume_moves = []

        for market in markets:
            old = previous.get(market["symbol"])
            if not old:
                continue

            old_price = float(old.get("price") or 0)
            old_volume = float(old.get("volume") or 0)

            if old_price > 0:
                price_change = (market["price"] / old_price - 1) * 100
                hourly.append((market, price_change))

            if old_volume > 0:
                volume_change = (market["volume"] / old_volume - 1) * 100
                volume_moves.append((market, volume_change))

        lines.extend([
            "",
            "Изменение цены с прошлого снимка:",
            "(примерно за час; учитываются совпавшие рынки)",
        ])

        lines.append("Сильнейший рост:")
        for market, change in sorted(hourly, key=lambda x: x[1], reverse=True)[:5]:
            lines.append(f"{market['symbol']}: {change:+.2f}%")

        lines.append("Сильнейшее падение:")
        for market, change in sorted(hourly, key=lambda x: x[1])[:5]:
            lines.append(f"{market['symbol']}: {change:+.2f}%")

        lines.extend([
            "",
            "Изменение 24ч оборота с прошлого снимка:",
            "(это изменение скользящего 24ч оборота, не чистый часовой объём)",
        ])

        for market, change in sorted(
            volume_moves, key=lambda x: x[1], reverse=True
        )[:5]:
            lines.append(f"{market['symbol']}: {change:+.2f}%")

    else:
        lines.extend([
            "",
            "История пока отсутствует.",
            "Следующий запуск добавит сравнение с этим снимком.",
        ])

    lines.extend(["", "Рынки по обороту:", ""])

    for rank, market in enumerate(markets, start=1):
        lines.append(
            f"{rank}. {market['symbol']} | "
            f"Цена: {fmt_price(market['price'])} | "
            f"24ч: {market['change']:+.2f}% | "
            f"Оборот: ${fmt_volume(market['volume'])}"
        )

    # Telegram ограничивает длину одного сообщения.
    chunks = []
    current = ""

    for line in lines:
        addition = line + "\n"
        if len(current) + len(addition) > 3500:
            chunks.append(current)
            current = ""
        current += addition

    if current:
        chunks.append(current)

    for chunk in chunks:
        send_telegram(chunk)

    # Сохраняем снимок только после успешной отправки отчёта.
    save_snapshot(markets)
    print(f"Report sent; markets={len(markets)}; previous_snapshot={bool(previous)}")


if __name__ == "__main__":
    main()
