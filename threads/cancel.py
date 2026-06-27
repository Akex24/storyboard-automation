# -*- coding: utf-8 -*-
"""
threads/cancel.py — фоновая best-effort отмена задач генерации на сервере FastGen
при нажатии «Остановить генерацию». Для каждой пары (op_id, key) шлём
DELETE /api/v5/generations/{id} с X-API-Key ЭТОЙ задачи (операции привязаны к
ключу, которым созданы) и ПУСТЫМ телом. Локальный стоп уже произошёл —
серверная отмена лишь освобождает кредиты/слоты быстрее.

v5 (2026-06-20, форма подтверждена живым прозвоном коллеги): отмена —
DELETE /api/v5/generations/{id} (id в URL, тела нет). HTTP 200 ≠ «отменено» —
читать поле "cancelled" в теле (приходит СТРОКОЙ "True"/"False"). Старый v4
POST /api/v4/operations/cancel с body {"operation_id": …} больше НЕ работает
(поле operation_id не принимается → 422). Результат каждой отмены пишем в
runtime.log тегом [CANCEL] (print → studio tee); сам ключ в лог НЕ пишем.

Cross-platform: чистый requests, без subprocess/shell.
"""
from __future__ import annotations

import requests
from PyQt6.QtCore import QThread

# Тот же боевой хост, что и генерация (storyboard_app.API_BASE).
API_BASE = "https://googler.fast-gen.ai"
CANCEL_TIMEOUT_SEC = 15


class CancelThread(QThread):
    """Отменяет список задач [(op_id, key), …] на сервере. best-effort, без
    сигналов результата — вызывающий снимает поток через reaper по finished."""

    def __init__(self, pairs, parent=None):
        super().__init__(parent)
        self._pairs = list(pairs or [])

    def run(self):
        for op_id, key in self._pairs:
            if not op_id or not key:
                continue
            try:
                # v5: отмена — DELETE /api/v5/generations/{id}, X-API-Key
                # ключа-создателя, ПУСТОЕ тело (без json=). raise_for_status
                # НЕ зовём — 4xx/5xx по одной паре не должны ронять остальные.
                r = requests.delete(
                    f"{API_BASE}/api/v6/generations/{op_id}",
                    headers={"X-API-Key": str(key)},
                    timeout=CANCEL_TIMEOUT_SEC)
                # v5: HTTP 200 ≠ отменено. Поле "cancelled" — СТРОКА "True"/"False".
                cancelled = False
                try:
                    cancelled = str(r.json().get("cancelled")).lower() == "true"
                except Exception:
                    pass  # ответ не-JSON/пустой → cancelled=False, http залогируем
                # Лог в runtime.log (studio tee, тег [CANCEL]). Ключ НЕ пишем.
                print(f"[CANCEL] op_id={op_id} http={r.status_code} "
                      f"cancelled={cancelled}")
            except Exception as e:
                # Сеть/таймаут — best-effort: логируем и продолжаем остальные пары.
                print(f"[CANCEL] op_id={op_id} FAILED: {e}")
