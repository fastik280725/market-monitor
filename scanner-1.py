import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE_URL = "https://open-api.bingx.com"
TICKER_URL = f"{BASE_URL}/openApi/swap/v2/quote/ticker"
KLINES_URL = f"{BASE_URL}/openApi/swap/v3/quote/klines"
FUNDING_URL = f"{BASE_URL}/openApi/swap/v2/quote/premiumIndex"
OPEN_INTEREST_URL = f"{BASE_URL}/openApi/swap/v2/quote/openInterest"
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
SNAPSHOT_FILE = Path("market_snapshot.json")
TOP_MARKETS = 100
MAX_CANDIDATES = 5
INTERVAL = "15m"
INTERVAL_MS = 900000
CANDLE_LIMIT = 30
COOLDOWN_SECONDS = 3600
REQUEST_TIMEOUT = 25
GEMINI_TIMEOUT = 90
REQUEST_PAUSE = 0.12
RETRY_INITIAL_SECONDS = 5
RETRY_MAX_SECONDS = 60
TELEGRAM_MAX_LENGTH = 3500
session = requests.Session()


def number(value, default=0.0):
    try:
        result = float(value)
        return result if result == result and abs(result) != float("inf") else default
    except (TypeError, ValueError, OverflowError):
        return default


def api_get(url, params=None):
    last_error = None
    for attempt in range(3):
        try:
            response = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            result = response.json()
            if isinstance(result, dict) and result.get("code", 0) not in (0, "0", None):
                raise RuntimeError(f"BingX API: {result.get('code')} {result.get('msg', '')}")
            return result
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(attempt + 1)
    raise RuntimeError(f"Request failed: {last_error}")


def fetch_markets():
    data = api_get(TICKER_URL).get("data", [])
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
        volume = number(item.get("quoteVolume"))
        if volume <= 0:
            volume = number(item.get("volume")) * price
        if price <= 0:
            continue
        markets.append({
            "symbol": symbol,
            "price": price,
            "volume": volume,
            "change_24h": number(item.get("priceChangePercent")),
        })
    markets.sort(key=lambda m: m["volume"], reverse=True)
    return markets[:TOP_MARKETS]


def parse_candle(row):
    if isinstance(row, dict):
        timestamp = row.get("time", row.get("timestamp", row.get("openTime", 0)))
        o, h, l, c = (number(row.get(k)) for k in ("open", "high", "low", "close"))
        quote_volume = number(row.get("quoteVolume", row.get("quote_volume", 0)))
        volume = quote_volume if quote_volume > 0 else number(row.get("volume", row.get("vol", 0)))
        volume_type = "quote" if quote_volume > 0 else "base"
    elif isinstance(row, (list, tuple)) and len(row) >= 6:
        timestamp = row[0]
        o, h, l, c = (number(row[i]) for i in range(1, 5))
        quote_volume = number(row[7]) if len(row) >= 8 else 0
        volume = quote_volume if quote_volume > 0 else number(row[5])
        volume_type = "quote" if quote_volume > 0 else "base"
    else:
        return None
    timestamp = int(number(timestamp))
    if timestamp <= 0 or min(o, h, l, c) <= 0 or volume < 0:
        return None
    return {
        "time": timestamp, "open": o, "high": h, "low": l,
        "close": c, "volume": volume, "volume_type": volume_type,
    }


def fetch_candles(symbol):
    rows = api_get(KLINES_URL, {"symbol": symbol, "interval": INTERVAL, "limit": CANDLE_LIMIT}).get("data", [])
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
    candles = sorted({c["time"]: c for c in candles}.values(), key=lambda c: c["time"])
    now_ms = int(time.time() * 1000)
    return [c for c in candles if c["time"] < 10**12 or c["time"] + INTERVAL_MS <= now_ms]


def fetch_optional_metrics(symbol):
    metrics = {"funding_rate": None, "open_interest": None}
    try:
        data = api_get(FUNDING_URL, {"symbol": symbol}).get("data", {})
        if isinstance(data, list):
            data = data[0] if data else {}
        if isinstance(data, dict):
            for key in ("lastFundingRate", "fundingRate"):
                if data.get(key) is not None:
                    metrics["funding_rate"] = number(data[key])
                    break
    except Exception as exc:
        print(f"Funding unavailable for {symbol}: {exc}")
    try:
        data = api_get(OPEN_INTEREST_URL, {"symbol": symbol}).get("data", {})
        if isinstance(data, list):
            data = data[0] if data else {}
        if isinstance(data, dict):
            for key in ("openInterest", "openInterestAmount", "amount"):
                if data.get(key) is not None:
                    metrics["open_interest"] = number(data[key])
                    break
    except Exception as exc:
        print(f"Open interest unavailable for {symbol}: {exc}")
    return metrics


def load_snapshot():
    try:
        data = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    markets = data.get("markets", {})
    sent = data.get("sent_signals", {})
    return {
        "markets": markets if isinstance(markets, dict) else {},
        "sent_signals": sent if isinstance(sent, dict) else {},
    }


def save_snapshot(markets, sent_signals):
    SNAPSHOT_FILE.write_text(json.dumps({
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "markets": markets,
        "sent_signals": sent_signals,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def analyze_market(market, candles):
    if len(candles) < 22:
        return None
    last, previous, hour_ago = candles[-1], candles[-2], candles[-5]
    close, prev_close, hour_close = last["close"], previous["close"], hour_ago["close"]
    if min(close, prev_close, hour_close) <= 0:
        return None
    change_15m = (close / prev_close - 1) * 100
    change_1h = (close / hour_close - 1) * 100
    change_24h = market["change_24h"]
    prior = candles[-21:-1]
    prior_high = max(c["high"] for c in prior)
    prior_low = min(c["low"] for c in prior)
    consistent = len({c["volume_type"] for c in candles[-21:]}) == 1
    avg_volume = sum(c["volume"] for c in prior) / len(prior)
    volume_valid = consistent and avg_volume > 0 and last["volume"] > 0
    volume_ratio = last["volume"] / avg_volume if volume_valid else 0.0
    breakout_up = volume_valid and close > prior_high and volume_ratio >= 1.2
    breakout_down = volume_valid and close < prior_low and volume_ratio >= 1.2
    score = (abs(change_15m) * 1.6 + abs(change_1h) * 0.8
             + min(volume_ratio, 5.0) * 1.2 + min(abs(change_24h), 20.0) * 0.12)
    if breakout_up or breakout_down:
        score += 4.0
    if change_15m * change_1h > 0:
        score += 0.8
    interesting = (score >= 2.7 or
                   (volume_valid and abs(change_15m) >= 0.6 and volume_ratio >= 1.1) or
                   (volume_valid and abs(change_1h) >= 1.5 and volume_ratio >= 1.1) or
                   breakout_up or breakout_down)
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
    strong = (breakout_up or breakout_down or
              (volume_valid and abs(change_15m) >= 1.0 and volume_ratio >= 1.4) or
              (volume_valid and abs(change_1h) >= 2.0 and volume_ratio >= 1.3
               and change_15m * change_1h > 0))
    return {
        "symbol": market["symbol"], "price": close,
        "direction": direction, "title": title,
        "strength": "СИЛЬНЫЙ" if strong else "НАБЛЮДЕНИЕ",
        "change_15m": change_15m, "change_1h": change_1h,
        "change_24h": change_24h, "volume_ratio": volume_ratio,
        "volume_valid": volume_valid, "score": score,
        "prior_high": prior_high, "prior_low": prior_low,
        "candles": candles,
    }


ANALYSIS_PROMPT = """
Ты анализируешь USDT-M perpetual фьючерсы BingX. Получаешь до пяти кандидатов.
ВНУТРЕННЕ сравни ВСЕ рынки по структуре свечей, импульсу, объёму,
риску запоздалого входа, funding и open interest (если предоставлены).
ВЫВЕДИ ТОЛЬКО ОДНУ лучшую обоснованную сделку. Никаких сравнений других
монет и никаких дополнительных абзацев. Если хорошей сделки нет, верни NO_TRADE.
Не выдумывай стакан, ликвидации, дельту, действия маркет-мейкеров и отсутствующие данные.
Не выдавай предварительный score скрипта за гарантию. Не обещай прибыль.

Ответ ТОЛЬКО в виде валидного JSON-объекта с полями:
{
  "status": "TRADE" или "NO_TRADE",
  "symbol": "точный символ из входных данных или null",
  "direction": "LONG" или "SHORT" или null,
  "entry_type": "MARKET" или "LIMIT" или null,
  "entry_price": "ОДНА конкретная цена числом в строке или null",
  "stop_loss": "одна конкретная цена числом в строке или null",
  "take_profit": "одна конкретная цена числом в строке или null",
  "reason": "одно короткое предложение по-русски"
}

Для TRADE все цены должны быть обоснованы доступными свечами.
MARKET допускается только если вход по последней известной цене свечи
имеет смысл и не требует подтверждения. LIMIT — конкретная планируемая цена.
Если для входа нужно подтверждение, которое ещё не получено, или нельзя
обоснованно задать вход, стоп и цель, верни NO_TRADE.
Для LONG должно быть stop_loss < entry_price < take_profit.
Для SHORT должно быть take_profit < entry_price < stop_loss.
Если невозможно обосновать соотношение потенциальной прибыли к риску,
верни NO_TRADE. Не придумывай уровни ради заполнения полей.
Не добавляй markdown, пояснения за пределами JSON и другие ключи.
"""


def build_candidate_data(candidate, metrics):
    candles = candidate["candles"][-22:]
    return {
        "symbol": candidate["symbol"],
        "last_closed_price": candidate["price"],
        "preliminary_direction": candidate["direction"],
        "preliminary_label": candidate["title"],
        "preliminary_strength": candidate["strength"],
        "preliminary_score": round(candidate["score"], 2),
        "change_15m_pct": round(candidate["change_15m"], 3),
        "change_1h_pct": round(candidate["change_1h"], 3),
        "change_24h_pct": round(candidate["change_24h"], 3),
        "volume_ratio": round(candidate["volume_ratio"], 3) if candidate["volume_valid"] else None,
        "prior_20_candle_high": candidate["prior_high"],
        "prior_20_candle_low": candidate["prior_low"],
        "funding_rate": metrics["funding_rate"],
        "open_interest": metrics["open_interest"],
        "candles_15m_utc": [{
            "time": datetime.fromtimestamp(
                c["time"] / 1000 if c["time"] >= 10**12 else c["time"],
                tz=timezone.utc,
            ).isoformat(),
            "open": c["open"], "high": c["high"], "low": c["low"],
            "close": c["close"], "volume": c["volume"],
            "volume_type": c["volume_type"],
        } for c in candles],
        "unavailable_data": ["order book", "liquidations", "trade delta", "full volume profile"],
    }


def is_retryable_status(status_code, error_text=""):
    if status_code in (408, 429, 500, 502, 503, 504):
        return True
    text = error_text.lower()
    return any(s in text for s in (
        "overloaded", "high demand", "temporarily unavailable",
        "resource_exhausted", "rate limit", "too many requests",
        "try again later", "service unavailable", "internal error",
    ))


def gemini_analyze_many(candidates, metrics_by_symbol):
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("Не задан секрет GEMINI_API_KEY")
    data = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "timeframe": INTERVAL,
        "candidates": [build_candidate_data(c, metrics_by_symbol[c["symbol"]]) for c in candidates],
    }
    payload = {
        "system_instruction": {"parts": [{"text": ANALYSIS_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": json.dumps(data, ensure_ascii=False)}]}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 1200,
            "responseMimeType": "application/json",
        },
    }
    delay = RETRY_INITIAL_SECONDS
    attempt = 0
    while True:
        attempt += 1
        print(f"Gemini combined analysis attempt {attempt} for {len(candidates)} candidates")
        try:
            response = session.post(
                GEMINI_URL,
                headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
                json=payload,
                timeout=GEMINI_TIMEOUT,
            )
        except requests.RequestException as exc:
            print(f"Gemini network error: {exc}; retry in {delay}s")
            time.sleep(delay)
            delay = min(delay * 2, RETRY_MAX_SECONDS)
            continue
        try:
            result = response.json()
        except ValueError:
            result = {"error": {"message": response.text[:1000]}}
        if not response.ok:
            error = result.get("error", {})
            error_message = error.get("message", str(result)) if isinstance(error, dict) else str(error)
            if is_retryable_status(response.status_code, error_message):
                retry_after = response.headers.get("Retry-After")
                try:
                    wait_seconds = max(delay, float(retry_after or 0))
                except ValueError:
                    wait_seconds = delay
                wait_seconds = min(wait_seconds, RETRY_MAX_SECONDS)
                print(f"Temporary Gemini HTTP {response.status_code}: {error_message[:200]}; retry in {wait_seconds}s")
                time.sleep(wait_seconds)
                delay = min(delay * 2, RETRY_MAX_SECONDS)
                continue
            raise RuntimeError(f"Gemini HTTP {response.status_code}: {error_message[:700]}")
        try:
            parts = result["candidates"][0]["content"]["parts"]
            raw = "\n".join(part["text"] for part in parts if "text" in part).strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            analysis = json.loads(raw)
            if not isinstance(analysis, dict):
                raise ValueError("JSON is not an object")
            return analysis
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Gemini вернул некорректный JSON: {exc}") from exc


def valid_price(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        price = float(value)
        if price > 0 and price == price and price != float("inf"):
            return price
    except (TypeError, ValueError, OverflowError):
        pass
    return None


def format_price(price):
    if price >= 1000:
        return f"{price:.2f}"
    if price >= 1:
        return f"{price:.4f}".rstrip("0").rstrip(".")
    if price >= 0.01:
        return f"{price:.6f}".rstrip("0").rstrip(".")
    return f"{price:.10f}".rstrip("0").rstrip(".")


def render_report(analysis, selected, started):
    time_label = started.strftime("%d.%m.%Y %H:%M UTC")
    status = str(analysis.get("status", "")).upper()
    reason = str(analysis.get("reason") or "Недостаточно подтверждений для обоснованного входа.").strip()[:200]
    if status == "NO_TRADE":
        return f"🧠 BINGX | {time_label}\n⛔ НЕТ СДЕЛКИ\n💡 Причина: {reason}"
    if status != "TRADE":
        raise RuntimeError(f"Неизвестный статус Gemini: {status}")
    symbol = str(analysis.get("symbol", ""))
    direction = str(analysis.get("direction", "")).upper()
    entry_type = str(analysis.get("entry_type", "")).upper()
    if symbol not in {c["symbol"] for c in selected}:
        raise RuntimeError("Gemini выбрал монету не из переданных кандидатов")
    if direction not in ("LONG", "SHORT") or entry_type not in ("MARKET", "LIMIT"):
        raise RuntimeError("Gemini вернул неверное направление или тип входа")
    entry = valid_price(analysis.get("entry_price"))
    stop = valid_price(analysis.get("stop_loss"))
    target = valid_price(analysis.get("take_profit"))
    if entry is None or stop is None or target is None:
        raise RuntimeError("Gemini не указал корректные цены входа, SL и TP")
    if direction == "LONG":
        risk, reward = entry - stop, target - entry
    else:
        risk, reward = stop - entry, entry - target
    if risk <= 0 or reward <= 0:
        raise RuntimeError("Gemini указал нелогичные уровни SL/TP")
    ratio = reward / risk
    entry_label = "РЫНОК" if entry_type == "MARKET" else "ЛИМИТКА"
    return (
        f"🏆 BINGX | ЛУЧШАЯ СДЕЛКА\n"
        f"🕒 {time_label}\n\n"
        f"🪙 Монета: {symbol}\n"
        f"📈 Направление: {direction}\n"
        f"💵 Вход: {format_price(entry)} ({entry_label})\n"
        f"🛑 Stop Loss: {format_price(stop)}\n"
        f"🎯 Take Profit: {format_price(target)}\n"
        f"⚖️ SL:TP (риск/прибыль): 1:{ratio:.2f}\n\n"
        f"💡 {reason}\n"
        f"⚠️ План основан на закрытых свечах; перед входом проверь актуальную цену."
    )


def split_telegram_message(message, max_length=TELEGRAM_MAX_LENGTH):
    chunks = []
    while message:
        if len(message) <= max_length:
            chunks.append(message)
            break
        cut = message.rfind("\n", 0, max_length)
        if cut < max_length // 2:
            cut = max_length
        chunks.append(message[:cut])
        message = message[cut:].lstrip("\n")
    return chunks


def send_telegram(message):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
    for index, chunk in enumerate(split_telegram_message(message), start=1):
        response = session.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True},
            timeout=REQUEST_TIMEOUT,
        )
        try:
            result = response.json()
        except ValueError:
            result = {"description": response.text}
        if not response.ok or not result.get("ok"):
            raise RuntimeError(f"Telegram error part {index}: {result}")
        print(f"Telegram message part {index} sent")


def main():
    started = datetime.now(timezone.utc)
    print("BingX market scanner started:", started.isoformat())
    snapshot = load_snapshot()
    sent_signals = snapshot["sent_signals"]
    markets = fetch_markets()
    if not markets:
        raise RuntimeError("BingX returned no markets")
    print(f"Scanning {len(markets)} markets")
    candidates = []
    current_time = int(time.time())
    for index, market in enumerate(markets, start=1):
        symbol = market["symbol"]
        try:
            candidate = analyze_market(market, fetch_candles(symbol))
            if candidate:
                key = f"{symbol}:{candidate['direction']}:{candidate['strength']}"
                last_sent = int(sent_signals.get(key, 0) or 0)
                if current_time - last_sent >= COOLDOWN_SECONDS:
                    candidate["key"] = key
                    candidates.append(candidate)
        except Exception as exc:
            print(f"[{index}/{len(markets)}] {symbol}: {exc}")
        if index < len(markets):
            time.sleep(REQUEST_PAUSE)
    candidates.sort(key=lambda c: c["score"], reverse=True)
    selected = candidates[:MAX_CANDIDATES]
    print(f"Qualifying candidates: {len(candidates)}; sent to Gemini: {len(selected)}")
    if selected:
        metrics = {}
        for candidate in selected:
            metrics[candidate["symbol"]] = fetch_optional_metrics(candidate["symbol"])
        try:
            analysis = gemini_analyze_many(selected, metrics)
            report = render_report(analysis, selected, started)
            send_telegram(report)
            for candidate in selected:
                sent_signals[candidate["key"]] = current_time
        except Exception as exc:
            print(f"Combined analysis failed: {exc}")
            try:
                send_telegram(f"⚠️ Не удалось выполнить анализ BingX.\nПричина: {str(exc)[:500]}")
            except Exception as telegram_exc:
                print("Telegram error report failed:", telegram_exc)
    else:
        print("No interesting new candidates")
    market_snapshot = {
        m["symbol"]: {"price": m["price"], "volume": m["volume"], "change_24h": m["change_24h"]}
        for m in markets
    }
    save_snapshot(market_snapshot, sent_signals)
    print("Snapshot saved")


if __name__ == "__main__":
    main()
