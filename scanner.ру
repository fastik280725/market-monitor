
import json
import os
import urllib.request
from datetime import datetime, timezone

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

    # Только USDT-бессрочные контракты.
    markets = [
        item for item in data
        if item.get("symbol", "").endswith("-USDT")
    ]

    # Сортировка по суточному обороту в USDT.
    markets.sort(
        key=lambda item: float(item.get("quoteVolume") or 0),
        reverse=True,
    )

    return markets[:100]


def fmt_number(value):
    value = float(value or 0)
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.2f}K"
    return f"{value:.2f}"


def send_telegram(text):
    url = (
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
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

    now = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    lines = [
        f"📊 BingX Futures — TOP 100",
        f"Обновлено: {now}",
        "Сортировка: оборот за 24 часа",
        "",
    ]

    for rank, market in enumerate(markets, start=1):
        symbol = market.get("symbol", "?")
        price = fmt_number(market.get("lastPrice"))
        change = float(market.get("priceChangePercent") or 0)
        volume = fmt_number(market.get("quoteVolume"))

        lines.append(
            f"{rank}. {symbol} | Цена: {price} | "
            f"24ч: {change:+.2f}% | Оборот: ${volume}"
        )

    # Telegram ограничивает длину одного сообщения.
    # Разбиваем отчёт на несколько сообщений.
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

    print(f"Report sent: {len(markets)} markets")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Ошибка попадёт в журнал GitHub Actions.
        print(f"Scanner failed: {error}")
        raise
