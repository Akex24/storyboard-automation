# -*- coding: utf-8 -*-
"""
threads/cancel.py — фоновая best-effort отмена задач генерации на сервере FastGen
при нажатии «Остановить генерацию». Для каждой пары (op_id, key) шлём
POST /api/v4/operations/cancel с X-API-Key ЭТОЙ задачи (операции привязаны к
ключу, которым созданы). Ошибки молча глотаем — локальный стоп уже произошёл,
серверная отмена лишь освобождает кредиты/слоты быстрее.

⚠️ Точная форма запроса cancel (body {operation_id} vs query / cancel-all) пока
НЕ подтверждена живьём (FastGen в ремонте) — проверить при оживлении сервера.

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
                requests.post(
                    f"{API_BASE}/api/v4/operations/cancel",
                    headers={"X-API-Key": str(key)},
                    json={"operation_id": str(op_id)},
                    timeout=CANCEL_TIMEOUT_SEC)
            except Exception:
                pass
