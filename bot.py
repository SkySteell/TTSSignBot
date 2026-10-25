#!/usr/bin/env python3
"""
Точка входа для Bothost.
  * Если заданы BYBIT_KEY и BYBIT_SECRET: автоторговля (MODE=demo по умолчанию).
  * Если ключей нет: только сигналы в Telegram (ключи Bybit не нужны).
Переменные окружения (вводятся в панели Bothost, НЕ в код):
  TG_TOKEN, TG_CHAT_ID, CAPITAL (по умолчанию 15), BYBIT_KEY, BYBIT_SECRET, MODE
"""
import os
import sys

import requests

try:
    sys.stdout.reconfigure(line_buffering=True)  # чтобы логи Bothost появлялись сразу
except Exception:
    pass

import bybit_futures_signal_bot as sb

trading = bool(os.getenv("BYBIT_KEY") and os.getenv("BYBIT_SECRET"))
mode = os.getenv("MODE", "demo").upper() if trading else "ТОЛЬКО СИГНАЛЫ"

try:
    ip = requests.get("https://api.ipify.org", timeout=8).text
    print(f"Исходящий IP сервера: {ip} (нужен для привязки боевого ключа Bybit)")
except Exception as e:
    print("IP определить не удалось:", e)

sb.tg(f"Запуск на Bothost. Режим: {mode}. Капитал {os.getenv('CAPITAL', '15')} USDT.")

if trading:
    import bybit_futures_trader as app
else:
    app = sb
app.main()
