# -*- coding: utf-8 -*-
"""
threads/key_health.py — фоновая проверка живости API-ключей FastGen (задача Б,
этап 2). При «Сохранить» в Settings проверяем каждый непустой ключ ЛЁГКИМ
запросом БЕЗ генерации (не тратит кредиты):

    POST {STORAGE_BASE}/upload  с заголовком X-API-Key и БЕЗ файла.

Сервер по ответу разделяет auth от тела (подтверждено curl-тестом):
  • 401 / 403            → ключ МЁРТВЫЙ          → 'dead'
  • 429                  → лимит                 → 'limit'
  • 422 / иной не-auth   → ключ ЖИВОЙ (сервер узнал ключ, ругается лишь на
                           отсутствие файла)     → 'alive'
  • timeout / ConnectionError / 5xx → СЕРВЕР НЕДОСТУПЕН (ключ НЕ виноват)
                                                 → 'server_down'

Cross-platform: чистый requests, без subprocess/shell. Ключи проверяются
ПОСЛЕДОВАТЕЛЬНО (≤5, лёгкий запрос ~до 10с) — не плодит потоки и не бьёт
concurrency-лимит сервера. STOP-флаг для graceful shutdown.
"""
from __future__ import annotations

import requests
from PyQt6.QtCore import QThread, pyqtSignal

# Тот же storage-endpoint, что использует заливка рефов (storyboard_app.py).
STORAGE_BASE = "https://storage.fast-gen.ai"
PROBE_TIMEOUT_SEC = 10


class KeyHealthThread(QThread):
    """Проверяет живость списка ключей. Эмитит результат по каждому ключу
    отдельным сигналом (UI красит сразу), в конце — `done`."""

    # (field_idx, key_str, status); status ∈ dead | alive | limit | server_down
    key_health = pyqtSignal(int, str, str)
    done = pyqtSignal()

    def __init__(self, pairs, parent=None):
        """pairs: список (field_idx:int, key_str:str) — НЕПУСТЫЕ, не manual-off."""
        super().__init__(parent)
        self._pairs = list(pairs or [])
        self._stop = False

    def stop(self):
        """Запросить остановку (graceful shutdown). Прерывает между ключами."""
        self._stop = True

    def run(self):
        for field_idx, key_str in self._pairs:
            if self._stop:
                break
            status = self._probe(key_str)
            try:
                self.key_health.emit(int(field_idx), str(key_str), status)
            except Exception:
                pass
        try:
            self.done.emit()
        except Exception:
            pass

    def _probe(self, key_str) -> str:
        """Один лёгкий запрос. Никогда не кидает наружу — на любой непонятной
        ситуации считаем сервер недоступным (ключ не виним)."""
        try:
            r = requests.post(
                f"{STORAGE_BASE}/upload",
                headers={"X-API-Key": str(key_str or "")},
                timeout=PROBE_TIMEOUT_SEC)
            code = r.status_code
            if code in (401, 403):
                return 'dead'
            if code == 429:
                return 'limit'
            if code >= 500:
                return 'server_down'
            # 422 (нет файла) и прочие не-auth коды → ключ сервер УЗНАЛ → живой.
            return 'alive'
        except requests.exceptions.Timeout:
            return 'server_down'
        except requests.exceptions.ConnectionError:
            return 'server_down'
        except Exception:
            return 'server_down'
