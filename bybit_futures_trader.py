#!/usr/bin/env python3
"""
Автоторговля по сигналам на Bybit USDT-перпетуалах.
Использует логику сигналов из bybit_futures_signal_bot.py (положите файлы рядом).

ГЛАВНОЕ ПРО БЕЗОПАСНОСТЬ (встроено в код):
  * Стоп и тейк ставятся ВМЕСТЕ с входом на стороне биржи (позиционные SL/TP), поэтому
    они работают, даже если бот или интернет упали.
  * После входа бот проверяет, что SL и TP реально появились. Если нет, позиция
    закрывается сразу, вы получаете сообщение.
  * По умолчанию режим DEMO (демо-счёт Bybit с виртуальными деньгами). Режим live
    включается только двумя переменными окружения.
  * Лимиты: макс. открытых позиций, дневной лимит убытка, общая просадка от пика,
    файл STOP (создайте его, и новые сделки прекращаются).

Установка:  pip install ccxt pandas numpy requests
Запуск (демо):
    BYBIT_KEY=... BYBIT_SECRET=... TG_TOKEN=... TG_CHAT_ID=... python bybit_futures_trader.py
Live (только после демо-периода):
    MODE=live LIVE_CONFIRM=YES-I-ACCEPT-RISK ... python bybit_futures_trader.py

Ключ API: права ТОЛЬКО «Контракты: торговля/чтение». Вывод средств НЕ включать.
Привяжите ключ к IP сервера. Маржу переведите в «изолированную» в приложении.
Это не финансовая рекомендация: вы можете потерять весь депозит.
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import bybit_futures_signal_bot as sb

MODE = os.getenv("MODE", "demo").lower()          # demo | testnet | live
CAPITAL_CAP = float(os.getenv("CAPITAL", "15"))   # торгуем не больше этой суммы
MAX_OPEN_POSITIONS = 2
DAILY_LOSS_LIMIT = 0.03    # −3% от баланса на начало дня: стоп на сутки
MAX_DRAWDOWN = 0.10        # −10% от пика: полная остановка до ручного сброса
MAX_SLIPPAGE_DIST = 0.30   # не входим, если цена ушла от сигнала более чем на 0.3 стопа
STATE_FILE = Path("trader_state.json")
STOP_FILE = Path("STOP")


# ---------------- БИРЖА ----------------
def make_exchange():
    import ccxt
    ex = ccxt.bybit({
        "apiKey": os.getenv("BYBIT_KEY"), "secret": os.getenv("BYBIT_SECRET"),
        "enableRateLimit": True, "options": {"defaultType": "swap"},
    })
    if MODE == "testnet":
        ex.set_sandbox_mode(True)
    elif MODE == "demo":
        ex.enable_demo_trading(True)
    elif MODE == "live":
        if os.getenv("LIVE_CONFIRM") != "YES-I-ACCEPT-RISK":
            sys.exit("Для live задайте LIVE_CONFIRM=YES-I-ACCEPT-RISK")
    else:
        sys.exit("MODE: demo | testnet | live")
    if not (os.getenv("BYBIT_KEY") and os.getenv("BYBIT_SECRET")):
        sys.exit("Нужны BYBIT_KEY и BYBIT_SECRET")
    return ex


def equity(ex):
    bal = ex.fetch_balance()
    return float((bal.get("USDT") or {}).get("total") or 0)


def open_positions(ex):
    out = {}
    for p in ex.fetch_positions():
        if float(p.get("contracts") or 0) > 0:
            out[p["symbol"]] = p
    return out


def close_market(ex, sym, qty, d):
    side = "sell" if d == 1 else "buy"
    return ex.create_order(sym, "market", side, qty, None, {"reduceOnly": True})


def verify_protection(ex, sym, tries=4):
    """Проверяет, что у позиции есть и стоп, и тейк. Возвращает (ok, position)."""
    pos = None
    for _ in range(tries):
        time.sleep(1)
        for p in ex.fetch_positions([sym]):
            if float(p.get("contracts") or 0) > 0:
                pos = p
                info = p.get("info") or {}
                if float(info.get("stopLoss") or 0) > 0 and float(info.get("takeProfit") or 0) > 0:
                    return True, p
    return False, pos


def open_trade(ex, sig):
    """Вход с SL/TP на бирже. Возвращает (dict | None, сообщение)."""
    sym, d = sig["symbol"], sig["d"]
    side = "buy" if d == 1 else "sell"
    last = float(ex.fetch_ticker(sym)["last"])
    if abs(last - sig["price"]) > MAX_SLIPPAGE_DIST * sig["dist"]:
        return None, f"цена ушла от сигнала ({last} vs {sig['price']:.6g}): пропуск"
    if (d == 1 and last <= sig["stop"]) or (d == -1 and last >= sig["stop"]):
        return None, "цена уже за стопом: пропуск"

    try:  # изолированная маржа и плечо; «не изменено» не считается ошибкой
        ex.set_margin_mode("isolated", sym, params={"leverage": sig["lev"]})
    except Exception as e:
        print(f"[{sym}] margin mode: {e}")
    try:
        ex.set_leverage(sig["lev"], sym)
    except Exception as e:
        print(f"[{sym}] leverage: {e}")

    qty = float(ex.amount_to_precision(sym, sig["qty"]))
    sl = ex.price_to_precision(sym, sig["stop"])
    tp = ex.price_to_precision(sym, sig["target"])
    params = {"stopLoss": {"triggerPrice": sl}, "takeProfit": {"triggerPrice": tp},
              "slTriggerBy": "MarkPrice", "tpTriggerBy": "LastPrice"}
    ex.create_order(sym, "market", side, qty, None, params)

    ok, pos = verify_protection(ex, sym)
    if not ok:
        try:
            if pos is not None:
                close_market(ex, sym, float(pos["contracts"]), d)
        except Exception as e:
            return None, f"🚨 SL/TP не подтверждены и закрыть не удалось ({e}): ПРОВЕРЬТЕ ПОЗИЦИЮ ВРУЧНУЮ"
        return None, "SL/TP не появились на бирже, позиция закрыта аварийно"
    entry = float(pos.get("entryPrice") or sig["price"])
    warn = ""
    if str(pos.get("marginMode", "")).lower() not in ("isolated", ""):
        warn = "⚠ маржа не изолированная: переключите в приложении Bybit"
    return {"qty": qty, "entry": entry, "stop": float(sl), "target": float(tp),
            "dist": sig["dist"], "d": d, "opened_ms": int(time.time() * 1000)}, warn


# ---------------- СОСТОЯНИЕ И ЛИМИТЫ ----------------
def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"last": {}, "open": {}, "closed": [], "day": "", "day_start": 0.0,
                "peak": 0.0, "halted": False, "notified_day": ""}


def save_state(st):
    STATE_FILE.write_text(json.dumps(st))


def risk_gate(st, eq):
    """Возвращает причину запрета новых сделок или None."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if st["day"] != today:
        st["day"], st["day_start"] = today, eq
    st["peak"] = max(st["peak"], eq)
    if STOP_FILE.exists():
        return "файл STOP"
    if st["halted"]:
        return "остановка по просадке (удалите trader_state.json после разбора)"
    if st["peak"] > 0 and eq < st["peak"] * (1 - MAX_DRAWDOWN):
        st["halted"] = True
        return f"просадка от пика более {MAX_DRAWDOWN:.0%}: бот остановлен"
    if st["day_start"] > 0 and eq < st["day_start"] * (1 - DAILY_LOSS_LIMIT):
        return f"дневной лимит убытка {DAILY_LOSS_LIMIT:.0%}: пауза до завтра"
    return None


def manage_open(ex, st, positions):
    """Закрытия по SL/TP фиксирует сама биржа; бот сообщает итог и режет по таймауту."""
    for sym in list(st["open"]):
        t = st["open"][sym]
        if sym not in positions:
            eq = equity(ex)
            delta = eq - t["equity_open"]
            r_est = delta / (t["qty"] * t["dist"]) if t["qty"] * t["dist"] else 0.0
            st["closed"].append(r_est)
            sb.tg(f"{'✅' if delta > 0 else '❌'} {sym} закрыта биржей (SL/TP)\n"
                  f"Изменение баланса {delta:+.3f} USDT (≈{r_est:+.2f}R)\nБаланс {eq:.2f} USDT\n"
                  + sb.stats_line(st["closed"]))
            del st["open"][sym]
        elif time.time() * 1000 - t["opened_ms"] > sb.MAX_HOLD * 3_600_000:
            try:
                close_market(ex, sym, float(positions[sym]["contracts"]), t["d"])
                sb.tg(f"⏱ {sym}: таймаут {sb.MAX_HOLD} свечей, позиция закрыта по рынку")
            except Exception as e:
                sb.tg(f"🚨 {sym}: не удалось закрыть по таймауту: {e}")


# ---------------- ОСНОВНОЙ ЦИКЛ ----------------
def main():
    ex = make_exchange()
    ex.load_markets()
    symbols = [s for s in sb.SYMBOLS if s in ex.markets]
    st = load_state()
    banner = (f"Автобот запущен. Режим: {MODE.upper()}, капитал ≤{CAPITAL_CAP} USDT, "
              f"риск {sb.RISK_PCT:.0%}, плечо ≤{sb.MAX_LEVERAGE}x, пары: {', '.join(symbols)}")
    sb.tg(banner)
    if MODE == "live":
        print("LIVE-режим: старт через 10 секунд, Ctrl+C для отмены")
        time.sleep(10)

    while True:
        try:
            eq = equity(ex)
            sb.CAPITAL = min(CAPITAL_CAP, eq)   # размер позиции считается от реального баланса
            positions = open_positions(ex)
            manage_open(ex, st, positions)
            gate = risk_gate(st, eq)
            if gate:
                today = st["day"]
                if st.get("notified_day") != today + gate:
                    sb.tg(f"⛔ Новые сделки отключены: {gate}")
                    st["notified_day"] = today + gate
                save_state(st)
                time.sleep(sb.SCAN_EVERY_SEC)
                continue

            try:
                btc = sb.trend_dir(sb.fetch_df(ex, sb.BTC_SYMBOL, sb.HTF))
            except Exception:
                btc = None

            for sym in symbols:
                if sym in positions or sym in st["open"] or len(positions) >= MAX_OPEN_POSITIONS:
                    continue
                try:
                    df = sb.fetch_df(ex, sym, sb.TF)
                    if int(df["ts"].iloc[-1]) - st["last"].get(sym, 0) < sb.COOLDOWN_BARS * 3_600_000:
                        continue
                    try:
                        funding = ex.fetch_funding_rate(sym).get("fundingRate")
                    except Exception:
                        funding = None
                    htf = sb.fetch_df(ex, sym, sb.HTF)
                    sig, reason = sb.evaluate(sym, df, htf, btc, funding, sb.market_info(ex, sym))
                    if not sig:
                        print(f"[{sym}] нет сигнала: {reason}")
                        continue
                    st["last"][sym] = sig["bar_ts"]
                    sb.tg(sb.signal_text(sig))
                    trade, msg = open_trade(ex, sig)
                    if trade is None:
                        sb.tg(f"⚠ {sym}: сделка не открыта. {msg}")
                    else:
                        trade["equity_open"] = eq
                        st["open"][sym] = trade
                        positions[sym] = {"contracts": trade["qty"]}
                        sb.tg(f"📌 {sym} {sig['side']} открыта: вход {trade['entry']:.6g}, "
                              f"SL {trade['stop']:.6g}, TP {trade['target']:.6g} (стоп и тейк на бирже) {msg}")
                    save_state(st)
                except Exception as e:
                    print(f"[{sym}] ошибка: {e}")
            save_state(st)
        except Exception as e:
            print("Цикл:", e)
        time.sleep(sb.SCAN_EVERY_SEC)


if __name__ == "__main__":
    main()
