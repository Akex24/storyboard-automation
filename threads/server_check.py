# -*- coding: utf-8 -*-
"""
threads/server_check.py — ручная проверка ЖИВОСТИ сервера генерации FastGen
(очередь #1). В отличие от key_health (лёгкий /upload без генерации) — здесь
РЕАЛЬНЫЙ боевой тест генерации одной картинки («яблоко»), как curl-тест из
handoff: POST /api/v4/flow/image/generate + poll /api/v4/operations/{id}.
Стоит ~4 кредита. Нужен когда сервер висит и непонятно — наш код или их сервер.

Поток получает ГОТОВЫЙ ключ (UI берёт key_pool.next_key() и передаёт сюда;
пустой ключ = «нет доступных» обрабатывается в UI ДО старта потока). Эмитит:
  • progress(sec)        — раз в секунду, для «проверяю… Nс» на кнопке;
  • result(outcome, sec) — итог: 'ok' | 'down' | 'noconn'.

Потолок 600с. STOP-флаг для graceful shutdown (closeEvent дренаж).
Cross-platform: чистый requests, без subprocess/shell.
"""
from __future__ import annotations

import requests
from PyQt6.QtCore import QThread, pyqtSignal

# Тот же боевой endpoint, что и реальная генерация (storyboard_app.API_BASE).
API_BASE = "https://googler.fast-gen.ai"

# Боевой тест: простой product-shot БЕЗ рефов, вертикальный кадр 9:16.
TEST_PROMPT = "a single red apple on a plain white table, simple product shot"
TEST_ASPECT = "9:16"

SUBMIT_TIMEOUT_SEC = 60     # POST submit — обычно быстрый (операция async)
POLL_TIMEOUT_SEC   = 30     # один GET operations/{id}
POLL_EVERY_SEC     = 5      # как часто опрашивать статус операции
CEILING_SEC        = 600    # потолок ожидания генерации


class ServerCheckThread(QThread):
    """Боевой тест генерации одной картинки. Итог — здоровье СЕРВЕРА, не ключа."""

    progress = pyqtSignal(int)        # секунд прошло
    result = pyqtSignal(str, int)     # (outcome, sec); outcome ∈ ok | down | noconn

    def __init__(self, key: str, parent=None):
        super().__init__(parent)
        self._key = str(key or "")
        self._stop = False

    def stop(self):
        """Запросить остановку (graceful shutdown). Прерывает между тиками."""
        self._stop = True

    def run(self):
        elapsed = 0
        try:
            self.progress.emit(0)
        except Exception:
            pass
        session = requests.Session()
        session.headers.update({"X-API-Key": self._key})

        # 1) Submit — POST на flow/image/generate (обычно отвечает быстро,
        #    генерация уходит в async-операцию).
        payload = {"prompt": TEST_PROMPT, "aspect_ratio": TEST_ASPECT}
        try:
            r = session.post(f"{API_BASE}/api/v4/flow/image/generate",
                             json=payload, timeout=SUBMIT_TIMEOUT_SEC)
            r.raise_for_status()
            data = r.json()
            op_id = data.get("operation_id")
            if not op_id:
                self._emit_result('down', elapsed)
                return
        except requests.exceptions.ConnectionError:
            self._emit_result('noconn', elapsed)
            return
        except Exception:
            # timeout submit, 4xx/5xx, JSON-мусор — сервер не принял задачу.
            self._emit_result('down', elapsed)
            return

        # 2) Poll — тикаем по секунде, опрашиваем статус раз в POLL_EVERY_SEC.
        last_poll = 0
        while elapsed < CEILING_SEC:
            if self._stop:
                return
            self.msleep(1000)
            elapsed += 1
            try:
                self.progress.emit(elapsed)
            except Exception:
                pass
            if elapsed - last_poll < POLL_EVERY_SEC:
                continue
            last_poll = elapsed
            try:
                pr = session.get(f"{API_BASE}/api/v4/operations/{op_id}",
                                 timeout=POLL_TIMEOUT_SEC)
                pr.raise_for_status()
                status = (pr.json() or {}).get("status")
                if status == "success":
                    self._emit_result('ok', elapsed)
                    return
                if status == "error":
                    self._emit_result('down', elapsed)
                    return
                # pending / processing — продолжаем тикать.
            except requests.exceptions.ConnectionError:
                self._emit_result('noconn', elapsed)
                return
            except Exception:
                # Транзиентный таймаут/5xx одного poll — не сдаёмся, тикаем
                # дальше до потолка (на перегруженном сервере GET тоже виснет).
                continue

        # Потолок 600с без success — сервер висит.
        self._emit_result('down', elapsed)

    def _emit_result(self, outcome: str, sec: int):
        if self._stop:
            return
        try:
            self.result.emit(str(outcome), int(sec))
        except Exception:
            pass
