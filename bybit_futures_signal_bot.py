#!/usr/bin/env python3
"""
Бот сигналов для Bybit USDT-перпетуалов (фьючерсы, умеренное плечо).
ТОЛЬКО СИГНАЛЫ: ордера сам не отправляет, API-ключи не нужны.

Что делает:
  1) раз в цикл проверяет пары на закрытых свечах 1h с фильтром по 4h;
  2) ищет два сетапа: откат по тренду и пробой с объёмом (лонг и шорт);
  3) считает стоп, цель, размер позиции, плечо, оценку ликвидации под ваш капитал;
  4) присылает сигнал в Telegram и ведёт ПАПЕР-ТРЕКЕР: сам проверяет, чем
     закончился каждый сигнал (стоп/цель/таймаут), и считает статистику в R
     после комиссий. Это даёт честные данные, прежде чем рисковать деньгами.

Установка:  pip install ccxt pandas numpy requests
Запуск:     TG_TOKEN=... TG_CHAT_ID=... python bybit_futures_signal_bot.py
Это не финансовая рекомендация. Фьючерсы с плечом могут привести к потере всего депозита.
"""
import json
import math
import os
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests

# ======================= НАСТРОЙКИ =======================
SYMBOLS = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "KAS/USDT:USDT"]
BTC_SYMBOL = "BTC/USDT:USDT"
TF, HTF = "1h", "4h"
LIMIT = 300

CAPITAL = float(os.getenv("CAPITAL", "15"))  # капитал в USDT
RISK_PCT = 0.01          # плановый риск на сделку: 1% капитала
HARD_RISK_PCT = 0.02     # потолок риска, если мин. лот заставляет рисковать больше плана
MAX_LEVERAGE = 3         # потолок «умеренного» плеча (по номиналу позиции / капиталу)
MARGIN_SHARE = 0.5       # одна позиция занимает не больше 50% капитала как маржа
MMR = 0.005              # грубая оценка maintenance margin для расчёта ликвидации

ADX_MIN = 20             # ниже: боковик, трендовые сигналы не берём
VOL_MULT = 1.5           # объём пробоя минимум 1.5× от среднего за 20 свечей
STOP_MIN_ATR, STOP_MAX_ATR = 1.0, 2.5   # допустимая дистанция стопа в ATR
RR = 2.0                 # цель = 2 × стоп
MAX_HOLD = 48            # таймаут сделки в свечах (для трекера)
COOLDOWN_BARS = 6        # пауза между сигналами по одной паре
FUNDING_SKIP = 0.0005    # пропуск, если фандинг против позиции больше 0.05%

FEE_TAKER = 0.00055      # базовый тейкер Bybit для USDT-перпетуалов (проверьте свой тариф)
SLIPPAGE = 0.0003        # допущение, за сторону
RT_COST = 2 * (FEE_TAKER + SLIPPAGE)    # издержки на круг (вход+выход), доля цены
MAX_COST_R = 0.25        # не берём сигнал, если издержки съедают >0.25R

SCAN_EVERY_SEC = 300
STATE_FILE = Path("futures_bot_state.json")
TG_TOKEN = os.getenv("TG_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")


# ======================= ИНДИКАТОРЫ =======================
def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def rsi(close, n=14):
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def atr(df, n=14):
    pc = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(),
                    (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def adx(df, n=14):
    up, dn = df["high"].diff(), -df["low"].diff()
    plus = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    minus = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    a = atr(df, n)
    pdi = 100 * plus.ewm(alpha=1 / n, adjust=False).mean() / a
    mdi = 100 * minus.ewm(alpha=1 / n, adjust=False).mean() / a
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean()


def trend_dir(df):
    """+1 восходящий, -1 нисходящий, 0 нет тренда (close/EMA50/EMA200)."""
    c = df["close"]
    e50, e200 = ema(c, 50).iloc[-1], ema(c, 200).iloc[-1]
    if c.iloc[-1] > e50 > e200:
        return 1
    if c.iloc[-1] < e50 < e200:
        return -1
    return 0


# ======================= РАЗМЕР ПОЗИЦИИ (ФЬЮЧЕРСЫ) =======================
def size_position(price, dist, d, market):
    """
    Возвращает (dict | None, причина). market: {'min_amt','min_cost','step'}.
    Логика: риск = RISK_PCT×капитал; позиция ограничена MAX_LEVERAGE×капитал;
    плечо = минимальное, чтобы маржа ≤ MARGIN_SHARE×капитал (изолированная маржа).
    """
    step = market.get("step") or 0.001
    min_amt = market.get("min_amt") or step
    min_cost = market.get("min_cost") or 0.0
    cap_notional = CAPITAL * MAX_LEVERAGE

    qty = (CAPITAL * RISK_PCT) / dist
    if qty * price > cap_notional:
        qty = cap_notional / price
    qty = math.floor(qty / step + 1e-9) * step
    flags = []
    if qty < min_amt:
        qty = min_amt
        flags.append("минимальный лот выше плана")
    if min_cost and qty * price < min_cost:
        qty = math.ceil(min_cost / price / step) * step
        flags.append("мин. сумма ордера выше плана")
    qty = round(qty, 8)

    risk = qty * dist
    if risk > CAPITAL * HARD_RISK_PCT:
        return None, (f"минимальный лот требует риска {risk:.2f} USDT "
                      f"({risk / CAPITAL:.1%} капитала): пропуск")
    notional = qty * price
    if notional > cap_notional * 1.001:
        return None, "позиция выше лимита плеча: пропуск"

    lev = max(1, math.ceil(notional / (CAPITAL * MARGIN_SHARE)))
    margin = notional / lev
    liq = price * (1 - 1 / lev + MMR) if d == 1 else price * (1 + 1 / lev - MMR)
    if abs(price - liq) < 2 * dist:
        return None, "ликвидация слишком близко к стопу: пропуск"
    return {"qty": qty, "notional": notional, "lev": lev, "margin": margin,
            "risk": risk, "risk_pct": risk / CAPITAL, "liq": liq, "flags": flags}, ""


# ======================= ЛОГИКА СИГНАЛА =======================
def evaluate(symbol, df, htf, btc_dir, funding, market):
    """Чистая функция: возвращает (signal | None, причина отказа)."""
    if len(df) < 210 or len(htf) < 210:
        return None, "мало данных"
    c, o, h, l, v = (df[k] for k in ("close", "open", "high", "low", "volume"))
    d = trend_dir(htf)
    if d == 0:
        return None, "4h без тренда"
    if btc_dir is not None and symbol != BTC_SYMBOL and btc_dir == -d:
        return None, "BTC на 4h против направления"

    e20, e50, e200 = ema(c, 20), ema(c, 50), ema(c, 200)
    r, a = rsi(c), atr(df)
    adx_v = float(adx(df).iloc[-1])
    if adx_v < ADX_MIN:
        return None, f"ADX {adx_v:.0f} < {ADX_MIN}: боковик"

    price, atr_v = float(c.iloc[-1]), float(a.iloc[-1])
    vol_ratio = float(v.iloc[-1] / v.rolling(20).mean().iloc[-1])
    hh = float(h.rolling(20).max().shift().iloc[-1])
    ll = float(l.rolling(20).min().shift().iloc[-1])
    bull = c.iloc[-1] > o.iloc[-1]
    candle_ok = bull if d == 1 else (not bull)
    tr1h = (price > e200.iloc[-1] and e50.iloc[-1] > e200.iloc[-1]) if d == 1 else \
           (price < e200.iloc[-1] and e50.iloc[-1] < e200.iloc[-1])
    if not tr1h:
        return None, "1h не подтверждает тренд"

    setup, why = None, []
    if d == 1:
        touched = bool((l.iloc[-6:] <= e20.iloc[-6:]).any())
        rsi_ret = r.iloc[-8:-1].min() < 45 and r.iloc[-1] > 50
        if touched and rsi_ret and candle_ok and price > e20.iloc[-1] and price - e20.iloc[-1] < 1.5 * atr_v:
            setup = "Откат по тренду (лонг)"
            why = ["4h: цена > EMA50 > EMA200", "1h: касание EMA20 за последние 6 свечей",
                   f"RSI вернулся выше 50 (сейчас {r.iloc[-1]:.0f})", "закрытие бычьей свечи выше EMA20"]
        elif price > hh and vol_ratio >= VOL_MULT and candle_ok and \
                price - e20.iloc[-1] <= 2.5 * atr_v and r.iloc[-1] < 75:
            setup = "Пробой с объёмом (лонг)"
            why = ["4h: цена > EMA50 > EMA200", "закрытие выше максимума 20 свечей",
                   f"объём x{vol_ratio:.1f}", "нет перегрева: RSI < 75, цена не далеко от EMA20"]
    else:
        touched = bool((h.iloc[-6:] >= e20.iloc[-6:]).any())
        rsi_ret = r.iloc[-8:-1].max() > 55 and r.iloc[-1] < 50
        if touched and rsi_ret and candle_ok and price < e20.iloc[-1] and e20.iloc[-1] - price < 1.5 * atr_v:
            setup = "Откат по тренду (шорт)"
            why = ["4h: цена < EMA50 < EMA200", "1h: касание EMA20 за последние 6 свечей",
                   f"RSI вернулся ниже 50 (сейчас {r.iloc[-1]:.0f})", "закрытие медвежьей свечи ниже EMA20"]
        elif price < ll and vol_ratio >= VOL_MULT and candle_ok and \
                e20.iloc[-1] - price <= 2.5 * atr_v and r.iloc[-1] > 25:
            setup = "Пробой с объёмом (шорт)"
            why = ["4h: цена < EMA50 < EMA200", "закрытие ниже минимума 20 свечей",
                   f"объём x{vol_ratio:.1f}", "нет перегрева: RSI > 25, цена не далеко от EMA20"]
    if setup is None:
        return None, "условия сетапов не выполнены"

    # Стоп: за свинг последних 6 свечей + 0.25 ATR, в коридоре 1–2.5 ATR
    if d == 1:
        stop = float(l.iloc[-6:].min()) - 0.25 * atr_v
        dist = price - stop
    else:
        stop = float(h.iloc[-6:].max()) + 0.25 * atr_v
        dist = stop - price
    if dist > STOP_MAX_ATR * atr_v:
        return None, "стоп за свингом шире 2.5 ATR: пропуск"
    if dist < STOP_MIN_ATR * atr_v:
        dist = STOP_MIN_ATR * atr_v
        stop = price - d * dist
    cost_r = RT_COST / (dist / price)
    if cost_r > MAX_COST_R:
        return None, f"издержки {cost_r:.2f}R > {MAX_COST_R}R: стоп слишком узкий"
    if funding is not None and d * funding > FUNDING_SKIP:
        return None, f"фандинг {funding:.3%} против позиции"
    target = price + d * RR * dist

    pos, reason = size_position(price, dist, d, market)
    if pos is None:
        return None, reason
    why.append(f"ADX {adx_v:.0f} ≥ {ADX_MIN}; издержки {cost_r:.2f}R")
    return {"symbol": symbol, "side": "LONG" if d == 1 else "SHORT", "d": d, "setup": setup,
            "price": price, "stop": stop, "target": target, "dist": dist, "cost_r": cost_r,
            "bar_ts": int(df["ts"].iloc[-1]), "funding": funding, "why": why, **pos}, ""


# ======================= ПАПЕР-ТРЕКЕР =======================
def resolve(trade, df):
    """Проверяет свечи после входа. Возвращает (R | None, причина, цена выхода)."""
    d, entry, stop, target, dist = trade["d"], trade["price"], trade["stop"], trade["target"], trade["dist"]
    bars = 0
    for _, row in df[df["ts"] > trade["bar_ts"]].iterrows():
        bars += 1
        hit_stop = row["low"] <= stop if d == 1 else row["high"] >= stop
        hit_tp = row["high"] >= target if d == 1 else row["low"] <= target
        px, why = None, None
        if hit_stop:  # консервативно: при касании обоих первым считаем стоп
            px, why = stop, "стоп"
        elif hit_tp:
            px, why = target, "цель"
        elif bars >= MAX_HOLD:
            px, why = float(row["close"]), "таймаут"
        if px is not None:
            r = d * (px - entry) / dist - entry * RT_COST / dist
            return r, why, px
    return None, "", None


def stats_line(closed):
    if not closed:
        return "статистика: пока нет закрытых сигналов"
    r = np.array(closed)
    wins, losses = r[r > 0].sum(), -r[r < 0].sum()
    pf = wins / losses if losses > 0 else float("inf")
    return (f"статистика: {len(r)} сигн., винрейт {(r > 0).mean():.0%}, ср.R {r.mean():+.2f}, "
            f"итого {r.sum():+.1f}R, PF {pf:.2f}")


# ======================= ВВОД-ВЫВОД =======================
def tg(text):
    print(text, "\n")
    if not (TG_TOKEN and TG_CHAT_ID):
        print("⚠ Telegram не настроен: TG_TOKEN и TG_CHAT_ID не заданы в этом окне")
        return
    try:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          json={"chat_id": TG_CHAT_ID, "text": text}, timeout=15)
        if r.status_code != 200:
            print(f"⚠ Telegram ответил {r.status_code}: {r.text}")
        else:
            print("✓ отправлено в Telegram")
    except requests.RequestException as e:
        print("⚠ Telegram error:", e)


def fmt(x):
    return f"{x:.6f}" if x < 1 else f"{x:.4f}" if x < 100 else f"{x:.2f}"


def signal_text(s):
    emoji = "🟢" if s["d"] == 1 else "🔴"
    fund = f"{s['funding']:.3%}" if s["funding"] is not None else "н/д"
    lines = [
        f"{emoji} {s['symbol']}  {s['side']}  [{TF}]  {s['setup']}",
        f"Вход ~ {fmt(s['price'])} (лимитка у этой цены, не гнаться)",
        f"Стоп: {fmt(s['stop'])}  ({s['dist'] / s['price']:.2%} от цены)",
        f"Цель: {fmt(s['target'])}  (R:R 1:{RR:g})",
        f"Объём: {s['qty']:g} (номинал {s['notional']:.2f} USDT)",
        f"Изолированная маржа, плечо {s['lev']}x: маржа {s['margin']:.2f} USDT",
        f"Риск: {s['risk']:.2f} USDT ({s['risk_pct']:.1%} капитала)",
        f"Ликвидация (оценка): {fmt(s['liq'])}  | фандинг {fund}",
        "Условия входа:", *[f"• {w}" for w in s["why"]],
        "План: после +1R можно подтянуть стоп в безубыток. Стоп ставить сразу с входом.",
    ]
    if s["flags"]:
        lines.append("⚠ " + "; ".join(s["flags"]))
    return "\n".join(lines)


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"last": {}, "open": [], "closed": []}


def save_state(st):
    STATE_FILE.write_text(json.dumps(st))


def fetch_df(ex, symbol, tf):
    raw = ex.fetch_ohlcv(symbol, timeframe=tf, limit=LIMIT)
    df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
    return df.iloc[:-1].reset_index(drop=True)  # без незакрытой свечи


def market_info(ex, symbol):
    m = ex.market(symbol)
    lim = m.get("limits", {})
    return {"min_amt": (lim.get("amount") or {}).get("min"),
            "min_cost": (lim.get("cost") or {}).get("min"),
            "step": (m.get("precision") or {}).get("amount")}


def main():
    import ccxt
    ex = ccxt.bybit({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    ex.load_markets()
    symbols = [s for s in SYMBOLS if s in ex.markets]
    missing = set(SYMBOLS) - set(symbols)
    if missing:
        print("Нет на Bybit (пропущены):", ", ".join(sorted(missing)))
    st = load_state()
    print(f"Бот запущен. Капитал {CAPITAL} USDT, риск {RISK_PCT:.0%}, плечо ≤{MAX_LEVERAGE}x. Пары: {', '.join(symbols)}")

    while True:
        try:
            btc = trend_dir(fetch_df(ex, BTC_SYMBOL, HTF))
        except Exception as e:
            print("BTC 4h error:", e)
            btc = None
        for sym in symbols:
            try:
                df = fetch_df(ex, sym, TF)
                # 1) закрываем открытые бумажные сигналы
                still = []
                for t in st["open"]:
                    if t["symbol"] != sym:
                        still.append(t)
                        continue
                    r, why, px = resolve(t, df)
                    if r is None:
                        still.append(t)
                    else:
                        st["closed"].append(r)
                        tg(f"{'✅' if r > 0 else '❌'} {sym} {t['side']} закрыт: {why}, {r:+.2f}R "
                           f"(после комиссий)\n{stats_line(st['closed'])}")
                st["open"] = still
                # 2) новый сигнал
                if any(t["symbol"] == sym for t in st["open"]):
                    continue
                if int(df["ts"].iloc[-1]) - st["last"].get(sym, 0) < COOLDOWN_BARS * 3_600_000:
                    continue
                try:
                    funding = ex.fetch_funding_rate(sym).get("fundingRate")
                except Exception:
                    funding = None
                htf = fetch_df(ex, sym, HTF)
                sig, reason = evaluate(sym, df, htf, btc, funding, market_info(ex, sym))
                if sig:
                    tg(signal_text(sig))
                    st["open"].append({k: sig[k] for k in
                                       ("symbol", "side", "d", "price", "stop", "target", "dist", "bar_ts")})
                    st["last"][sym] = sig["bar_ts"]
                else:
                    print(f"[{sym}] нет сигнала: {reason}")
                save_state(st)
            except Exception as e:
                print(f"[{sym}] ошибка: {e}")
        time.sleep(SCAN_EVERY_SEC)


if __name__ == "__main__":
    main()
