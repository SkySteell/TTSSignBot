#!/usr/bin/env python3
"""
Точка входа для Bothost.
  * При наличии BYBIT_KEY и BYBIT_SECRET: ТОЛЬКО демоторговля (MODE=demo).
  * Если ключей нет: только сигналы в Telegram (ключи Bybit не нужны).
Переменные окружения (вводятся в панели Bothost, НЕ в код):
  TG_TOKEN, TG_CHAT_ID, TG_ADMIN_ID, CAPITAL (по умолчанию 150), BYBIT_KEY, BYBIT_SECRET, MODE
"""
import os
import sys


try:
    sys.stdout.reconfigure(line_buffering=True)  # чтобы логи Bothost появлялись сразу
except Exception:
    pass

import bybit_futures_signal_bot as sb

trading = bool(os.getenv("BYBIT_KEY") and os.getenv("BYBIT_SECRET"))
mode = os.getenv("MODE", "demo").upper() if trading else "ТОЛЬКО СИГНАЛЫ"


VERSION = "v4.2: DEMO 150 USDT, восстановление связи с Bybit"
sb.tg(f"Запуск на Bothost. Версия: {VERSION}. Режим: {mode}. Капитал {os.getenv('CAPITAL', '150')} USDT.")

import traceback

try:
    if trading:
        import bybit_futures_trader as app
    else:
        app = sb
    app.main()
except Exception:
    err = traceback.format_exc()
    print(err)
    sb.tg("🚨 Бот остановился с ошибкой:\n" + err[-1500:])
    raise
