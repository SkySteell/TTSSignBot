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
import threading
import time
import types
from datetime import datetime, timezone
from pathlib import Path

import bybit_futures_signal_bot as sb
import tg_ui

MODE = os.getenv("MODE", "demo").lower()          # demo | testnet | live
CAPITAL_CAP = float(os.getenv("CAPITAL", "150"))   # торгуем не больше этой суммы
MAX_OPEN_POSITIONS = 2
DAILY_LOSS_LIMIT = 0.03    # −3% от баланса на начало дня: стоп на сутки
MAX_DRAWDOWN = 0.10        # −10% от пика: полная остановка до ручного сброса
MAX_SLIPPAGE_DIST = 0.30   # не входим, если цена ушла от сигнала более чем на 0.3 стопа
STATE_FILE = Path(os.getenv("STATE_FILE", "trader_state.json"))
STOP_FILE = Path("STOP")
LOCK = threading.RLock()   # общий замок: цикл бота и кнопки Telegram не мешают друг другу
REASONS = {}               # последняя причина «нет входа» по каждой паре (для кнопки «Пары»)
META = {}                  # служебное: время последней проверки


# ---------------- БИРЖА ----------------
def make_exchange():
    import ccxt
    # В тестовом выпуске торговля на реальных средствах выключена безусловно.
    if MODE != "demo":
        raise RuntimeError("TTSSignBot: разрешён только MODE=demo; live/testnet отключены")
    if CAPITAL_CAP <= 0:
        raise RuntimeError("CAPITAL должен быть положительным")
    if not (os.getenv("BYBIT_KEY") and os.getenv("BYBIT_SECRET")):
        raise RuntimeError("Для DEMO нужны отдельные API-ключи демосчёта Bybit")
    unified = os.getenv("UNIFIED_ACCOUNT", "1") != "0"
    ex = ccxt.bybit({
        "apiKey": os.getenv("BYBIT_KEY"), "secret": os.getenv("BYBIT_SECRET"),
        "enableRateLimit": True,
        "timeout": 15000,
        "options": {"defaultType": "swap", "enableUnifiedMargin": False,
                    "enableUnifiedAccount": unified},
    })
    ex.enable_demo_trading(True)
    return ex


def equity(ex):
    """USDT equity, включая плавающий PnL, а не walletBalance/USDT.total."""
    bal = ex.fetch_balance({"type": "swap"})
    accounts = ((bal.get("info") or {}).get("result") or {}).get("list") or []
    for account in accounts:
        for coin in account.get("coin") or []:
            if coin.get("coin") == "USDT":
                value = coin.get("equity")
                if value not in (None, ""):
                    n = float(value)
                    if not (0 < n < float("inf")):
                        raise RuntimeError("Некорректный USDT equity")
                    return n
    raise RuntimeError("Bybit не вернул подтверждённый equity USDT: торговля заблокирована")


def open_positions(ex):
    """Только позиции one-way USDT linear; неоднозначные режимы запрещены."""
    out = {}
    for p in ex.fetch_positions():
        if float(p.get("contracts") or 0) <= 0:
            continue
        sym = p["symbol"]
        if sym in out:
            raise RuntimeError(f"{sym}: две позиции на одну пару (hedge не поддерживается)")
        idx = (p.get("info") or {}).get("positionIdx")
        if idx not in (None, "", 0, "0"):
            raise RuntimeError(f"{sym}: hedge-mode, positionIdx={idx}; торговля отключена")
        out[sym] = p
    return out


def close_market(ex, sym, qty, d):
    side = "sell" if d == 1 else "buy"
    return ex.create_order(sym, "market", side, qty, None, {"reduceOnly": True})


class UnsafeTradeError(RuntimeError):
    """Исход ордера или защита не подтверждены: требуется ручная проверка."""


def verify_protection(ex, sym, tries=5):
    """Проверить биржевые SL/TP, не полагаясь на ответ create_order."""
    latest = None
    for _ in range(tries):
        time.sleep(1)
        positions = ex.fetch_positions([sym])
        for p in positions:
            if p.get("symbol") != sym or float(p.get("contracts") or 0) <= 0:
                continue
            latest = p
            info = p.get("info") or {}
            sl = float(info.get("stopLoss") or 0)
            tp = float(info.get("takeProfit") or 0)
            if sl > 0 and tp > 0:
                return True, p
    return False, latest


def ensure_margin_and_leverage(ex, sym, leverage):
    """Не входить, если isolated и плечо не удалось подтвердить."""
    errors = []
    for operation in (
        lambda: ex.set_margin_mode("isolated", sym, params={"leverage": leverage}),
        lambda: ex.set_leverage(leverage, sym),
    ):
        try:
            operation()
        except Exception as exc:
            # Bybit может отвечать "not modified" на уже действующую настройку.
            errors.append(str(exc))
    ps = ex.fetch_positions([sym])
    for p in ps:
        if p.get("symbol") != sym:
            continue
        mode = str(p.get("marginMode") or "").lower()
        lev = float(p.get("leverage") or (p.get("info") or {}).get("leverage") or 0)
        if mode == "isolated" and abs(lev - leverage) < 0.01:
            return
    raise RuntimeError(
        f"Isolated margin / плечо {leverage}x не подтверждены Bybit. "
        + ("; ".join(errors)[:250] if errors else "Проверьте настройки демосчёта")
    )


def emergency_flatten(ex, sym, d):
    """Закрыть найденную позицию и дождаться фактического отсутствия на бирже."""
    ps = open_positions(ex)
    pos = ps.get(sym)
    if pos is not None:
        side = str(pos.get("side") or "").lower()
        if side != ("long" if d == 1 else "short"):
            raise UnsafeTradeError(f"{sym}: сторона позиции не совпадает, ручная проверка")
        close_market(ex, sym, float(pos["contracts"]), d)
    for _ in range(5):
        time.sleep(1)
        if sym not in open_positions(ex):
            return True
    raise UnsafeTradeError(f"{sym}: позиция всё ещё открыта после аварийного закрытия")


def open_trade(ex, sig):
    """Открыть сделку только в DEMO, подтвердить защиту на бирже."""
    sym, d = sig["symbol"], sig["d"]
    side = "buy" if d == 1 else "sell"
    last = float(ex.fetch_ticker(sym)["last"])
    if abs(last - sig["price"]) > MAX_SLIPPAGE_DIST * sig["dist"]:
        return None, f"цена ушла от сигнала ({last} vs {sig['price']:.6g}): пропуск"
    if (d == 1 and last <= sig["stop"]) or (d == -1 and last >= sig["stop"]):
        return None, "цена уже за стопом: пропуск"
    try:
        ensure_margin_and_leverage(ex, sym, sig["lev"])
    except Exception as exc:
        return None, f"настройки маржи/плеча: {exc}"

    qty = float(ex.amount_to_precision(sym, sig["qty"]))
    sl = ex.price_to_precision(sym, sig["stop"])
    tp = ex.price_to_precision(sym, sig["target"])
    if qty <= 0:
        return None, "объём после округления равен нулю"
    actual_dist = abs(last - float(sl))
    est_risk = qty * (actual_dist + last * sb.RT_COST)
    if est_risk > sb.CAPITAL * sb.RISK_PCT + 1e-8:
        return None, f"риск с учётом издержек {est_risk:.3f} USDT выше лимита"
    params = {"stopLoss": {"triggerPrice": sl}, "takeProfit": {"triggerPrice": tp},
              "slTriggerBy": "MarkPrice", "tpTriggerBy": "LastPrice"}
    try:
        order = ex.create_order(sym, "market", side, qty, None, params)
    except Exception as exc:
        # Timeout/5xx может означать, что ордер уже принят Bybit. Не повторять!
        raise UnsafeTradeError(
            f"{sym}: результат размещения неизвестен ({str(exc)[:250]}). "
            "Новые входы заблокированы. Проверьте позиции и SL/TP вручную"
        ) from exc
    try:
        ok, pos = verify_protection(ex, sym)
    except Exception as exc:
        try:
            emergency_flatten(ex, sym, d)
        except Exception as close_exc:
            raise UnsafeTradeError(
                f"{sym}: нет связи при проверке SL/TP; закрытие не подтверждено: {close_exc}"
            ) from exc
        raise UnsafeTradeError(f"{sym}: проверка защиты дала ошибку; позиция закрыта, требуется проверка") from exc
    if not ok:
        try:
            emergency_flatten(ex, sym, d)
        except Exception as exc:
            raise UnsafeTradeError(f"{sym}: SL/TP отсутствуют, закрытие НЕ подтверждено: {exc}") from exc
        raise UnsafeTradeError(f"{sym}: SL/TP не подтверждены; позиция аварийно закрыта")
    entry = float(pos.get("entryPrice") or last)
    if str(pos.get("side") or "").lower() != ("long" if d == 1 else "short"):
        raise UnsafeTradeError(f"{sym}: сторона позиции не совпала; проверьте Bybit вручную")
    return {
        "qty": float(pos.get("contracts") or qty), "entry": entry,
        "stop": float(sl), "target": float(tp), "dist": sig["dist"],
        "risk_usdt": est_risk, "d": d, "side": sig["side"],
        "order_id": str(order.get("id") or ""),
        "opened_ms": int(time.time() * 1000),
    }, ""


# ---------------- СОСТОЯНИЕ И ЛИМИТЫ ----------------
def load_state():
    base = {
        "schema_version": 2, "last": {}, "open": {}, "closed": [],
        "legacy_closed": [], "pending": None, "day": "", "day_start": 0.0,
        "peak": 0.0, "halted": False, "notified_day": "", "paused": False,
    }
    if not STATE_FILE.exists():
        return base
    try:
        st = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(st, dict):
            raise ValueError("ожидался JSON объект")
    except (OSError, ValueError) as exc:
        # Не начинать торговлю с пустым состоянием после порчи файла.
        raise RuntimeError(f"Файл состояния повреждён: {exc}. Проверьте Bybit и восстановите файл") from exc
    if st.get("schema_version", 1) < 2:
        # Старые 9 сделок рассчитаны по общей дельте баланса: не смешивать с новым PnL.
        st["legacy_closed"] = st.get("closed", [])
        st["closed"] = []
        st["schema_version"] = 2
    for k, v in base.items():
        st.setdefault(k, v)
    if not isinstance(st["open"], dict):
        raise RuntimeError("Некорректная структура открытых сделок в состоянии")
    return st


def save_state(st):
    """Атомарная запись: при рестарте нельзя читать наполовину записанный JSON."""
    import tempfile
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(st, ensure_ascii=False, allow_nan=False)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=str(STATE_FILE.parent),
            prefix=".trader_", suffix=".tmp", delete=False,
        ) as tmp:
            temp_path = Path(tmp.name)
            tmp.write(payload)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(temp_path, STATE_FILE)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def risk_gate(st, eq):
    """Запрещает новые входы. База лимитов ограничена торговым капиталом."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if st["day"] != today:
        st["day"], st["day_start"] = today, eq
        st["notified_day"] = ""
    st["peak"] = max(float(st["peak"]), eq)
    if STOP_FILE.exists():
        return "файл STOP"
    if st.get("pending"):
        return "незавершённая отправка ордера, требуется проверка на Bybit"
    if st["halted"]:
        return "аварийная остановка (сначала проверить биржу и состояние)"
    peak_base = min(CAPITAL_CAP, float(st["peak"]))
    daily_base = min(CAPITAL_CAP, float(st["day_start"]))
    if peak_base > 0 and float(st["peak"]) - eq > peak_base * MAX_DRAWDOWN:
        st["halted"] = True
        return f"просадка торгового капитала более {MAX_DRAWDOWN:.0%}: ручная остановка"
    if daily_base > 0 and float(st["day_start"]) - eq > daily_base * DAILY_LOSS_LIMIT:
        return f"дневной лимит убытка {DAILY_LOSS_LIMIT:.0%}: пауза до завтра"
    return None


def fetch_trade_closed_pnl(ex, sym, trade):
    """Возвращает (pnl, количество) или None, пока биржа не подтвердила закрытие.

    Записи о закрытии отдельной позиции берутся с Bybit V5 Closed PnL, а не из
    разницы баланса всего счёта. При частичных закрытиях суммируются строки.
    """
    opened_ms = int(trade.get("opened_ms") or 0)
    qty = float(trade.get("qty") or 0)
    if not opened_ms or qty <= 0:
        return None
    end_ms = int(time.time() * 1000)
    start_ms = max(opened_ms - 5000, end_ms - 7 * 86400 * 1000 + 1)
    params = {"category": "linear", "symbol": ex.market_id(sym),
              "startTime": start_ms, "endTime": end_ms, "limit": 100}
    rows = []
    cursor = None
    for _ in range(5):
        if cursor:
            params["cursor"] = cursor
        resp = ex.private_get_v5_position_closed_pnl(params)
        if str(resp.get("retCode", 0)) != "0":
            raise RuntimeError(f"Bybit Closed PnL: {resp.get('retMsg')}")
        result = resp.get("result") or {}
        rows.extend(result.get("list") or [])
        cursor = result.get("nextPageCursor")
        if not cursor:
            break
    else:
        raise RuntimeError(f"{sym}: история закрытий длиннее 5 страниц")
    candidates = [
        r for r in rows
        if int(r.get("createdTime") or r.get("updatedTime") or 0) >= opened_ms - 5000
        and float(r.get("closedSize") or 0) > 0
    ]
    total_qty = sum(float(r.get("closedSize") or 0) for r in candidates)
    # Не приписываем PnL чужих операций: требуем соответствие объёма.
    step = float((ex.market(sym).get("precision") or {}).get("amount") or 0)
    tolerance = max(step / 2, qty * 0.00001, 1e-10)
    if abs(total_qty - qty) > tolerance:
        return None
    return sum(float(r["closedPnl"]) for r in candidates), total_qty


def manage_open(ex, st, positions):
    """Проверяет таймаут и записывает только подтверждённый биржевой PnL."""
    for sym in list(st["open"]):
        trade = st["open"][sym]
        if sym not in positions:
            value = fetch_trade_closed_pnl(ex, sym, trade)
            if value is None:
                REASONS[sym] = "закрытие ожидает сверки Closed PnL Bybit"
                continue
            pnl, closed_qty = value
            nominal_risk = float(trade.get("risk_usdt") or
                                 (float(trade["qty"]) * float(trade["dist"])))
            r_est = pnl / nominal_risk if nominal_risk > 0 else 0
            ids = f"{sym}:{trade.get('opened_ms')}"
            if not any(c.get("trade_id") == ids for c in st["closed"]):
                st["closed"].append({
                    "trade_id": ids, "ts": int(time.time()), "symbol": sym,
                    "side": trade.get("side", "?"), "r": r_est, "pnl": pnl,
                    "closed_qty": closed_qty, "source": "bybit_closed_pnl",
                })
                save_state(st)
                sb.tg(
                    f"{'✅' if pnl > 0 else '❌'} {sym} закрыта. "
                    f"Bybit Closed PnL: {pnl:+.3f} USDT ({r_est:+.2f}R)\n"
                    + sb.stats_line([c["r"] for c in st["closed"]])
                )
            del st["open"][sym]
            save_state(st)
        elif time.time() * 1000 - int(trade["opened_ms"]) > sb.MAX_HOLD * 3_600_000:
            try:
                pos = positions[sym]
                if str(pos.get("side") or "").lower() != ("long" if trade["d"] == 1 else "short"):
                    raise UnsafeTradeError("сторона позиции отличается от записанной")
                close_market(ex, sym, float(pos["contracts"]), trade["d"])
                sb.tg(f"⏱ {sym}: таймаут {sb.MAX_HOLD} ч, отправлено закрытие по рынку; ждём подтверждения")
            except Exception as exc:
                st["halted"] = True
                st["paused"] = True
                save_state(st)
                sb.tg(f"🚨 {sym}: закрытие по таймауту не подтверждено: {exc}. Новые входы остановлены")
                raise


# ---------------- ОСНОВНОЙ ЦИКЛ ----------------
def run_cycle(ex, st, symbols):
    eq = equity(ex)
    sb.CAPITAL = min(CAPITAL_CAP, eq)   # размер позиции считается от реального баланса
    positions = open_positions(ex)
    manage_open(ex, st, positions)
    META["last_cycle"] = time.time()

    gate = risk_gate(st, eq)
    if gate:
        for s_ in symbols:
            REASONS[s_] = f"новые сделки отключены: {gate}"
        key = st["day"] + gate
        if st.get("notified_day") != key:
            sb.tg(f"⛔ Новые сделки отключены: {gate}")
            st["notified_day"] = key
        save_state(st)
        return
    if st.get("paused"):
        for s_ in symbols:
            REASONS[s_] = "пауза (кнопка в боте)"
        save_state(st)
        return

    try:
        btc = sb.trend_dir(sb.fetch_df(ex, sb.BTC_SYMBOL, sb.HTF))
    except Exception:
        btc = None

    for sym in symbols:
        if sym in positions or sym in st["open"]:
            REASONS[sym] = "позиция уже открыта"
            continue
        if len(positions) >= MAX_OPEN_POSITIONS:
            REASONS[sym] = f"лимит открытых позиций ({MAX_OPEN_POSITIONS})"
            continue
        try:
            df = sb.fetch_df(ex, sym, sb.TF)
            if int(df["ts"].iloc[-1]) - st["last"].get(sym, 0) < sb.COOLDOWN_BARS * 3_600_000:
                REASONS[sym] = f"пауза {sb.COOLDOWN_BARS} ч после последнего сигнала"
                continue
            try:
                funding = ex.fetch_funding_rate(sym).get("fundingRate")
            except Exception:
                funding = None
            htf = sb.fetch_df(ex, sym, sb.HTF)
            sig, reason = sb.evaluate(sym, df, htf, btc, funding, sb.market_info(ex, sym))
            if not sig:
                REASONS[sym] = reason
                print(f"[{sym}] нет сигнала: {reason}")
                continue
            REASONS[sym] = "сигнал найден"
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
            sb.tg(f"⚠ {sym}: ошибка в боте: {str(e)[:500]}")
    save_state(st)


def main():
    ex = make_exchange()
    ex.load_markets()
    symbols = [s_ for s_ in sb.SYMBOLS if s_ in ex.markets]
    st = load_state()
    sb.tg(f"Автобот запущен. Режим: {MODE.upper()}, капитал ≤{CAPITAL_CAP} USDT, "
          f"риск {sb.RISK_PCT:.0%}, плечо ≤{sb.MAX_LEVERAGE}x, пары: {', '.join(symbols)}")

    if sb.TG_TOKEN and sb.TG_CHAT_ID:   # кнопки в Telegram
        ctx = types.SimpleNamespace(
            ex=ex, st=st, lock=LOCK, reasons=REASONS, meta=META,
            equity=lambda: equity(ex), positions=lambda: open_positions(ex),
            close_market=lambda s_, q, d: close_market(ex, s_, q, d),
            save=lambda: save_state(st),
            info={"mode": MODE.upper(), "symbols": symbols, "capital_cap": CAPITAL_CAP,
                  "risk": sb.RISK_PCT, "lev": sb.MAX_LEVERAGE, "max_pos": MAX_OPEN_POSITIONS,
                  "scan": sb.SCAN_EVERY_SEC, "daily": DAILY_LOSS_LIMIT, "dd": MAX_DRAWDOWN})
        threading.Thread(target=tg_ui.TgUI(ctx, sb.TG_TOKEN, sb.TG_CHAT_ID).run, daemon=True).start()

    if MODE == "live":
        print("LIVE-режим: старт через 10 секунд, Ctrl+C для отмены")
        time.sleep(10)

    while True:
        try:
            with LOCK:
                run_cycle(ex, st, symbols)
        except Exception as e:
            print("Цикл:", e)
        time.sleep(sb.SCAN_EVERY_SEC)


if __name__ == "__main__":
    main()
