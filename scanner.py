
import json
import os
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal

BINGX_URL = (
    "https://open-api.bingx.com"
    "/openApi/swap/v2/quote/ticker"
)

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]


def get_market_data():
    request = urllib.request.Request(
        BINGX_URL,
        headers={"User-Agent": "MarketMonitor/1.0"},
    )

    with urllib.request.urlopen(request, timeout=20) as response:
        result = json.loads(response.read().decode("utf-8"))

    if result.get("code") != 0:
        raise RuntimeError(
            f"BingX API error: {result.get('msg', 'unknown')}"
        )

    data = result.get("data", [])
    if isinstance(data, dict):
        data = [data]

    markets = []

    # Берём USDT-контракты и исключаем очевидные
    # валютные пары, акции, индексы и товары.
    excluded = (
        "NC", "NCCO", "NCFX", "NCSK",
    )

    for item in data:
        symbol = item.get("symbol", "")

        if not symbol.endswith("-USDT"):
            continue

        base = symbol[:-5]

        if base.startswith(excluded):
            continue

        # Только символы, состоящие из букв и цифр.
        if not base.isalnum():
            continue

        item["base"] = base
        markets.append(item)

    markets.sort(
        key=lambda item: float(item.get("quoteVolume") or 0),
        reverse=True,
    )

    return markets[:100]


def fmt_price(value):
    number = Decimal(str(value or "0"))

    if number == 0:
        return "0"

    if abs(number) >= 1000:
        return f"{number:,.2f}"

    if abs(number) >= 1:
        return f"{number:.4f}".rstrip("0").rstrip(".")

    # Для дешёвых активов сохраняем значащие цифры.
    return f"{number:.8f}".rstrip("0").rstrip(".")


def fmt_volume(value):
    number = float(value or 0)

    if number >= 1_000_000_000:
        return f"{number / 1_000_000_000:.2f}B"
    if number >= 1_000_000:
        return f"{number / 1_000_000:.2f}M"
    if number >= 1_000:
        return f"{number / 1_000:.2f}K"

    return f"{number:.2f}"


def send_telegram(text):
    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_TOKEN}/sendMessage"
    )

    payload = json.dumps({
        "chat_id": CHAT_ID,
        "text": text,
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
    markets = get_market_data()

    if not markets:
        raise RuntimeError("No markets returned by the filter")

    now = datetime.now(timezone.utc).strftime(
        "%d.%m.%Y %H:%M UTC"
    )

    changes = [
        (market, float(market.get("priceChangePercent") or 0))
        for market in markets
    ]

    rising = sum(1 for _, change in changes if change > 0)
    falling = sum(1 for _, change in changes if change < 0)
    unchanged = len(changes) - rising - falling

    leaders_up = sorted(
        changes, key=lambda item: item[1], reverse=True
    )[:5]

    leaders_down = sorted(
        changes, key=lambda item: item[1]
    )[:5]

    lines = [
        "📊 BingX Futures — TOP 100 CRYPTO",
        f"Обновлено: {now}",
        "Сортировка: оборот за 24 часа",
        "",
        "📈 Сводка рынка:",
        f"Растут: {rising} | Падают: {falling} | "
        f"Без изменений: {unchanged}",
        "",
        "🚀 Топ-5 роста за 24ч:",
    ]

    for market, change in leaders_up:
        lines.append(
            f"{market['symbol']}: {change:+.2f}%"
        )

    lines.extend(["", "🔻 Топ-5 падения за 24ч:"])

    for market, change in leaders_down:
        lines.append(
            f"{market['symbol']}: {change:+.2f}%"
        )

    lines.extend([
        "",
        "📋 Рынки по обороту:",
        "",
    ])

    for rank, market in enumerate(markets, start=1):
        symbol = market.get("symbol", "?")
        price = fmt_price(market.get("lastPrice"))
        change = float(
            market.get("priceChangePercent") or 0
        )
        volume = fmt_volume(market.get("quoteVolume"))

        lines.append(
            f"{rank}. {symbol} | Цена: {price} | "
            f"24ч: {change:+.2f}% | Оборот: ${volume}"
        )

    # Делим отчёт на части, чтобы уложиться в лимит Telegram.
    chunks = []
    current = ""

    for line in lines:
        if len(current) + len(line) + 1 > 3500:
            chunks.append(current)
            current = ""

        current += line + "\n"

    if current:
        chunks.append(current)

    for chunk in chunks:
        send_telegram(chunk)

    print(
        f"Report sent: {len(markets)} markets; "
        f"rising={rising}, falling={falling}"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Scanner failed: {error}")
        raise
