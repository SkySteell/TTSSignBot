"""
Telegram-интерфейс с кнопками для автобота (без сторонних библиотек, только requests).
Отвечает ТОЛЬКО в чате TG_CHAT_ID, остальные игнорирует.
Кнопки: Статистика (день / 7 дней / 30 дней / всё), Позиции, Баланс, Статус, Пары,
Пауза / Продолжить, Закрыть всё (с подтверждением).
"""
import time
import secrets
from datetime import datetime, timezone

import requests


def _f(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def px(x):
    return f"{x:.6f}" if x < 1 else f"{x:.4f}" if x < 100 else f"{x:.2f}"


def ago(ts):
    if not ts:
        return "н/д"
    s = int(time.time() - ts)
    return f"{s} с назад" if s < 90 else f"{s // 60} мин назад" if s < 5400 else f"{s // 3600} ч назад"


class TgUI:
    def __init__(self, ctx, token, chat_id, admin_id):
        self.c = ctx
        self.chat = str(chat_id)
        self.admin_id = str(admin_id)
        self.close_token = None
        self.close_requested_at = 0
        self.api = f"https://api.telegram.org/bot{token}"
        self.offset = 0

    # ---------- транспорт ----------
    def call(self, method, **payload):
        try:
            return requests.post(f"{self.api}/{method}", json=payload, timeout=45).json()
        except Exception as e:
            print("TG UI:", e)
            return {}

    def menu(self):
        paused = self.c.st.get("paused")
        rows = [["📊 Статистика", "📌 Позиции"], ["💰 Баланс", "ℹ️ Статус"],
                ["🔎 Пары", "▶ Продолжить" if paused else "⏸ Пауза"], ["🛑 Закрыть всё"]]
        return {"keyboard": [[{"text": t} for t in r] for r in rows],
                "resize_keyboard": True, "is_persistent": True}

    def send(self, text, markup=None):
        self.call("sendMessage", chat_id=self.chat, text=text[:4000],
                  reply_markup=markup if markup is not None else self.menu())

    def inline(self, rows):
        return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in r] for r in rows]}

    # ---------- цикл ----------
    def run(self):
        self.call("deleteWebhook")
        self.send("Меню готово. Кнопки внизу экрана.")
        while True:
            r = self.call("getUpdates", offset=self.offset, timeout=30,
                          allowed_updates=["message", "callback_query"])
            if not r.get("ok"):
                if r.get("error_code") == 409:
                    print("TG UI: конфликт getUpdates: этот токен использует другой процесс")
                time.sleep(5)
                continue
            for u in r["result"]:
                self.offset = u["update_id"] + 1
                try:
                    self.handle(u)
                except Exception as e:
                    print("TG UI handle:", e)
                    self.send(f"⚠ Ошибка интерфейса: {str(e)[:300]}")

    def handle(self, u):
        cq = u.get("callback_query")
        if cq:
            self.call("answerCallbackQuery", callback_query_id=cq["id"])
            if (str((cq.get("message") or {}).get("chat", {}).get("id")) == self.chat
                    and str((cq.get("from") or {}).get("id")) == self.admin_id):
                self.on_action(cq.get("data", ""))
            return
        m = u.get("message") or {}
        if (str(m.get("chat", {}).get("id")) != self.chat
                or str((m.get("from") or {}).get("id")) != self.admin_id):
            return
        self.on_text((m.get("text") or "").strip())

    # ---------- команды ----------
    def on_text(self, t):
        if t in ("/start", "/menu") or t.lower() == "меню":
            self.send("Меню:")
        elif "Статистика" in t:
            self.send("За какой период?", self.inline([[("Сегодня", "stats:today"), ("7 дней", "stats:7")],
                                                       [("30 дней", "stats:30"), ("Всё время", "stats:all")]]))
        elif "Позиции" in t:
            self.send(self.positions_text())
        elif "Баланс" in t:
            self.send(self.balance_text())
        elif "Статус" in t:
            self.send(self.status_text())
        elif "Пары" in t:
            self.send(self.pairs_text())
        elif t == "⏸ Пауза":
            with self.c.lock:
                self.c.st["paused"] = True
                self.c.save()
            self.send("⏸ Новые входы остановлены. Позиции остаются под защитой на Bybit.")
        elif t == "▶ Продолжить":
            with self.c.lock:
                blocked = self.c.st.get("halted") or self.c.st.get("pending")
                if not blocked:
                    self.c.st["paused"] = False
                    self.c.save()
            self.send("⛔ Аварийная остановка: сначала требуется сверка с Bybit."
                      if blocked else "▶ Бот снова ищет входы.")
        elif t == "🛑 Закрыть всё":
            self.close_token = secrets.token_urlsafe(8)
            self.close_requested_at = time.time()
            self.send("Закрыть ВСЕ открытые позиции по рынку и остановить новые входы?",
                      self.inline([[("Да, закрыть всё", "closeall:" + self.close_token),
                                    ("Отмена", "closeall:no")]]))
        else:
            self.send("Выберите действие кнопками внизу.")

    def on_action(self, data):
        if data.startswith("stats:"):
            self.send(self.stats_text(data.split(":")[1]))
        elif data == "closeall:no":
            self.close_token = None
            self.send("Отменено.")
        elif data.startswith("closeall:"):
            token = data.split(":", 1)[1]
            if (not self.close_token or token != self.close_token
                    or time.time() - self.close_requested_at > 60):
                self.send("Подтверждение устарело. Запросите закрытие заново.")
                return
            self.close_token = None
            self.send(self.close_all())

    # ---------- тексты ----------
    def stats_text(self, period):
        if period == "today":
            start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            label = "сегодня (UTC)"
        elif period in ("7", "30"):
            start, label = time.time() - int(period) * 86400, f"{period} дней"
        else:
            start, label = 0, "всё время"
        with self.c.lock:
            rows = [x for x in self.c.st["closed"] if x["ts"] >= start] if start else list(self.c.st["closed"])
        if not rows:
            return f"📊 Статистика, {label}: закрытых сделок нет."
        r = [x["r"] for x in rows]
        wins = sum(v for v in r if v > 0)
        losses = -sum(v for v in r if v < 0)
        pf = f"{wins / losses:.2f}" if losses > 0 else "∞"
        pnl = sum(x.get("pnl", 0.0) for x in rows)
        by = {}
        for x in rows:
            n, s = by.get(x["symbol"], (0, 0.0))
            by[x["symbol"]] = (n + 1, s + x["r"])
        lines = [f"📊 Статистика, {label}",
                 f"Сделок: {len(r)}  |  винрейт {sum(v > 0 for v in r) / len(r):.0%}",
                 f"Средний результат: {sum(r) / len(r):+.2f}R  |  итого {sum(r):+.2f}R",
                 f"Profit factor: {pf}",
                 f"Лучшая {max(r):+.2f}R  |  худшая {min(r):+.2f}R",
                 f"Результат в деньгах: {pnl:+.3f} USDT", "По парам:"]
        lines += [f"• {s}: {n} сд., {v:+.2f}R" for s, (n, v) in sorted(by.items())]
        if len(r) < 30:
            lines.append(f"\nВыборка мала ({len(r)} из 30 нужных): выводы делать рано.")
        return "\n".join(lines)

    def positions_text(self):
        try:
            with self.c.lock:
                ps = self.c.positions()
        except Exception as e:
            return f"Не удалось получить позиции: {str(e)[:300]}"
        if not ps:
            return "📌 Открытых позиций нет."
        out = ["📌 Открытые позиции"]
        for sym, p in ps.items():
            info = p.get("info") or {}
            long_ = str(p.get("side", "")).lower() == "long"
            sl, tp = _f(info.get("stopLoss")), _f(info.get("takeProfit"))
            out.append(
                f"{'🟢 LONG' if long_ else '🔴 SHORT'} {sym}\n"
                f"Объём {p.get('contracts')}, вход {px(_f(p.get('entryPrice')))}, "
                f"сейчас {px(_f(p.get('markPrice')))}\n"
                f"Нереализованный результат: {_f(p.get('unrealizedPnl')):+.3f} USDT\n"
                f"SL: {px(sl) if sl else 'НЕТ ⚠'}  |  TP: {px(tp) if tp else 'НЕТ ⚠'}")
        return "\n\n".join(out)

    def balance_text(self):
        try:
            with self.c.lock:
                eq = self.c.equity()
                st = self.c.st
                day_start, peak = _f(st.get("day_start")), _f(st.get("peak"))
        except Exception as e:
            return f"Не удалось получить баланс: {str(e)[:300]}"
        i = self.c.info
        lines = [f"💰 Баланс: {eq:.2f} USDT"]
        if day_start > 0:
            lines.append(f"За сегодня: {eq - day_start:+.2f} USDT ({eq / day_start - 1:+.1%})")
        if peak > 0:
            lines.append(f"Просадка от пика: {max(0.0, 1 - eq / peak):.1%} (остановка при {i['dd']:.0%})")
        lines.append(f"Дневной лимит убытка: {i['daily']:.0%}")
        return "\n".join(lines)

    def status_text(self):
        i, st = self.c.info, self.c.st
        flags = []
        if st.get("paused"):
            flags.append("⏸ пауза")
        if st.get("halted"):
            flags.append("⛔ аварийная остановка")
        if st.get("pending"):
            flags.append("🚨 неразрешённый ордер")
        state = ", ".join(flags) if flags else "✅ работает"
        return (f"ℹ️ Статус: {state}\n"
                f"Режим: {i['mode']}\n"
                f"Капитал в расчёте: до {i['capital_cap']:g} USDT, риск {i['risk']:.0%} на сделку, "
                f"плечо ≤{i['lev']}x\n"
                f"Макс. позиций: {i['max_pos']}, проверка каждые {i['scan'] // 60} мин\n"
                f"Пары: {', '.join(i['symbols'])}\n"
                f"Последняя проверка: {ago(self.c.meta.get('last_cycle'))}\n"
                f"Сделок в новой статистике: {len(st['closed'])}\n"
                f"Исторических (непроверенных): {len(st.get('legacy_closed', []))}")

    def pairs_text(self):
        if not self.c.reasons:
            return "🔎 Первая проверка ещё не завершена."
        lines = [f"🔎 Почему нет входа (проверка {ago(self.c.meta.get('last_cycle'))}):"]
        lines += [f"• {s}: {why}" for s, why in self.c.reasons.items()]
        return "\n".join(lines)

    def close_all(self):
        out = []
        with self.c.lock:
            self.c.st["paused"] = True
            self.c.save()  # до первого рыночного ордера
            try:
                ps = self.c.positions()
            except Exception as exc:
                self.c.st["halted"] = True
                self.c.save()
                return f"🚨 Не удалось прочитать позиции: {str(exc)[:250]}"
            if not ps:
                return "⏸ Открытых позиций нет. Новые входы поставлены на паузу."
            errors = []
            for sym, pos in ps.items():
                side = str(pos.get("side") or "").lower()
                if side not in ("long", "short"):
                    errors.append(f"{sym}: неизвестное направление")
                    continue
                d = 1 if side == "long" else -1
                try:
                    self.c.close_market(sym, _f(pos.get("contracts")), d)
                    out.append(f"⏳ {sym}: отправлено закрытие")
                except Exception as exc:
                    errors.append(f"{sym}: {str(exc)[:200]}")
            try:
                for _ in range(5):
                    if not self.c.positions():
                        break
                    time.sleep(1)
                remaining = list(self.c.positions())
            except Exception as exc:
                remaining = ["невозможно проверить позиции"]
                errors.append(str(exc)[:200])
            if remaining:
                errors.append("Не закрыты / не подтверждены: " + ", ".join(remaining))
            if errors:
                self.c.st["halted"] = True
                out += ["🚨 " + e for e in errors]
            else:
                out.append("✅ Все позиции подтверждены закрытыми на бирже.")
            self.c.save()
        out.append("⏸ Новые входы остановлены. Возобновление — вручную после проверки.")
        return "\n".join(out)
